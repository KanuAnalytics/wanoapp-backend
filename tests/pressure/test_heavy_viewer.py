"""A heavy viewer (the founder's account: has watched nearly everything) got
blank For You, Trending and Discover. Feeds must never go empty because of
"already watched"; unseen posts still come first, and "Not interested" never
comes back."""

from datetime import datetime

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import FakeRecombee, _catalog, _feed, rb, mix_on  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _watched(db, uid, ids, not_interested=()):
    await db.watch_history.insert_many(
        [{"user_id": ObjectId(uid), "video_id": ObjectId(v), "watched_at": datetime.utcnow(),
          **({"not_interested": True} if v in not_interested else {})} for v in ids])


@pytest.mark.parametrize("scenario", ["for-you", "trending"])
async def test_HV1_for_you_and_trending_fill_up_for_someone_who_watched_everything(client, db, rb, scenario):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    uid, h = await make_user(db)
    await _watched(db, uid, ids, not_interested=ids[:3])
    r = await _feed(client, h, scenario=scenario, limit=8)
    assert r.status_code == 200, r.text
    got = [v["id"] for v in r.json()]
    assert len(got) >= 4, got
    assert not set(got) & set(ids[:3])          # Not interested never comes back


async def test_HV2_unseen_posts_still_come_first(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    uid, h = await make_user(db)
    unseen = ids[10:12]
    await _watched(db, uid, [v for v in ids if v not in unseen])
    got = [v["id"] for v in (await _feed(client, h, limit=8)).json()]
    assert set(unseen) <= set(got)
    assert len(got) >= 4


async def test_HV3_discover_pages_are_never_empty_for_a_heavy_viewer(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    uid, h = await make_user(db)
    await _watched(db, uid, ids, not_interested=ids[:2])
    for skip in (0, 4, 8):
        r = await client.get("/api/v1/feed/", params={"limit": 4, "skip": skip, "exclude_watched": "true",
                                                      "exclude_following": "true", "privacy": "public"}, headers=h)
        assert r.status_code == 200, r.text
        got = [v["id"] for v in r.json()]
        assert len(got) >= 2, (skip, got)
        assert not set(got) & set(ids[:2])


async def test_HV4_light_viewer_still_never_sees_watched_posts_while_unseen_ones_remain(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    uid, h = await make_user(db)
    await _watched(db, uid, ids[:3])
    got = [v["id"] for v in (await _feed(client, h, limit=8)).json()]
    assert len(got) == 8 and not set(got) & set(ids[:3])
