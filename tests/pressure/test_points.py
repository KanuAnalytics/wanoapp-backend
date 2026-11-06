"""Wano points: earned from real views on original posts in a set category, only on
days the creator posts; a month counts only with 20+ posting days; spent on extra
reach (boost); cash payout later, admin-approved, off by default.
Attacks written before the code."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

D = datetime(2026, 11, 10)            # a Tuesday; settled from 11 Nov 01:00 UTC


@pytest.fixture
def clock(monkeypatch):
    from app.services import points
    state = {"now": datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(points, "_utcnow", lambda: state["now"])
    return state


async def _post(db, c, day=D, hour=9, category="comedy", **extra):
    return await make_video(db, c, created_at=(day + timedelta(hours=hour)).replace(tzinfo=timezone.utc),
                            isReadyToStream=True, category=category, **extra)


async def _views(db, vid, creator, day=D, views=0, finished=0):
    await db.post_daily.update_one(
        {"_id": f"{vid}:{day.date().isoformat()}"},
        {"$inc": {"views": views, "finished": finished},
         "$setOnInsert": {"video_id": ObjectId(vid), "creator_id": ObjectId(creator), "day": day}},
        upsert=True)


async def _pending(db, user):
    from app.services import points
    return (await points.summary(db, ObjectId(user)))["pending"]


async def _balance(db, user):
    from app.services import points
    return (await points.summary(db, ObjectId(user)))["balance"]


async def _month(db, c, days, views_per_day=10, year=2026, month=10):
    for d in range(1, days + 1):
        day = datetime(year, month, d)
        vid = await _post(db, c, day=day)
        await _views(db, vid, c, day=day, views=views_per_day)


# ---------------------------------------------------------------- earning

async def test_E1_views_on_a_posting_day_earn_pending_points(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    await _views(db, v, c, views=100, finished=30)
    await points.settle(db)
    assert await _pending(db, c) == 100 * points.PER_VIEW + 30 * points.PER_FINISH
    assert await _balance(db, c) == 0                         # not spendable until the month locks in


async def test_E2_no_post_that_day_no_points(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    old = await _post(db, c, day=D - timedelta(days=3))
    await _views(db, old, c, views=500)                       # views today on an old post, no post today
    await points.settle(db)
    assert await _pending(db, c) == 0


async def test_E3_older_qualifying_posts_earn_on_a_posting_day(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    old = await _post(db, c, day=D - timedelta(days=3))
    await _post(db, c)
    await _views(db, old, c, views=50)
    await points.settle(db)
    assert await _pending(db, c) == 50 * points.PER_VIEW


async def test_E4_posts_that_dont_qualify_neither_earn_nor_count_as_posting(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    bad = [await _post(db, c, category=None), await _post(db, c, category="gambling"),
           await _post(db, c, privacy="private"), await _post(db, c, moderation={"state": "held"}),
           await _post(db, c, is_active=False), await _post(db, c, status="scheduled"),
           await _post(db, c, originality={"state": "rejected"})]
    for v in bad:
        await _views(db, v, c, views=100)
    await points.settle(db)
    assert await _pending(db, c) == 0
    assert (await points.summary(db, ObjectId(c)))["posting_days"] == 0


async def test_E5_two_copy_reports_stop_a_post_earning(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    await _views(db, v, c, views=100)
    for cat in ("intellectual_property", "spam"):
        await db.reports.insert_one({"video_id": ObjectId(v), "reporter_id": ObjectId(), "category": cat,
                                     "is_active": True, "created_at": datetime.utcnow()})
    await points.settle(db)
    assert await _pending(db, c) == 0


async def test_E6_daily_cap(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    await _views(db, v, c, views=10 ** 7)
    await points.settle(db)
    assert await _pending(db, c) == points.DAILY_CAP


async def test_E7_settling_twice_or_concurrently_pays_once(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c)
    await _views(db, v, c, views=100)
    await asyncio.gather(*[points.settle(db) for _ in range(4)])
    await points.settle(db)
    assert await _pending(db, c) == 100


async def test_E8_today_is_not_settled_early_but_missed_days_are_caught_up(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    for back in range(4):
        day = D - timedelta(days=back)
        v = await _post(db, c, day=day)
        await _views(db, v, c, day=day, views=10)
    today = await _post(db, c, day=D + timedelta(days=1))
    await _views(db, today, c, day=D + timedelta(days=1), views=999)
    await points.settle(db)
    assert await _pending(db, c) == 40


async def test_E9_record_view_feeds_post_daily_and_ignores_own_views(client, db, clock):
    from app.services import creators
    c, _ = await make_user(db)
    v = await _post(db, c)
    doc = await db.videos.find_one({"_id": ObjectId(v)})
    await creators.record_view(db, doc, viewer=None, portion=1.0)
    await creators.record_view(db, doc, viewer=c, portion=1.0)
    rows = await db.post_daily.find({"video_id": ObjectId(v)}).to_list(None)
    assert sum(r["views"] for r in rows) == 1 and sum(r["finished"] for r in rows) == 1


# ---------------------------------------------------------------- the month

async def test_M1_twenty_posting_days_lock_the_month_in(client, db, clock):
    from app.services import points
    good, _ = await make_user(db)
    short, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    await _month(db, good, 20)
    await _month(db, short, 19)
    clock["now"] = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    await points.settle(db)
    assert await _balance(db, good) == 200 and await _pending(db, good) == 0
    assert await _balance(db, short) == 0 and await _pending(db, short) == 0
    hist = (await points.summary(db, ObjectId(short)))["history"]
    assert any(h["kind"] == "expired" for h in hist)


async def test_M2_two_posts_a_day_is_still_one_posting_day(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for d in range(1, 11):
        for h in (8, 14):
            v = await _post(db, c, day=datetime(2026, 10, d), hour=h)
            await _views(db, v, c, day=datetime(2026, 10, d), views=5)
    clock["now"] = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    assert await _balance(db, c) == 0                          # 10 days, not 20


# ---------------------------------------------------------------- spending on reach

async def _rich(db, amount=1000):
    from app.services import points
    c, h = await make_user(db)
    await points.grant(db, ObjectId(c), amount, "test")
    return c, h


async def test_B1_boost_spends_points_and_adds_reach(client, db, clock):
    from app.services import creators, points
    c, h = await _rich(db)
    v = await _post(db, c, day=datetime.utcnow() - timedelta(days=5))     # too old for normal first viewers
    r = await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 300})
    assert r.status_code == 200, r.text
    assert r.json()["extra_viewers"] == 300 * points.VIEWERS_PER_POINT
    assert await _balance(db, c) == 700
    got = await creators.first_viewers(db, [], None)
    assert v in [str(d["_id"]) for d in got if d.get("stage") == "paid"]
    await db.video_reach.update_one({"_id": ObjectId(v)}, {"$inc": {"impressions": 300 * points.VIEWERS_PER_POINT}},
                                    upsert=True)
    assert v not in [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]


async def test_B2_boost_rules(client, db, clock):
    from app.services import points
    c, h = await _rich(db, 500)
    other, _ = await make_user(db)
    mine = await _post(db, c, day=datetime.utcnow())
    theirs = await _post(db, other, day=datetime.utcnow())
    hidden = await _post(db, c, day=datetime.utcnow(), privacy="private")
    post = lambda vid, n: client.post("/api/v1/points/boost", headers=h, json={"video_id": vid, "points": n})
    assert (await post(theirs, 100)).status_code == 403
    assert (await post(hidden, 100)).status_code == 400
    assert (await post(mine, points.MIN_BOOST - 1)).status_code == 422
    assert (await post(mine, 600)).status_code == 400              # more than the balance
    assert (await post("bad", 100)).status_code == 400
    assert (await post(mine, -100)).status_code == 422
    assert await _balance(db, c) == 500


async def test_B3_concurrent_boosts_cannot_overdraw(client, db, clock):
    c, h = await _rich(db, 300)
    v = await _post(db, c, day=datetime.utcnow())
    rs = await asyncio.gather(*[client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 100})
                                for _ in range(8)])
    assert sum(r.status_code == 200 for r in rs) == 3
    assert await _balance(db, c) == 0


async def test_B4_unused_boost_is_refunded_when_it_ends(client, db, clock):
    from app.services import points
    c, h = await _rich(db, 400)
    v = await _post(db, c, day=datetime.utcnow())
    await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 400})
    await db.video_reach.update_one({"_id": ObjectId(v)}, {"$inc": {"impressions": 100 * points.VIEWERS_PER_POINT}},
                                    upsert=True)
    clock["now"] = clock["now"] + timedelta(days=points.BOOST_DAYS, hours=1)
    await points.settle(db)
    await points.settle(db)
    assert await _balance(db, c) == 300


async def test_B5_a_post_held_during_a_boost_stops_and_refunds(client, db, clock):
    from app.services import creators, points
    c, h = await _rich(db, 200)
    v = await _post(db, c, day=datetime.utcnow())
    await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 200})
    await db.videos.update_one({"_id": ObjectId(v)}, {"$set": {"privacy": "private", "moderation.state": "held"}})
    assert v not in [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]
    clock["now"] = clock["now"] + timedelta(days=points.BOOST_DAYS, hours=1)
    await points.settle(db)
    assert await _balance(db, c) == 200


# ---------------------------------------------------------------- payouts

async def test_P1_payouts_are_off_until_wano_turns_them_on(client, db, clock, monkeypatch):
    from app.services import points
    c, h = await _rich(db, 10 ** 6)
    r = await client.post("/api/v1/points/payouts", headers=h)
    assert r.status_code == 403
    monkeypatch.setattr(points, "payouts_enabled", lambda: True)
    low, lh = await _rich(db, points.payout_minimum() - 1)
    assert (await client.post("/api/v1/points/payouts", headers=lh)).status_code == 400
    r = await client.post("/api/v1/points/payouts", headers=h)
    assert r.status_code == 200 and r.json()["points"] == 10 ** 6
    assert await _balance(db, c) == 0
    assert (await client.post("/api/v1/points/payouts", headers=h)).status_code == 400   # nothing left


async def test_P2_admin_approves_or_rejects(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "payouts_enabled", lambda: True)
    a, ah = await make_user(db, user_type="admin")
    c, h = await _rich(db, points.payout_minimum())
    d, dh = await _rich(db, points.payout_minimum())
    pid = (await client.post("/api/v1/points/payouts", headers=h)).json()["id"]
    qid = (await client.post("/api/v1/points/payouts", headers=dh)).json()["id"]
    assert (await client.post(f"/api/v1/points/admin/payouts/{pid}/approve", headers=h)).status_code == 403
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert {x["id"] for x in lst} == {pid, qid}
    assert (await client.post(f"/api/v1/points/admin/payouts/{pid}/approve", headers=ah)).status_code == 200
    assert (await client.post(f"/api/v1/points/admin/payouts/{pid}/reject", headers=ah)).status_code == 409
    assert (await client.post(f"/api/v1/points/admin/payouts/{qid}/reject", headers=ah)).status_code == 200
    assert await _balance(db, c) == 0 and await _balance(db, d) == points.payout_minimum()


# ---------------------------------------------------------------- originality, categories, API

async def test_O1_admin_marks_a_post_not_original_and_its_points_go(client, db, clock):
    from app.services import points
    a, ah = await make_user(db, user_type="admin")
    c, _ = await make_user(db)
    v = await _post(db, c)
    keep = await _post(db, c, hour=12)
    await _views(db, v, c, views=100)
    await _views(db, keep, c, views=10)
    await points.settle(db)
    r = await client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=ah)
    assert r.status_code == 200
    assert await _pending(db, c) == 10
    _, uh = await make_user(db)
    assert (await client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=uh)).status_code == 403


async def test_O2_revoking_locked_points_already_spent_goes_negative_and_blocks_spending(client, db, clock):
    from app.services import points
    a, ah = await make_user(db, user_type="admin")
    c, h = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    await _month(db, c, 20, views_per_day=10)
    clock["now"] = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    v = await _post(db, c, day=datetime.utcnow())
    assert (await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 150})).status_code == 200
    first = await db.videos.find_one({"creator_id": ObjectId(c), "created_at": {"$lt": datetime(2026, 10, 2)}})
    for doc in await db.videos.find({"creator_id": ObjectId(c), "created_at": {"$lt": datetime(2026, 10, 11)}}).to_list(None):
        await client.post(f"/api/v1/points/admin/videos/{doc['_id']}/not-original", headers=ah)
    assert await _balance(db, c) == -50
    assert (await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 100})).status_code == 400
    assert first


async def test_C1_category_set_by_the_creator_only(client, db, clock):
    c, h = await make_user(db)
    _, oh = await make_user(db)
    v = await _post(db, c, category=None)
    assert (await client.put(f"/api/v1/points/videos/{v}/category", headers=oh, json={"category": "music"})).status_code == 403
    assert (await client.put(f"/api/v1/points/videos/{v}/category", headers=h, json={"category": "gambling"})).status_code == 422
    assert (await client.put(f"/api/v1/points/videos/{v}/category", headers=h, json={"category": "music"})).status_code == 200
    assert (await db.videos.find_one({"_id": ObjectId(v)}))["category"] == "music"
    cats = (await client.get("/api/v1/points/categories", params={"authenticated": "false"})).json()
    assert {"comedy", "music", "food_culture", "learn_news", "film", "sports"} == {x["id"] for x in cats}


async def test_A1_my_points_page(client, db, clock):
    from app.services import points
    c, h = await make_user(db)
    v = await _post(db, c, day=datetime.utcnow())
    r = await client.get("/api/v1/points/me", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert {"balance", "pending", "posting_days", "days_needed", "month", "boost", "payout", "history"} <= set(body)
    assert body["days_needed"] == points.QUALIFY_DAYS and body["payout"]["enabled"] is False
    assert (await client.get("/api/v1/points/me")).status_code == 401
    assert v


# ---------------------------------------------------------------- round 2

from tests.pressure.conftest import REAL_MONGO  # noqa: E402

real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")


async def test_R1_revoking_twice_at_once_takes_points_once(client, db, clock):
    from app.services import points
    a, ah = await make_user(db, user_type="admin")
    c, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    await _month(db, c, 20)
    clock["now"] = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    v = (await db.videos.find_one({"creator_id": ObjectId(c)}))["_id"]
    await asyncio.gather(*[client.post(f"/api/v1/points/admin/videos/{v}/not-original", headers=ah) for _ in range(5)])
    assert await _balance(db, c) == 190


async def test_R2_two_payout_requests_at_once_reserve_once(client, db, clock, monkeypatch):
    from app.services import points
    monkeypatch.setattr(points, "payouts_enabled", lambda: True)
    c, h = await _rich(db, points.payout_minimum() * 2)
    rs = await asyncio.gather(*[client.post("/api/v1/points/payouts", headers=h) for _ in range(5)])
    assert sum(r.status_code == 200 for r in rs) == 1
    assert await db.points_payouts.count_documents({}) == 1 and await _balance(db, c) == 0


async def test_R3_boosted_post_not_shown_to_its_creator_or_twice(client, db, clock):
    from app.services import creators
    c, h = await _rich(db, 500)
    v = await _post(db, c, day=datetime.utcnow())
    await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 200})
    await client.post("/api/v1/points/boost", headers=h, json={"video_id": v, "points": 200})   # tops up
    assert v not in [str(d["_id"]) for d in await creators.first_viewers(db, [], ObjectId(c))]
    assert v not in [str(d["_id"]) for d in await creators.first_viewers(db, [ObjectId(v)], None)]
    got = [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]
    assert got.count(v) == 1
    b = (await db.videos.find_one({"_id": ObjectId(v)}))["points_boost"]
    assert b["target"] == 400 and b["points"] == 400


async def test_R4_last_minute_of_the_month_counts_for_that_month(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for d in range(12, 32):                                   # 12-31 Oct = 20 days, last one at 23:59
        day = datetime(2026, 10, d)
        v = await _post(db, c, day=day, hour=23 if d == 31 else 9)
        await _views(db, v, c, day=day, views=1)
    clock["now"] = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)    # server was off for a week
    await points.settle(db)
    assert await _balance(db, c) == 20


@real_only
async def test_R5_settling_a_busy_day_is_quick(client, db, clock):
    import time
    from app.services import points
    users = (await db.users.insert_many([{"username": f"p{i}", "is_active": True} for i in range(3000)])).inserted_ids
    vids = (await db.videos.insert_many([{"creator_id": u, "is_active": True, "privacy": "public",
                                          "isReadyToStream": True, "category": "comedy",
                                          "created_at": D + timedelta(hours=10)} for u in users])).inserted_ids
    await db.post_daily.insert_many([{"_id": f"{v}:{D.date()}", "video_id": v, "creator_id": u, "day": D,
                                      "views": 7, "finished": 1} for v, u in zip(vids, users)])
    t0 = time.perf_counter()
    await points.settle(db)
    assert time.perf_counter() - t0 < 10
    assert await db.points_ledger.count_documents({"kind": "earned"}) == 3000
