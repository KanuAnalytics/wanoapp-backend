"""Wano Creators: cash goes only to the top N qualifying creators each month
(N = pool / $10), and only real viewers earn creators points. Attacks first."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_points import _month, _post, _views, clock  # noqa: F401
from tests.pressure.test_points_pool import AFTER_OCT

pytestmark = pytest.mark.asyncio

OCT = datetime(2026, 10, 1)


async def _creator(db, *, age=30, account_days=90, followers=500, phone=True, **extra):
    u, h = await make_user(
        db,
        date_of_birth=datetime(2026 - age, 1, 1),
        created_at=datetime(2026, 11, 1, tzinfo=timezone.utc) - timedelta(days=account_days),
        followers_count=followers,
        phone_number=f"+2567{ObjectId().binary.hex()[-8:]}" if phone else None,
        **extra,
    )
    return u, h


async def _reach(db, c, viewers=60, views=1200, day=datetime(2026, 10, 20)):
    """Earning reach in the 28 days before the month closes."""
    for i in range(viewers):
        await db.points_viewer_daily.insert_one({"_id": f"{ObjectId()}:{c}:{day:%Y%m%d}", "viewer": ObjectId(),
                                                 "creator": ObjectId(c), "n": 1, "at": day})
    v = await _post(db, c, day=day)
    await _views(db, v, c, day=day, views=views)


async def _october(db, clock, creators):
    """creators: {user_id: views_per_day}; each posts 20 days in October."""
    from app.services import points
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for c, v in creators.items():
        await _month(db, c, 20, views_per_day=v)
    clock["now"] = AFTER_OCT
    await points.settle(db)


async def _share(db, u, month="2026-10"):
    r = await db.creator_earnings.find_one({"_id": f"share:{u}:{month}"})
    return (r or {}).get("cents", 0) / 100


# ---------------------------------------------------------------- top N

async def test_T1_only_the_top_n_share_the_pool(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    cs = []
    for i in range(30):
        c, _ = await _creator(db)
        await _reach(db, c)
        cs.append(c)
    await _october(db, clock, {c: 10 + i for i, c in enumerate(cs)})        # cs[-1] has the most points
    res = await points.distribute(db, "2026-10")                             # $200 pool -> top 20
    assert res["creators"] == 20
    paid = [c for c in cs if await _share(db, c) > 0]
    assert set(paid) == set(cs[10:])
    assert sum([await _share(db, c) for c in cs]) == pytest.approx(200)


async def test_T2_n_follows_the_pool(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    cs = []
    for i in range(12):
        c, _ = await _creator(db)
        await _reach(db, c)
        cs.append(c)
    await _october(db, clock, {c: 10 for c in cs})
    await points.set_pool(db, "2026-10", floor_usd=50)                        # $50 -> top 5
    assert (await points.distribute(db, "2026-10"))["creators"] == 5


# ---------------------------------------------------------------- who qualifies for cash

@pytest.mark.parametrize("what,kwargs,reach", [
    ("under 18", {"age": 16}, {}),
    ("no verified phone", {"phone": False}, {}),
    ("account under 30 days", {"account_days": 20}, {}),
    ("under 100 followers", {"followers": 40}, {}),
    ("under 50 different viewers", {}, {"viewers": 30}),
    ("under 1,000 earning views", {}, {"views": 400}),
])
async def test_Q1_each_requirement_keeps_cash_away(client, db, clock, monkeypatch, what, kwargs, reach):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    good, _ = await _creator(db)
    bad, _ = await _creator(db, **kwargs)
    await _reach(db, good)
    await _reach(db, bad, **reach)
    # Low views case: keep October itself quiet too (its views count in the 28 days).
    await _october(db, clock, {good: 10, bad: 1 if "views" in reach else 1000})
    await points.distribute(db, "2026-10")
    assert await _share(db, bad) == 0, what
    assert await _share(db, good) == pytest.approx(200)
    assert (await points.summary(db, ObjectId(bad)))["balance"] > 0       # they still have points for boosts


async def test_Q2_a_copied_post_strike_blocks_cash(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    _, ah = await make_user(db, user_type="admin")
    good, _ = await _creator(db)
    bad, _ = await _creator(db)
    for c in (good, bad):
        await _reach(db, c)
    await _october(db, clock, {good: 10, bad: 50})
    copied = await _post(db, bad, day=datetime(2026, 10, 25), hour=20)
    await client.post(f"/api/v1/points/admin/videos/{copied}/not-original", headers=ah)
    await points.distribute(db, "2026-10")
    assert await _share(db, bad) == 0 and await _share(db, good) == pytest.approx(200)


async def test_Q3_creator_status_on_my_points_page(client, db, clock):
    c, h = await _creator(db, followers=40)
    await _reach(db, c, viewers=20, views=300)
    st = (await client.get("/api/v1/points/me", headers=h)).json()["creator_status"]
    assert st["eligible"] is False
    checks = {x["key"]: x for x in st["checks"]}
    assert checks["followers"] == {"key": "followers", "ok": False, "have": 40, "need": 100}
    assert checks["viewers_28d"]["have"] == 20 and checks["viewers_28d"]["need"] == 50
    assert checks["age"]["ok"] is True and checks["phone"]["ok"] is True
    assert "date_of_birth" not in str(st)                # no birth date sent back


async def test_Q4_thresholds_are_settings(client, db, clock, monkeypatch):
    from app.core.config import settings
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    monkeypatch.setattr(settings, "CREATOR_MIN_FOLLOWERS", 1000, raising=False)
    c, _ = await _creator(db, followers=500)
    await _reach(db, c)
    await _october(db, clock, {c: 10})
    await points.distribute(db, "2026-10")
    assert await _share(db, c) == 0


# ---------------------------------------------------------------- real viewers only

async def _watch(db, vid, viewer, device=None, ip=None):
    from app.services import creators
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    await creators.record_view(db, doc, viewer=viewer, portion=1.0, device=device, ip=ip)


@pytest.fixture
def view_day(monkeypatch, clock):
    from app.services import creators
    monkeypatch.setattr(creators, "_now", lambda: datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc))
    return clock


async def _viewer(db, phone=True):
    u, _ = await make_user(db, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                           phone_number=f"+2567{ObjectId().binary.hex()[-8:]}" if phone else None)
    return u


async def _earned(db, vid):
    row = await db.post_daily.find_one({"video_id": ObjectId(vid)})
    return (row or {}).get("pviews", 0)


async def test_V1_email_only_accounts_dont_earn_creators_points(client, db, view_day):
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    await _watch(db, v, await _viewer(db, phone=False))
    await _watch(db, v, await _viewer(db, phone=True))
    assert await _earned(db, v) == 1


async def test_V2_many_accounts_on_one_phone_or_connection_earn_for_three_only(client, db, view_day, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "POINTS_IP_ACCOUNTS", 3, raising=False)   # default 50 (shared mobile IPs)
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    viewers = [await _viewer(db) for _ in range(10)]
    for u in viewers:
        await _watch(db, v, u, device="phone-1", ip="41.1.1.1")
    assert await _earned(db, v) == 3
    v2 = await _post(db, c, day=datetime(2026, 11, 10), hour=11)
    for u in viewers:                                    # new devices, same connection
        await _watch(db, v2, u, device=f"d-{u}", ip="41.1.1.1")
    assert await _earned(db, v2) == 3                     # still only the connection's same 3 accounts
    fresh = await _viewer(db)
    await _watch(db, v2, fresh, device="brand-new", ip="41.1.1.1")
    assert await _earned(db, v2) == 3


async def test_V3_parallel_accounts_on_one_device_still_capped(client, db, view_day):
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    viewers = [await _viewer(db) for _ in range(12)]
    await asyncio.gather(*[_watch(db, v, u, device="farm", ip=f"10.0.0.{i}") for i, u in enumerate(viewers)])
    assert await _earned(db, v) == 3


async def test_V4_the_same_account_on_its_own_device_isnt_double_limited(client, db, view_day):
    c, _ = await make_user(db)
    u = await _viewer(db)
    posts = [await _post(db, c, day=datetime(2026, 11, 10), hour=8 + i) for i in range(3)]
    for p in posts:
        await _watch(db, p, u, device="mine", ip="41.2.2.2")
    assert sum([await _earned(db, p) for p in posts]) == 3


async def test_V6_shared_mobile_connection_doesnt_block_real_people_by_default(client, db, view_day):
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    for i in range(20):
        await _watch(db, v, await _viewer(db), device=f"own-phone-{i}", ip="41.10.10.10")
    assert await _earned(db, v) == 20


async def test_V5_watch_endpoint_passes_device_and_ip(client, db, view_day):
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    for _ in range(5):
        u = await _viewer(db)
        from app.core.security import create_access_token
        h = {"Authorization": f"Bearer {create_access_token({'sub': u})}", "X-Device-Id": "shared-phone",
             "X-Forwarded-For": "41.3.3.3"}
        r = await client.post(f"/api/v1/videos/{v}/watch", headers=h, json={"portion": 1})
        assert r.status_code == 200, r.text
    assert await _earned(db, v) == 3


# ---------------------------------------------------------------- round 2

from tests.pressure.conftest import REAL_MONGO  # noqa: E402
real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")


@real_only
async def test_R1_choosing_winners_stays_fast_when_top_creators_dont_qualify(client, db, clock, monkeypatch):
    """2,000 locked-in creators; the 1,980 with the most points fail a check."""
    import time
    from app.services import points
    month = "2026-10"
    ids = (await db.users.insert_many([{"username": f"c{i}", "is_active": True, "followers_count": 10,
                                        "date_of_birth": datetime(1990, 1, 1), "phone_number": f"+2567{i:08d}",
                                        "created_at": datetime(2026, 1, 1)} for i in range(2000)])).inserted_ids
    await db.users.update_many({"_id": {"$in": ids[-20:]}}, {"$set": {"followers_count": 500}})
    await db.points_ledger.insert_many([{"_id": f"earn:{u}", "user_id": u, "kind": "earned", "state": "locked",
                                         "month": month, "amount": 5000 - i} for i, u in enumerate(ids)])
    day = datetime(2026, 10, 20)
    await db.post_daily.insert_many([{"_id": f"{u}:{day.date()}", "video_id": ObjectId(), "creator_id": u, "day": day,
                                      "views": 2000, "pviews": 2000} for u in ids[-20:]])
    await db.points_viewer_daily.insert_many([{"_id": f"{ObjectId()}:{u}", "viewer": ObjectId(), "creator": u, "n": 1,
                                               "at": day} for u in ids[-20:] for _ in range(60)])
    await db.points_runs.insert_one({"_id": "month:" + month})
    t0 = time.perf_counter()
    res = await points.distribute(db, month)
    took = time.perf_counter() - t0
    assert res["creators"] == 20 and took < 15, took


async def test_R2_missing_or_broken_birth_dates_never_qualify_and_dont_crash(client, db, clock):
    from app.services import points
    end = datetime(2026, 11, 1)
    for dob in (None, "1990-01-01", datetime(2030, 1, 1), 1990):
        u, _ = await _creator(db)
        await db.users.update_one({"_id": ObjectId(u)}, {"$set": {"date_of_birth": dob}})
        st = await points.creator_status(db, ObjectId(u), end)
        assert st["eligible"] is False and next(c for c in st["checks"] if c["key"] == "age")["ok"] is False


async def test_R3_turning_18_by_the_end_of_the_month_counts(client, db, clock):
    from app.services import points
    end = datetime(2026, 11, 1)
    u, _ = await _creator(db)
    await db.users.update_one({"_id": ObjectId(u)}, {"$set": {"date_of_birth": datetime(2008, 11, 1)}})
    age = next(c for c in (await points.creator_status(db, ObjectId(u), end))["checks"] if c["key"] == "age")
    assert age["ok"] is True
    await db.users.update_one({"_id": ObjectId(u)}, {"$set": {"date_of_birth": datetime(2008, 11, 2)}})
    age = next(c for c in (await points.creator_status(db, ObjectId(u), end))["checks"] if c["key"] == "age")
    assert age["ok"] is False


async def test_R4_garbage_follower_counts(client, db, clock):
    from app.services import points
    for bad in (-500, "lots", None, float("nan")):
        u, _ = await _creator(db)
        await db.users.update_one({"_id": ObjectId(u)}, {"$set": {"followers_count": bad}})
        f = next(c for c in (await points.creator_status(db, ObjectId(u), datetime(2026, 11, 1)))["checks"]
                 if c["key"] == "followers")
        assert f["ok"] is False and f["have"] == 0


async def test_R5_ties_at_the_cut_off_are_decided_the_same_way_every_time(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "max_share", lambda: 1.0)
    cs = []
    for _ in range(25):
        c, _ = await _creator(db)
        await _reach(db, c)
        cs.append(c)
    await _october(db, clock, {c: 10 for c in cs})                 # everyone tied
    shares = await points._month_shares(db, "2026-10")
    a = await points._eligible_top(db, shares, "2026-10", 20)
    b = await points._eligible_top(db, dict(reversed(list(shares.items()))), "2026-10", 20)
    assert set(a) == set(b) and len(a) == 20


async def test_R6_junk_device_ids_dont_break_or_bypass(client, db, view_day):
    c, _ = await make_user(db)
    v = await _post(db, c, day=datetime(2026, 11, 10))
    for i in range(6):
        await _watch(db, v, await _viewer(db), device="x" * 500 + "$where" + "/../" , ip="41.5.5.5")
    assert await _earned(db, v) == 3
