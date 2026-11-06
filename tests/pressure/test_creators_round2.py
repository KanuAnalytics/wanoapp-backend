"""Round 2: trying to game or sabotage first viewers and streaks."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_creators import _fresh_served, _post_days, _reach, _viewers

pytestmark = pytest.mark.asyncio


async def _fresh_ids(client, h, limit=20):
    r = await _feed(client, h, limit=limit)
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json() if v.get("source") == "fresh"], r.json()


async def test_G1_fake_account_ring_cannot_pass_a_failed_test(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    flop = await make_video(db, c, created_at=ago(hours=3), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(flop), "impressions": 45, "fy_views": 30,
                                     "fy_portion_sum": 3.0, "fy_finished": 0})
    for _, h in await _viewers(db, 25):           # the ring watches it by link, to the end
        await client.post(f"/api/v1/videos/{flop}/watch", headers=h, json={"portion": 1})
    assert (await _reach(db, flop))["views"] == 25      # their views still count as views...
    for _, h in await _viewers(db, 5):
        fresh, _ = await _fresh_ids(client, h)
        assert flop not in fresh                          # ...but don't buy For You reach


async def test_G2_bots_loading_pages_cannot_burn_a_rivals_test(client, db, rb):
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = ids
    c, _ = await make_user(db)
    rival = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    for i in range(150):                            # 150 bot devices, never scroll
        await client.get("/api/v1/feed/v2?scenario=for-you&limit=20&authenticated=false",
                         headers={"X-Device-Id": f"bot-{i}"})
    from app.services import creators
    pool = {str(d["_id"]): d for d in await creators.first_viewers(db, [], None)}
    assert rival in pool and pool[rival]["stage"] == "test"


async def test_G3_delete_and_repost_does_not_buy_more_tests(client, db, rb):
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = ids
    c, _ = await make_user(db)
    posted = []
    for i in range(10):
        v = await make_video(db, c, created_at=ago(minutes=60 - i * 5), isReadyToStream=True)
        posted.append(v)
        if i < 9:
            await db.videos.update_one({"_id": ObjectId(v)}, {"$set": {"is_active": False}})
    for i in range(10):     # ...and floods without deleting, same day
        posted.append(await make_video(db, c, created_at=ago(minutes=4 - i * 0.1), isReadyToStream=True))
    for _, h in await _viewers(db, 30):
        await _feed(client, h, limit=20)
    served = await _fresh_served(db)
    assert len([v for v in posted if served[v]]) <= 3


async def test_G4_a_streak_does_not_rescue_a_flop(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    await _post_days(db, c, list(range(1, 11)))
    flop = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(flop), "fy_views": 60, "fy_portion_sum": 6.0, "fy_finished": 1})
    pool = [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]
    assert flop not in pool


async def test_G5_streak_raises_the_cap_but_it_is_still_a_cap(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    await _post_days(db, c, list(range(1, 11)))
    hit = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(hit), "fy_views": creators.STREAK_BOOST_CAP,
                                     "fy_portion_sum": 500.0, "fy_finished": 450})
    assert hit not in [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]


async def test_G6_real_for_you_viewers_move_a_post_from_test_to_boost(client, db, rb):
    from app.services import creators
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = ids
    c, _ = await make_user(db)
    good = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    reached = 0
    for _, h in await _viewers(db, 40):
        fresh, _ = await _fresh_ids(client, h)
        if good in fresh:
            await client.post(f"/api/v1/videos/{good}/watch", headers=h, json={"portion": 0.95})
            reached += 1
    r = await _reach(db, good)
    assert r["fy_views"] == reached >= creators.TEST_AUDIENCE
    stage = {str(d["_id"]): d["stage"] for d in await creators.first_viewers(db, [], None)}[good]
    assert stage == "boost"


async def test_G7_reporting_the_same_impression_twice_counts_once(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    _, h = await make_user(db)
    fresh, _ = await _fresh_ids(client, h)
    assert vid in fresh
    for p in (0.1, 0.5, 1.0, 1.0, 1.0):
        await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": p})
    r = await _reach(db, vid)
    assert r["fy_views"] == 1 and r["fy_finished"] == 0     # first report decides the test


async def test_G8_burst_of_simultaneous_viewers(client, db, rb):
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = ids
    new = []
    for i in range(30):
        c, _ = await make_user(db)
        new.append(await make_video(db, c, created_at=ago(minutes=i + 1), isReadyToStream=True))
    viewers = await _viewers(db, 40)
    results = await asyncio.gather(*[_feed(client, h, limit=20) for _, h in viewers])
    assert all(r.status_code == 200 and len(r.json()) == 20 for r in results)


async def test_G9_recombee_down_still_gives_first_viewers(client, db, rb):
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = ids
    rb.down = True
    c, _ = await make_user(db)
    new = await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True)
    _, h = await make_user(db)
    fresh, page = await _fresh_ids(client, h)
    assert new in fresh and len(page) == 20


async def test_G10_control_group_unaffected(client, db, rb, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 0, raising=False)
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True)
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    assert {v["source"] for v in page} == {"personal"}


async def test_G11_backdated_or_future_posts_cannot_jump_the_queue(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    future = await make_video(db, c, created_at=datetime.now(timezone.utc) + timedelta(days=2), isReadyToStream=True)
    old = await make_video(db, c, created_at=ago(days=3), isReadyToStream=True)
    pool = [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]
    assert future not in pool and old not in pool


async def test_G12_odd_reach_rows_do_not_break_the_pool(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(vid), "impressions": "many", "fy_views": float("nan"),
                                     "fy_portion_sum": None, "fy_finished": -7})
    await db.video_reach.insert_one({"_id": "junk", "fy_views": 3})
    pool = await creators.first_viewers(db, [], None)
    assert vid in [str(d["_id"]) for d in pool]
