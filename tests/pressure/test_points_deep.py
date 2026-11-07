"""Deep pressure test of Wano points and the cash pool: every way we could find
to farm points, drain the pool or get paid twice. Attacks written before the fixes."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import REAL_MONGO, make_user, make_video
from tests.pressure.test_points import _month, _pending, _post, clock  # noqa: F401
from tests.pressure.test_points_pool import AFTER_OCT, _cash, _close_october
from tests.pressure.test_for_you import rb  # noqa: F401

pytestmark = pytest.mark.asyncio
real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")

D = datetime(2026, 11, 10)


@pytest.fixture
def day_clock(monkeypatch, clock):
    """Views are recorded on 10 Nov; points settle on 11 Nov."""
    from app.services import creators
    monkeypatch.setattr(creators, "_now", lambda: datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))
    return clock


async def _viewer(db, days_old=30, **extra):
    u, _ = await make_user(db, created_at=datetime(2026, 11, 10, tzinfo=timezone.utc) - timedelta(days=days_old), **extra)
    return u


async def _watch(db, vid, viewer, portion=1.0):
    from app.services import creators
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    await creators.record_view(db, doc, viewer=viewer, portion=portion)


# ---------------------------------------------------------------- fake views

async def test_A1_logged_out_views_earn_no_points(client, db, day_clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    for _ in range(500):
        await _watch(db, v, None)                      # 500 "devices"
    await points.settle(db)
    assert await _pending(db, c) == 0
    assert (await db.post_daily.find_one({"video_id": ObjectId(v)}))["views"] == 500   # still counted as views


async def test_A2_brand_new_accounts_earn_nothing_old_ones_do(client, db, day_clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    for _ in range(20):
        await _watch(db, v, await _viewer(db, days_old=1))
    for _ in range(3):
        await _watch(db, v, await _viewer(db, days_old=30))
    await points.settle(db)
    assert await _pending(db, c) == 3 * (points.PER_VIEW + points.PER_FINISH)


async def test_A3_one_viewer_rewatching_or_binging_is_capped_per_day(client, db, day_clock):
    from app.services import points
    c, _ = await make_user(db)
    posts = [await _post(db, c, hour=8 + i % 10) for i in range(10)]
    fan = await _viewer(db)
    for v in posts:
        for _ in range(4):                             # views reset every 6 h upstream
            await _watch(db, v, fan)
    await points.settle(db)
    assert await _pending(db, c) == points.VIEWER_DAILY_CAP * (points.PER_VIEW + points.PER_FINISH)


async def test_A4_boosted_viewers_dont_pay_back_more_than_the_boost(client, db, day_clock):
    """1 point buys 1 viewer; a viewer earns up to 2. Paid viewers must earn 0,
    or boosting would print points."""
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    paid, organic = await _viewer(db), await _viewer(db)
    await db.feed_impressions.insert_one({"viewer": paid, "video_id": ObjectId(v), "source": "boost",
                                          "at": datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)})
    await _watch(db, v, paid)
    await _watch(db, v, organic)
    await points.settle(db)
    assert await _pending(db, c) == points.PER_VIEW + points.PER_FINISH


async def test_A5_for_you_labels_paid_slots_as_boost(client, db, day_clock, monkeypatch, rb):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 100, raising=False)
    import app.services.points as P
    monkeypatch.setattr(P, "_utcnow", lambda: datetime.now(timezone.utc))
    c, _ = await make_user(db)
    v = await make_video(db, c, created_at=datetime.now(timezone.utc) - timedelta(days=5), isReadyToStream=True)
    await db.videos.update_one({"_id": ObjectId(v)}, {"$set": {"points_boost": {
        "start": 0, "target": 500, "points": 500, "until": datetime.utcnow() + timedelta(days=2), "refunded": False}}})
    _, h = await make_user(db)
    page = (await client.get("/api/v1/feed/v2?scenario=for-you&limit=10", headers=h)).json()
    srcs = {x["id"]: x.get("source") for x in page}
    assert srcs.get(v) == "boost", srcs
    imp = await db.feed_impressions.find_one({"video_id": ObjectId(v)})
    assert imp and imp["source"] == "boost"


# ---------------------------------------------------------------- the pool

async def test_B1_one_creator_cant_take_more_than_the_share_cap(client, db, clock):
    from app.services import points
    whale, _ = await make_user(db)
    small = [(await make_user(db))[0] for _ in range(3)]
    await _close_october(db, clock, {whale: 1000, **{s: 10 for s in small}})
    res = await points.distribute(db, "2026-10")
    cap = 200 * points.MAX_SHARE
    share = lambda u: db.creator_earnings.find_one({"_id": f"share:{u}:2026-10"})
    assert (await share(whale))["cents"] / 100 == pytest.approx(cap)
    for s in small:
        assert (await share(s))["cents"] / 100 <= cap
    assert res["paid_usd"] <= 200 and res["kept_usd"] == pytest.approx(200 - res["paid_usd"])


async def test_B2_pool_typo_needs_confirmation(client, db, clock):
    from app.services import points
    with pytest.raises(points.PointsError) as e:
        await points.set_pool(db, "2026-11", revenue_usd=10_000_000)      # $2,000,000 pool
    assert e.value.status == 400
    r = await points.set_pool(db, "2026-11", revenue_usd=10_000_000, confirm=True)
    assert r["pool_usd"] == 2_000_000
    _, ah = await make_user(db, user_type="admin")
    r = await client.put("/api/v1/points/admin/pool/2026-12", headers=ah, json={"revenue_usd": 9_000_000})
    assert r.status_code == 400


async def test_B3_new_cash_is_on_hold_before_it_can_be_paid(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    a, h = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.distribute(db, "2026-10")
    e = (await points.summary(db, ObjectId(a)))["earnings"]
    assert e["cash_usd"] == 0 and e["held_usd"] == 200 and e["can_request_payout"] is False
    await points.set_payout_account(db, ObjectId(a), "mobile_money", "+256700000001", "ug")
    with pytest.raises(points.PointsError):
        await points.request_payout(db, ObjectId(a))
    clock["now"] = AFTER_OCT + timedelta(days=points.HOLD_DAYS + 1)
    await points.settle(db)
    e = (await points.summary(db, ObjectId(a)))["earnings"]
    assert e["cash_usd"] == 200 and e["held_usd"] == 0
    assert (await points.request_payout(db, ObjectId(a)))["usd"] == 200


async def test_B4_copied_post_found_during_the_hold_loses_its_cash(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    _, ah = await make_user(db, user_type="admin")
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    await _close_october(db, clock, {a: 10, b: 10})
    await points.distribute(db, "2026-10")
    for doc in await db.videos.find({"creator_id": ObjectId(a)}).to_list(None):
        await client.post(f"/api/v1/points/admin/videos/{doc['_id']}/not-original", headers=ah)
    clock["now"] = AFTER_OCT + timedelta(days=points.HOLD_DAYS + 1)
    await points.settle(db)
    assert await _cash(db, a) == 0
    assert await _cash(db, b) == 100            # b's share isn't touched


async def test_B5_hidden_or_deleted_posts_dont_lock_in(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    await _month(db, c, 20, views_per_day=10)
    await points.settle(db)                               # every day counted; the month not closed yet
    first = await db.videos.find({"creator_id": ObjectId(c)}).sort("created_at", 1).to_list(2)
    await db.videos.update_one({"_id": first[0]["_id"]}, {"$set": {"is_active": False}})
    await db.videos.update_one({"_id": first[1]["_id"]}, {"$set": {"privacy": "private", "moderation.state": "held"}})
    clock["now"] = AFTER_OCT
    await points.settle(db)
    assert (await points.summary(db, ObjectId(c)))["balance"] == 180


# ---------------------------------------------------------------- payouts

async def test_C1_payout_needs_an_account_and_a_valid_method(client, db, clock):
    from app.services import points
    a, h = await make_user(db)
    await points._credit_cash(db, ObjectId(a), 2000)           # $20 available
    assert (await client.post("/api/v1/points/payouts", headers=h)).status_code == 400
    for bad in ({"method": "crypto", "account": "x", "country": "ug"},
                {"method": "pix", "account": "", "country": "br"},
                {"method": "mobile_money", "account": "<script>", "country": "ug"},
                {"method": "mobile_money", "account": "+256700000001", "country": "$ne"}):
        assert (await client.put("/api/v1/points/payout-account", headers=h, json=bad)).status_code == 422, bad
    r = await client.put("/api/v1/points/payout-account", headers=h,
                         json={"method": "mobile_money", "account": "+256 700 000 001", "country": "UG"})
    assert r.status_code == 200 and r.json()["account_hint"].endswith("001") and "700" not in r.json()["account_hint"]
    assert (await client.post("/api/v1/points/payouts", headers=h)).status_code == 200


async def test_C2_two_accounts_paying_to_the_same_number_are_flagged(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    ids = []
    for _ in range(2):
        u, h = await make_user(db)
        await points._credit_cash(db, ObjectId(u), 1000)
        await client.put("/api/v1/points/payout-account", headers=h,
                         json={"method": "mobile_money", "account": "+256700000009", "country": "ug"})
        assert (await client.post("/api/v1/points/payouts", headers=h)).status_code == 200
        ids.append(u)
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert all(x["shared_account_with"] for x in lst)
    assert {x["payout_account"]["account"] for x in lst} == {"256700000009"}       # admins see the full number


async def test_C3_banned_creators_cant_cash_out(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    a, h = await make_user(db)
    await points._credit_cash(db, ObjectId(a), 1000)
    await client.put("/api/v1/points/payout-account", headers=h,
                     json={"method": "pix", "account": "someone@example.com", "country": "br"})
    pid = (await client.post("/api/v1/points/payouts", headers=h)).json()["id"]
    await db.users.update_one({"_id": ObjectId(a)}, {"$set": {"is_active": False}})
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert lst[0]["user"]["account_active"] is False
    with pytest.raises(points.PointsError):
        await points.request_payout(db, ObjectId(a))
    assert pid


async def test_C4_payout_account_is_private(client, db, clock):
    a, h = await make_user(db)
    await client.put("/api/v1/points/payout-account", headers=h,
                     json={"method": "mobile_money", "account": "+256700000001", "country": "ug"})
    me = (await client.get("/api/v1/points/me", headers=h)).json()
    assert "+256700000001" not in str(me) and me["earnings"]["payout_account"]["method"] == "mobile_money"
    prof = await client.get(f"/api/v1/users/{a}/badges")
    assert "+256700000001" not in prof.text


# ---------------------------------------------------------------- concurrency at the month boundary

@real_only
async def test_D1_parallel_settles_at_month_end_lock_once(client, db, clock):
    from app.services import points
    users = [(await make_user(db))[0] for _ in range(5)]
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for u in users:
        await _month(db, u, 20, views_per_day=10)
    clock["now"] = AFTER_OCT
    await asyncio.gather(*[points.settle(db) for _ in range(6)])
    for u in users:
        assert (await points.summary(db, ObjectId(u)))["balance"] == 200
    clock["now"] = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
    await asyncio.gather(*[points.settle(db) for _ in range(6)])
    assert sum([await db.creator_earnings.count_documents({"user_id": ObjectId(u)}) for u in users]) == 5


# ---------------------------------------------------------------- round 2

async def test_R1_one_viewer_firing_views_in_parallel_still_capped(client, db, day_clock):
    from app.services import points
    c, _ = await make_user(db)
    posts = [await _post(db, c, hour=8 + i % 10) for i in range(12)]
    fan = await _viewer(db)
    await asyncio.gather(*[_watch(db, v, fan) for v in posts])
    await points.settle(db)
    assert await _pending(db, c) == points.VIEWER_DAILY_CAP * (points.PER_VIEW + points.PER_FINISH)


async def test_R2_banned_or_broken_viewer_accounts_earn_nothing_and_dont_crash(client, db, day_clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    banned = await _viewer(db, is_active=False)
    broken, _ = await make_user(db, created_at="yesterday")
    await _watch(db, v, banned)
    await _watch(db, v, broken)
    await _watch(db, v, str(ObjectId()))                   # deleted account
    await points.settle(db)
    assert await _pending(db, c) == 0


async def test_R3_split_never_pays_more_than_the_pool_or_the_cap(client, db):
    import random
    from app.services import points
    rnd = random.Random(7)
    for _ in range(300):
        n = rnd.randint(1, 30)
        shares = {ObjectId(): rnd.choice([1, 5, 50, 500, 50_000, 10 ** 7]) for _ in range(n)}
        cents = rnd.randint(0, 2_000_000)
        cap = rnd.choice([0.05, 0.25, 0.5, 1.0])
        out = points._split(cents, shares, cap)
        assert sum(out.values()) <= cents
        assert all(c >= 0 for c in out.values())
        if cap < 1:
            assert all(c <= int(cents * cap) for c in out.values())
        ranked = sorted(shares, key=lambda u: shares[u])
        for a, b in zip(ranked, ranked[1:]):                # more points never means less cash
            assert out.get(a, 0) <= out.get(b, 0) + 1
        if cap >= 1 and cents and sum(shares.values()):
            assert sum(out.values()) == cents               # no cap: the whole pool goes out


async def test_R4_after_the_hold_a_late_ruling_takes_points_not_paid_cash(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    _, ah = await make_user(db, user_type="admin")
    a, _ = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.distribute(db, "2026-10")
    clock["now"] = AFTER_OCT + timedelta(days=points.HOLD_DAYS + 1)
    await points.settle(db)
    v = (await db.videos.find_one({"creator_id": ObjectId(a)}))["_id"]
    await client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=ah)
    assert await _cash(db, a) == 200
    assert (await points.summary(db, ObjectId(a)))["balance"] == 190


async def test_R5_several_rulings_during_the_hold_add_up_and_never_go_negative(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    _, ah = await make_user(db, user_type="admin")
    a, _ = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.distribute(db, "2026-10")
    vids = [d["_id"] for d in await db.videos.find({"creator_id": ObjectId(a)}).to_list(None)]
    await asyncio.gather(*[client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=ah) for v in vids[:5]])
    row = await db.creator_earnings.find_one({"_id": f"share:{a}:2026-10"})
    w = await db.earnings_wallet.find_one({"_id": ObjectId(a)})
    assert row["cents"] == 15000 and w["held_cents"] == row["cents"]
    for v in vids:
        await client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=ah)
    row = await db.creator_earnings.find_one({"_id": f"share:{a}:2026-10"})
    w = await db.earnings_wallet.find_one({"_id": ObjectId(a)})
    assert row["cents"] == 0 and w["held_cents"] == 0


async def test_R6_payout_takes_only_released_cash(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    a, _ = await make_user(db)
    await points._credit_cash(db, ObjectId(a), 700)
    await _close_october(db, clock, {a: 10})
    await points.distribute(db, "2026-10")
    await points.set_payout_account(db, ObjectId(a), "mobile_money", "+256700000001", "ug")
    assert (await points.request_payout(db, ObjectId(a)))["usd"] == 7
    e = (await points.summary(db, ObjectId(a)))["earnings"]
    assert e["cash_usd"] == 0 and e["held_usd"] == 200


async def test_R7_the_same_number_written_differently_is_still_flagged(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    for acct in ("+256 700 000 009", "256-700-000-009"):
        u, h = await make_user(db)
        await points._credit_cash(db, ObjectId(u), 1000)
        r = await client.put("/api/v1/points/payout-account", headers=h,
                             json={"method": "mobile_money", "account": acct, "country": "ug"})
        assert r.status_code == 200
        await client.post("/api/v1/points/payouts", headers=h)
    for acct in ("Someone@Example.com", "someone@example.com"):
        u, h = await make_user(db)
        await points._credit_cash(db, ObjectId(u), 1000)
        await client.put("/api/v1/points/payout-account", headers=h,
                         json={"method": "pix", "account": acct, "country": "br"})
        await client.post("/api/v1/points/payouts", headers=h)
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert len(lst) == 4 and all(len(x["shared_account_with"]) == 1 for x in lst)


async def test_R8_changing_the_account_after_asking_doesnt_redirect_the_payout(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    a, h = await make_user(db)
    await points._credit_cash(db, ObjectId(a), 1000)
    await client.put("/api/v1/points/payout-account", headers=h,
                     json={"method": "mobile_money", "account": "+256700000001", "country": "ug"})
    await client.post("/api/v1/points/payouts", headers=h)
    await client.put("/api/v1/points/payout-account", headers=h,
                     json={"method": "mobile_money", "account": "+256799999999", "country": "ug"})
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert lst[0]["payout_account"]["account"] == "256700000001"


async def test_R9_confirming_a_big_pool_through_the_api(client, db, clock):
    _, ah = await make_user(db, user_type="admin")
    r = await client.put("/api/v1/points/admin/pool/2026-12", headers=ah,
                         json={"revenue_usd": 9_000_000, "confirm": True})
    assert r.status_code == 200 and r.json()["pool_usd"] == 1_800_000
    assert (await client.get("/api/v1/points/admin/pool/2026-12", headers=ah)).status_code == 200


@real_only
async def test_R10_earning_checks_keep_views_fast(client, db, day_clock):
    import time
    c, _ = await make_user(db)
    v = await _post(db, c)
    viewers = [await _viewer(db) for _ in range(300)]
    t0 = time.perf_counter()
    for u in viewers:
        await _watch(db, v, u)
    per_view = (time.perf_counter() - t0) / len(viewers)
    assert per_view < 0.02, per_view
