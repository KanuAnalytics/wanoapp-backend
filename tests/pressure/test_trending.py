"""Deep pressure tests for trending (For You trending slot, GET /feed/trending,
GraphQL trendingVideos / feed). Written before the fix; each names the attack."""

import asyncio
import time

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import FakeRecombee, ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _trending_ids(client, headers=None, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = await client.get(f"/api/v1/feed/trending?{q}", headers=headers or {})
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()]


async def _users(db, n, **kw):
    return [await make_user(db, **kw) for _ in range(n)]


async def _engage(client, vid, people, *, portion=1.0, like=False, comment=False):
    for _, h in people:
        await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": portion})
        if like:
            await client.post(f"/api/v1/videos/{vid}/like", headers=h)
        if comment:
            await client.post("/api/v1/", headers=h, json={"video_id": vid, "content": "fire"})


async def _clear_cache():
    from app.services import trending
    trending.clear_cache()


# ------------------------------------------------------------------ gaming

async def test_T1_view_farming_from_one_device_counts_once(client, db, rb):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator, created_at=ago(hours=2), isReadyToStream=True)
    for _ in range(200):
        await client.post(f"/api/v1/videos/{vid}/watch?authenticated=false",
                          headers={"X-Device-Id": "farm1"}, json={"portion": 1})
    from app.services.metrics_service import metrics_buffer
    assert metrics_buffer.views_buffer.get(vid, 0) <= 1, metrics_buffer.views_buffer.get(vid)


async def test_T1_rotating_fake_devices_cannot_buy_trending(client, db, rb):
    creator, _ = await make_user(db)
    farmed = await make_video(db, creator, created_at=ago(hours=2), isReadyToStream=True, title="FARMED")
    real_creator, _ = await make_user(db)
    real = await make_video(db, real_creator, created_at=ago(hours=3), isReadyToStream=True, title="REAL")
    for i in range(400):   # one script, 400 invented device ids, same IP
        await client.post(f"/api/v1/videos/{farmed}/watch?authenticated=false",
                          headers={"X-Device-Id": f"fake{i}"}, json={"portion": 1})
    await _engage(client, real, await _users(db, 12), like=True)
    await _clear_cache()
    ids = await _trending_ids(client)
    assert real in ids
    assert farmed not in ids[: ids.index(real) + 1], "farmed views outrank real engagement"


# ------------------------------------------------------------------ what trending means

async def test_T2_momentum_beats_old_lifetime_totals(client, db, rb):
    old_creator, _ = await make_user(db)
    old_hit = await make_video(db, old_creator, created_at=ago(days=6), views_count=90000,
                               likes_count=3000, comments_count=500, isReadyToStream=True)
    new_creator, _ = await make_user(db)
    rising = await make_video(db, new_creator, created_at=ago(hours=20), views_count=0,
                              isReadyToStream=True)
    await _engage(client, rising, await _users(db, 30), like=True)
    await _clear_cache()
    ids = await _trending_ids(client)
    assert rising in ids and (old_hit not in ids or ids.index(rising) < ids.index(old_hit)), ids


async def test_T3_skipped_videos_lose_to_watched_ones(client, db, rb):
    a, _ = await make_user(db)
    skipped = await make_video(db, a, created_at=ago(hours=5), isReadyToStream=True)
    b, _ = await make_user(db)
    loved = await make_video(db, b, created_at=ago(hours=5), isReadyToStream=True)
    await _engage(client, skipped, await _users(db, 60), portion=0.05)
    people = await _users(db, 10)
    await _engage(client, loved, people, portion=1.0)
    await _engage(client, loved, people[:2], portion=1.0, like=True)
    await _clear_cache()
    ids = await _trending_ids(client)
    assert loved in ids
    assert skipped not in ids or ids.index(loved) < ids.index(skipped), ids


async def test_T4_noise_is_not_trending(client, db, rb):
    c, _ = await make_user(db)
    noise = await make_video(db, c, created_at=ago(minutes=5), likes_count=1, isReadyToStream=True)
    (_, h), = await _users(db, 1)
    await client.post(f"/api/v1/videos/{noise}/like", headers=h)
    r, _ = await make_user(db)
    real = await make_video(db, r, created_at=ago(hours=4), isReadyToStream=True)
    await _engage(client, real, await _users(db, 10), like=True)
    await _clear_cache()
    ids = await _trending_ids(client)
    assert real in ids and noise not in ids, ids


async def test_T5_country_trending_first_then_global(client, db, rb):
    ug, _ = await make_user(db, localization={"country": "UG"})
    ng, _ = await make_user(db, localization={"country": "NG"})
    ug_vid = await make_video(db, ug, country="UG", created_at=ago(hours=6), isReadyToStream=True)
    ng_vid = await make_video(db, ng, country="NG", created_at=ago(hours=6), isReadyToStream=True)
    await _engage(client, ug_vid, await _users(db, 8), like=True)
    await _engage(client, ng_vid, await _users(db, 14), like=True)
    await _clear_cache()
    viewer, h = await make_user(db, localization={"country": "UG"})
    ids = await _trending_ids(client, h)
    assert ids[:2] == [ug_vid, ng_vid], ids          # local first, global still shown
    ids_ng = await _trending_ids(client, None, country="NG")
    assert ids_ng[0] == ng_vid


async def test_T6_hidden_and_banned_never_trend(client, db, rb):
    banned, _ = await make_user(db, is_active=False)
    good, _ = await make_user(db)
    vids = {
        "banned": await make_video(db, banned, created_at=ago(hours=2), isReadyToStream=True),
        "private": await make_video(db, good, privacy="private", created_at=ago(hours=2)),
        "unlisted": await make_video(db, good, privacy="unlisted", created_at=ago(hours=2)),
        "processing": await make_video(db, good, isReadyToStream=False, created_at=ago(hours=2)),
    }
    for v in vids.values():
        await db.videos.update_one({"_id": ObjectId(v)}, {"$set": {"likes_count": 9999, "views_count": 99999}})
    real = await make_video(db, good, created_at=ago(hours=3), isReadyToStream=True)
    await _engage(client, real, await _users(db, 10), like=True)
    for k in ("private", "unlisted", "processing"):
        await db.videos.update_one({"_id": ObjectId(vids[k])}, {"$set": {"is_active": True}})
    await _clear_cache()
    ids = await _trending_ids(client)
    assert not set(vids.values()) & set(ids), ids


async def test_T7_deleted_after_trending_disappears(client, db, rb):
    c, ch = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=3), isReadyToStream=True)
    await _engage(client, vid, await _users(db, 10), like=True)
    await _clear_cache()
    assert vid in await _trending_ids(client)
    await client.delete(f"/api/v1/videos/{vid}", headers=ch)
    assert vid not in await _trending_ids(client), "cached trending kept a deleted video"


# ------------------------------------------------------------------ GraphQL trending

async def test_T8_graphql_trending_has_a_time_window():
    import inspect
    from app.graphql import subscriptions
    src = inspect.getsource(subscriptions.Subscription.trending_videos)
    assert "trending" in src and "top(" in src, "subscription still sorts all-time views"


async def test_T8_graphql_feed_is_really_trending(client, db, rb):
    c, _ = await make_user(db, localization={"country": "UG"})
    newest_dud = await make_video(db, c, country="UG", created_at=ago(minutes=1), isReadyToStream=True,
                                  title="NEWESTDUD")
    hot = await make_video(db, c, country="UG", created_at=ago(hours=5), isReadyToStream=True, title="HOT")
    await _engage(client, hot, await _users(db, 10), like=True)
    await _clear_cache()
    _, h = await make_user(db, localization={"country": "UG"})
    r = await client.post("/graphql", headers=h, json={"query": "{ feed(limit: 5) { video { title } reason } }"})
    body = r.json()
    assert not body.get("errors"), body
    titles = [x["video"]["title"] for x in body["data"]["feed"]]
    assert titles and titles[0] == "HOT", titles


# ------------------------------------------------------------------ For You integration and load

async def test_T9_for_you_trending_slot_uses_momentum(client, db, rb):
    old_creator, _ = await make_user(db)
    old_hit = await make_video(db, old_creator, created_at=ago(days=6), views_count=90000,
                               likes_count=3000, isReadyToStream=True)
    rc, _ = await make_user(db)
    rising = await make_video(db, rc, created_at=ago(hours=20), isReadyToStream=True)
    await _engage(client, rising, await _users(db, 20), like=True)
    await _clear_cache()
    _, h = await make_user(db)
    page = (await client.get("/api/v1/feed/v2?scenario=for-you&limit=20", headers=h)).json()
    trending = [v["id"] for v in page if v.get("source") == "trending"]
    assert trending and trending[0] == rising, (trending, rising, old_hit)


async def test_T10_trending_is_computed_once_under_load(client, db, rb, monkeypatch):
    from app.services import trending
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=3), isReadyToStream=True)
    await _engage(client, vid, await _users(db, 6), like=True)
    trending.clear_cache()
    calls = {"n": 0}
    real = trending._compute

    async def counting(*a, **k):
        calls["n"] += 1
        return await real(*a, **k)
    monkeypatch.setattr(trending, "_compute", counting)
    await asyncio.gather(*(client.get("/api/v1/feed/trending") for _ in range(40)))
    assert calls["n"] <= 2, calls


async def test_T11_thousands_of_recent_videos_fast(client, db, rb):
    creators = [(await make_user(db))[0] for _ in range(50)]
    docs = [{
        "creator_id": ObjectId(creators[i % 50]), "title": f"v{i}", "privacy": "public", "is_active": True,
        "isReadyToStream": True, "status": "published", "media_type": "video", "country": "UG",
        "views_count": i % 900, "likes_count": i % 120, "comments_count": i % 15,
        "created_at": ago(hours=i % 160),
    } for i in range(5000)]
    await db.videos.insert_many(docs)
    await _clear_cache()
    t = time.perf_counter()
    ids = await _trending_ids(client, limit=40)
    assert len(ids) == 40
    assert time.perf_counter() - t < 3.0


async def test_T12_bad_params(client, db, rb):
    for q in ("limit=0", "limit=500", "country=" + "x" * 50):
        r = await client.get(f"/api/v1/feed/trending?{q}")
        assert r.status_code == 422, (q, r.status_code)
