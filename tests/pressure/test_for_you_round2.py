"""Round 2: trying to break the new For You feed."""

import asyncio
import time

import pytest
from bson import ObjectId
from recombee_api_client.api_requests import RecommendItemsToUser

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import FakeRecombee, _catalog, _feed, _world, ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_R2_garbage_ids_from_recombee_are_ignored(client, db, rb):
    _, ids = await _catalog(db, n=12, creators=6)
    rb.pool = ["notanid", "", str(ObjectId()), "../x"] + ids  # junk, deleted, real
    _, h = await make_user(db)
    r = await _feed(client, h, limit=10)
    assert r.status_code == 200 and len(r.json()) == 10


async def test_R2_bad_next_recomm_id_still_returns_a_page(client, db, rb):
    _, ids = await _catalog(db, n=20, creators=6)
    rb.pool = ids
    _, h = await make_user(db)
    r = await _feed(client, h, limit=10, next_recomm_id="does-not-exist")
    assert r.status_code == 200 and len(r.json()) == 10


@pytest.mark.parametrize("limit", [1, 2, 5, 50])
async def test_R2_any_page_size(client, db, rb, limit):
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=limit)).json()
    assert len(page) == min(limit, 43)
    assert len({v["id"] for v in page}) == len(page)


@pytest.mark.parametrize("limit", [0, 51, -5])
async def test_R2_bad_page_size_rejected(client, db, rb, limit):
    _, h = await make_user(db)
    assert (await _feed(client, h, limit=limit)).status_code == 422


async def test_R2_old_app_without_device_id_still_works(client, db, rb):
    _, ids = await _catalog(db, n=10, creators=6)
    rb.pool = ids
    r = await _feed(client, None, authenticated="false", limit=5)
    assert r.status_code == 200 and len(r.json()) == 5
    assert rb.of(RecommendItemsToUser)[0].user_id == "temp-user"


async def test_R2_banned_creator_never_appears(client, db, rb):
    banned, _ = await make_user(db, is_active=False)
    bad = [await make_video(db, banned, created_at=ago(hours=h), title="BANNED", likes_count=99999,
                            isReadyToStream=True) for h in (1, 5, 30)]
    ids, *_ = await _world(db)
    rb.pool = bad + ids
    _, h = await make_user(db, following=[ObjectId(banned)])
    for _ in range(3):
        assert "BANNED" not in (await _feed(client, h, limit=20)).text


async def test_R2_private_featured_video_not_leaked(client, db, rb):
    owner, _ = await make_user(db)
    priv = await make_video(db, owner, privacy="private", title="PRIVATEFEAT")
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    r = await _feed(client, h, video_id=priv, limit=10)
    assert "PRIVATEFEAT" not in r.text and len(r.json()) == 10


async def test_R2_not_interested_never_comes_back(client, db, rb):
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    nope = page[0]["id"]
    assert (await client.post(f"/api/v1/videos/{nope}/not-interested", headers=h)).status_code == 200
    await db.feed_impressions.delete_many({})   # even after the 24 h memory expires
    for _ in range(4):
        assert nope not in {v["id"] for v in (await _feed(client, h, limit=10)).json()}


async def test_R2_concurrent_requests_dont_crash(client, db, rb):
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    rs = await asyncio.gather(*(_feed(client, h, limit=10) for _ in range(15)))
    assert {r.status_code for r in rs} == {200}
    for r in rs:
        page = [v["id"] for v in r.json()]
        assert len(page) == len(set(page))


async def test_R2_thousands_of_videos_stay_fast(client, db, rb):
    creators = [(await make_user(db))[0] for _ in range(60)]
    docs = []
    for i in range(3000):
        docs.append({
            "creator_id": ObjectId(creators[i % 60]), "title": f"v{i}", "description": "", "video_type": "bits",
            "privacy": "public", "is_active": True, "isReadyToStream": True, "status": "published",
            "media_type": "video", "images": [], "views_count": i % 700, "likes_count": i % 97,
            "comments_count": i % 13, "created_at": ago(hours=i % 400),
            "remoteUrl_CF": f"https://videodelivery.net/{'%032x' % i}/manifest/video.m3u8",
        })
    res = await db.videos.insert_many(docs)
    rb.pool = [str(x) for x in res.inserted_ids[::7]]
    _, h = await make_user(db, following=[ObjectId(c) for c in creators[:20]])
    t = time.perf_counter()
    nxt = None
    for _ in range(3):
        page = (await _feed(client, h, limit=20, **({"next_recomm_id": nxt} if nxt else {}))).json()
        assert len(page) == 20
        nxt = page[-1]["recomm_id"]
    per_page = (time.perf_counter() - t) / 3
    # In-memory test DB is much slower than real Mongo with indexes; this guards
    # against accidental full scans per item, not real latency.
    assert per_page < 3.0, f"{per_page:.2f}s per page"


async def test_R2_rollout_splits_viewers_evenly(monkeypatch):
    from app.core.config import settings
    from app.services.for_you import variant_for
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 50)
    mix = sum(variant_for(f"user{i}") == "mix" for i in range(2000))
    assert 900 < mix < 1100, mix
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 0)
    assert all(variant_for(f"u{i}") == "control" for i in range(200))
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 100)
    assert all(variant_for(f"u{i}") == "mix" for i in range(200))


async def test_R2_report_with_no_data(client, db, rb):
    _, ah = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/metrics/for-you", headers=ah)
    assert r.status_code == 200 and r.json()["variants"] == {}


async def test_R2_watch_without_feed_impression_is_fine(client, db, rb):
    _, ids = await _catalog(db, n=2)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/videos/{ids[0]}/watch", headers=h, json={"portion": 0.5})
    assert r.status_code == 200


async def test_R2_impression_index_and_ttl_defined():
    import inspect
    from app.core import database
    src = inspect.getsource(database)
    assert "feed_impressions" in src and "expireAfterSeconds" in src


async def test_R2_tags_booster_only_when_user_has_tags(client, db, rb):
    _, ids = await _catalog(db, n=10, creators=6)
    rb.pool = ids
    _, h = await make_user(db, tags=[])
    await _feed(client, h)
    assert getattr(rb.of(RecommendItemsToUser)[-1], "booster", None) in (None,) or \
        not isinstance(rb.of(RecommendItemsToUser)[-1].booster, str)
