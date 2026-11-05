"""Round 2: attacks on the new trending engine itself."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_trending import _clear_cache, _engage, _trending_ids, _users

pytestmark = pytest.mark.asyncio


async def _stats(db, vid):
    tot = {}
    async for r in db.video_stats_hourly.find({"video_id": ObjectId(vid)}):
        for k, v in r.items():
            if isinstance(v, (int, float)) and k not in ("_id",):
                tot[k] = tot.get(k, 0) + v
    return tot


async def test_R2_shared_carrier_ip_still_counts_real_users(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    people = await _users(db, 120)
    for _, h in people:   # all behind one carrier IP
        await client.post(f"/api/v1/videos/{vid}/watch", headers={**h, "X-Forwarded-For": "41.210.1.1"},
                          json={"portion": 1})
    assert (await _stats(db, vid)).get("views") == 120


async def test_R2_like_unlike_toggling_counts_once(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    _, h = await make_user(db)
    for _ in range(40):
        await client.post(f"/api/v1/videos/{vid}/like", headers=h)
        await client.delete(f"/api/v1/videos/{vid}/like", headers=h)
    assert (await _stats(db, vid)).get("likes", 0) <= 1


async def test_R2_comment_spam_counts_once_per_person(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    _, h = await make_user(db)
    for i in range(30):
        await client.post("/api/v1/", headers=h, json={"video_id": vid, "content": f"spam {i}"})
    assert (await _stats(db, vid)).get("comments", 0) <= 1


async def test_R2_share_spam_counts_once_per_person(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    _, h = await make_user(db)
    for _ in range(30):
        await client.post(f"/api/v1/videos/{vid}/share", headers=h)
    assert (await _stats(db, vid)).get("shares", 0) <= 1


async def test_R2_creator_cannot_trend_their_own_video(client, db, rb):
    creator, ch = await make_user(db)
    vid = await make_video(db, creator, created_at=ago(hours=1), isReadyToStream=True)
    await client.post(f"/api/v1/videos/{vid}/watch", headers=ch, json={"portion": 1})
    await client.post(f"/api/v1/videos/{vid}/like", headers=ch)
    await client.post("/api/v1/", headers=ch, json={"video_id": vid, "content": "me"})
    await client.post(f"/api/v1/videos/{vid}/share", headers=ch)
    s = await _stats(db, vid)
    assert not any(s.get(k) for k in ("likes", "comments", "shares", "finished")), s


async def test_R2_logged_out_finishes_alone_never_trend(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    for i in range(500):
        await client.post(f"/api/v1/videos/{vid}/watch?authenticated=false",
                          headers={"X-Device-Id": f"d{i}", "X-Forwarded-For": f"10.0.{i // 250}.{i % 250}"},
                          json={"portion": 1})
    await _clear_cache()
    assert vid not in await _trending_ids(client)


async def test_R2_momentum_decays_with_time(client, db, rb):
    from app.services import trending
    c1, _ = await make_user(db)
    c2, _ = await make_user(db)
    old = await make_video(db, c1, created_at=ago(hours=40), isReadyToStream=True)
    new = await make_video(db, c2, created_at=ago(hours=3), isReadyToStream=True)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    same = {"views": 50, "finished": 30, "likes": 20}
    await db.video_stats_hourly.insert_many([
        {"_id": f"{old}:a", "video_id": ObjectId(old), "hour": now - timedelta(hours=36), **same},
        {"_id": f"{new}:a", "video_id": ObjectId(new), "hour": now - timedelta(hours=1), **same},
    ])
    trending.clear_cache()
    ids = await _trending_ids(client)
    assert ids.index(new) < ids.index(old)
    # Beyond the 48 h window it is gone from momentum entirely.
    await db.video_stats_hourly.update_one({"_id": f"{old}:a"}, {"$set": {"hour": now - timedelta(hours=60)}})
    trending.clear_cache()
    ranked = await trending._compute(db)
    assert all(not (v == old and basis == "momentum") for v, _, _, basis in ranked)


async def test_R2_cache_picks_up_new_engagement_after_expiry(client, db, rb, monkeypatch):
    from app.services import trending
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    assert vid not in await _trending_ids(client)
    await _engage(client, vid, await _users(db, 8), like=True)
    monkeypatch.setattr(trending, "CACHE_SECONDS", 0)
    assert vid in await _trending_ids(client)


async def test_R2_unknown_country_falls_back_to_global(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, country="NG", created_at=ago(hours=2), isReadyToStream=True)
    await _engage(client, vid, await _users(db, 8), like=True)
    await _clear_cache()
    assert vid in await _trending_ids(client, None, country="ZZ")


async def test_R2_odd_documents_do_not_crash(client, db, rb):
    c, _ = await make_user(db)
    await db.videos.insert_many([
        {"creator_id": ObjectId(c), "is_active": True, "privacy": "public", "likes_count": -5,
         "views_count": None, "created_at": ago(hours=1), "country": 123},
        {"creator_id": ObjectId(c), "is_active": True, "privacy": "public", "likes_count": 50,
         "comments_count": "x"},           # no created_at, bad types
        {"creator_id": None, "is_active": True, "privacy": "public", "likes_count": 90, "created_at": ago(hours=2)},
    ])
    await db.video_stats_hourly.insert_one({"_id": "junk", "video_id": "notanid", "hour": ago(hours=1),
                                            "likes": "lots"})
    await _clear_cache()
    r = await client.get("/api/v1/feed/trending")
    assert r.status_code == 200, r.text


async def test_R2_graphql_feed_offset_past_end(client, db, rb):
    _, h = await make_user(db)
    r = await client.post("/graphql", headers=h, json={"query": "{ feed(limit: 5, offset: 500) { reason } }"})
    body = r.json()
    assert not body.get("errors") and body["data"]["feed"] == []


async def test_R2_graphql_subscription_yields_trending(client, db, rb):
    from types import SimpleNamespace
    from app.graphql.subscriptions import Subscription
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True, title="SUBHOT")
    old = await make_video(db, c, created_at=ago(days=200), views_count=10 ** 6, likes_count=10 ** 5,
                           isReadyToStream=True, title="ANCIENT")
    await _engage(client, vid, await _users(db, 8), like=True)
    await _clear_cache()
    viewer, _ = await make_user(db)
    info = SimpleNamespace(context={"user_id": viewer})
    gen = Subscription().trending_videos(info)
    first = await gen.__anext__()
    await gen.aclose()
    titles = [v.title for v in first]
    assert "SUBHOT" in titles and "ANCIENT" not in titles, titles


async def test_R2_ttl_indexes_defined():
    import inspect
    from app.core import database
    src = inspect.getsource(database)
    assert "video_stats_hourly" in src and "view_dedupe" in src
