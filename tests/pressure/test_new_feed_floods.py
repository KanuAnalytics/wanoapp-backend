"""One creator posting many videos must not take over the New tab, in the store
builds too (skip paging): their posts after the first DAILY_CAP of a day are
left out of New, pages stay full, and a creator isn't back to back when it can
be helped. The new app's cursor paging keeps its own per-page cap."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import new_feed
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

NEW = "/api/v1/feed/"


@pytest.fixture(autouse=True)
def _fresh_caches():
    new_feed._flood_cache = (None, None, [])
    new_feed._inactive_cache = (None, [])
    yield
    new_feed._flood_cache = (None, None, [])
    new_feed._inactive_cache = (None, [])


def _today(h, m=0):
    d = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    t = d + timedelta(hours=h, minutes=m)
    return t if t <= datetime.utcnow() else t - timedelta(days=1)


async def _page(client, headers=None, **params):
    base = {"limit": 10} if headers else {"limit": 10, "authenticated": "false"}
    q = "&".join(f"{k}={v}" for k, v in {**base, **params}.items())
    r = await client.get(f"{NEW}?{q}", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _flooded(db, spam_count=8, others=20):
    """A spammer's burst a few minutes ago on top of others spread over the past hours."""
    spammer, _ = await make_user(db)
    now = datetime.utcnow()
    spam = [await make_video(db, spammer, created_at=now - timedelta(minutes=1, seconds=i * 10), isReadyToStream=True)
            for i in range(spam_count)]
    other = []
    for i in range(others):
        c, _ = await make_user(db)
        other.append(await make_video(db, c, created_at=now - timedelta(minutes=5 + i * 3), isReadyToStream=True))
    return str(spammer), spam, other


async def test_FL1_store_build_shows_at_most_3_of_a_burst_and_pages_stay_full(client, db):
    spammer, spam, other = await _flooded(db)
    p1 = await _page(client, skip=0)
    p2 = await _page(client, skip=10)
    assert len(p1) == 10 and len(p2) == 10, "a short page ends the store build's feed"
    shown = [v for v in p1 + p2 if v["creator_id"] == spammer]
    assert len(shown) == new_feed.DAILY_CAP
    # the first 3 of the day are the ones kept (oldest of the burst), so new uploads never shift pages
    assert {v["id"] for v in shown} == set(spam[-3:])
    ids = [v["id"] for v in p1 + p2]
    assert len(ids) == len(set(ids))


async def test_FL2_no_back_to_back_when_others_can_go_between(client, db):
    spammer, spam, other = await _flooded(db)
    p1 = await _page(client, skip=0)
    creators = [v["creator_id"] for v in p1]
    assert all(a != b for a, b in zip(creators, creators[1:])), creators


async def test_FL3_a_fourth_post_does_not_shift_pages(client, db):
    spammer, spam, other = await _flooded(db, spam_count=3)
    before = [v["id"] for v in await _page(client, skip=0)] + [v["id"] for v in await _page(client, skip=10)]
    await make_video(db, ObjectId(spammer), created_at=datetime.utcnow(), isReadyToStream=True)
    new_feed._flood_cache = (None, None, [])
    after = [v["id"] for v in await _page(client, skip=0)] + [v["id"] for v in await _page(client, skip=10)]
    assert sorted(before) == sorted(after)


async def test_FL4_normal_creators_untouched(client, db):
    made = []
    for i in range(15):
        c, _ = await make_user(db)
        made.append(await make_video(db, c, created_at=datetime.utcnow() - timedelta(minutes=i), isReadyToStream=True))
    p1 = await _page(client, skip=0)
    assert [v["id"] for v in p1] == made[:10]          # newest first, nothing reordered


async def test_FL5_three_a_day_is_fine_and_days_are_separate(client, db):
    creator, _ = await make_user(db)
    y = datetime.utcnow() - timedelta(days=1)
    made = [await make_video(db, creator, created_at=y.replace(hour=10, minute=i), isReadyToStream=True) for i in range(3)]
    made += [await make_video(db, creator, created_at=datetime.utcnow() - timedelta(minutes=i + 1), isReadyToStream=True)
             for i in range(3)]
    for i in range(10):
        c, _ = await make_user(db)
        await make_video(db, c, created_at=datetime.utcnow() - timedelta(hours=2, minutes=i), isReadyToStream=True)
    seen = [v["id"] for v in await _page(client, skip=0, limit=20)]
    assert set(made) <= set(seen)


async def test_FL6_profile_grid_shows_everything(client, db):
    spammer, spam, _ = await _flooded(db)
    grid = [v["id"] for v in await _page(client, user_id=spammer, limit=20)]
    assert sorted(grid) == sorted(spam)


async def test_FL7_new_app_cursor_keeps_its_own_cap(client, db):
    spammer, spam, other = await _flooded(db)
    r = await client.get(f"{NEW}?limit=10&authenticated=false&cursor=start")
    page = r.json()
    assert sum(v["creator_id"] == spammer for v in page) <= new_feed.MAX_PER_CREATOR


async def test_FL8_not_interested_and_floods_combine(client, db):
    spammer, spam, other = await _flooded(db)
    viewer_id, viewer = await make_user(db)
    await db.watch_history.insert_one({"user_id": ObjectId(viewer_id), "video_id": ObjectId(other[0]),
                                       "not_interested": True, "watched_at": datetime.utcnow()})
    p1 = await _page(client, viewer, skip=0)
    ids = [v["id"] for v in p1]
    assert other[0] not in ids and len(ids) == 10
    assert sum(v["creator_id"] == spammer for v in p1) <= new_feed.DAILY_CAP


async def test_FL9_busy_day_stays_fast(client, db):
    import time
    from datetime import timezone
    c = [ObjectId((await make_user(db))[0]) for _ in range(30)]
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": c[i % 30], "privacy": "public", "is_active": True,
                                  "isReadyToStream": True, "created_at": now - timedelta(seconds=20 * i),
                                  "views_count": 0, "likes_count": 0, "remoteUrl_CF": "x"}
                                 for i in range(3000)])
    t = time.perf_counter()
    p1 = await _page(client, skip=0, limit=25)
    first = time.perf_counter() - t
    t = time.perf_counter()
    await _page(client, skip=25, limit=25)          # cached exclusions
    second = time.perf_counter() - t
    assert len(p1) == 25 and first < 8 and second < first
