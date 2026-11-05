"""The store builds page New by skip and stop at the first page shorter than
`limit` (they ignore X-Has-More). Posts hidden after the page is cut would
end their feed early, so skip pages must stay full."""

import pytest
from bson import ObjectId

from app.services import new_feed
from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago

pytestmark = pytest.mark.asyncio

NEW = "/api/v1/feed/"


@pytest.fixture(autouse=True)
def _fresh_inactive_cache():
    new_feed._inactive_cache = (None, [])
    yield
    new_feed._inactive_cache = (None, [])


async def _spread(db, n):
    ids = []
    for i in range(n):
        c, _ = await make_user(db)
        ids.append(await make_video(db, c, created_at=ago(minutes=1 + i * 5), isReadyToStream=True))
    return ids


async def _page(client, headers=None, **params):
    base = {"limit": 10} if headers else {"limit": 10, "authenticated": "false"}
    q = "&".join(f"{k}={v}" for k, v in {**base, **params}.items())
    r = await client.get(f"{NEW}?{q}", headers=headers or {})
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()]


async def test_S1_deactivated_creator_does_not_shorten_a_skip_page(client, db):
    made = await _spread(db, 25)
    gone = (await db.videos.find_one({"_id": ObjectId(made[3])}))["creator_id"]
    await db.users.update_one({"_id": gone}, {"$set": {"is_active": False}})

    page1 = await _page(client, skip=0)
    page2 = await _page(client, skip=10)
    assert len(page1) == 10 and len(page2) == 10, "a short page ends the store build's feed"
    assert made[3] not in page1 + page2
    assert page1 + page2 == [v for v in made if v != made[3]][:20]


async def test_S1_not_interested_does_not_shorten_a_skip_page(client, db):
    made = await _spread(db, 25)
    viewer_id, viewer = await make_user(db)
    await db.watch_history.insert_one({
        "user_id": ObjectId(viewer_id), "video_id": ObjectId(made[2]),
        "not_interested": True, "watched_at": ago(minutes=0),
    })

    page1 = await _page(client, viewer, skip=0)
    assert len(page1) == 10, "a short page ends the store build's feed"
    assert made[2] not in page1


async def test_S1_profile_grid_is_unchanged(client, db):
    creator, _ = await make_user(db)
    made = [await make_video(db, creator, created_at=ago(minutes=i), isReadyToStream=True) for i in range(5)]
    assert await _page(client, user_id=str(creator)) == made
