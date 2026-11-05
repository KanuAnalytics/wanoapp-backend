"""Round 2: attacks on the New tab's cursor paging itself."""

import base64
import struct
import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_new_feed import _ids, _new, _spread

pytestmark = pytest.mark.asyncio


async def _walk(client, headers=None, pages=200, **params):
    seen, cursor, n = [], "start", 0
    while cursor and n < pages:
        ids, r = await _ids(client, headers, cursor=cursor, **params)
        seen += ids
        cursor = r.headers.get("X-Next-Cursor")
        n += 1
    return seen, n


async def test_M1_flood_of_60_never_duplicates_and_others_all_show(client, db):
    spammer, _ = await make_user(db)
    for i in range(60):
        await make_video(db, spammer, created_at=ago(seconds=i), isReadyToStream=True)
    others = await _spread(db, 25, minutes_apart=1, start_minutes=0)
    seen, _ = await _walk(client)
    assert len(seen) == len(set(seen))
    assert set(others) <= set(seen)


async def test_M2_identical_timestamps_walk_exactly_once(client, db):
    t = ago(minutes=5)
    made = []
    for _ in range(33):
        c, _ = await make_user(db)
        made.append(await make_video(db, c, created_at=t, isReadyToStream=True))
    seen, _ = await _walk(client)
    assert sorted(seen) == sorted(made)


async def test_M3_tampered_cursor_cannot_pull_private_or_blocked_posts(client, db):
    from app.services.new_feed import encode_cursor
    c, _ = await make_user(db)
    blocked, _ = await make_user(db)
    priv = await make_video(db, c, privacy="private", isReadyToStream=True)
    bl = await make_video(db, blocked, isReadyToStream=True)
    await _spread(db, 3)
    _, viewer = await make_user(db, blocked_users=[ObjectId(blocked)])
    forged = encode_cursor(datetime.utcnow() + timedelta(hours=1), ObjectId(), [ObjectId(priv), ObjectId(bl)])
    ids, _ = await _ids(client, viewer, cursor=forged)
    assert priv not in ids and bl not in ids


async def test_M4_carried_post_deleted_meanwhile(client, db):
    busy, _ = await make_user(db)
    vids = [await make_video(db, busy, created_at=ago(seconds=i), isReadyToStream=True) for i in range(5)]
    await _spread(db, 12, minutes_apart=1, start_minutes=1)
    ids, r = await _ids(client, None, cursor="start")
    await db.videos.update_many({"_id": {"$in": [ObjectId(v) for v in vids]}}, {"$set": {"is_active": False}})
    ids2, _ = await _ids(client, None, cursor=r.headers["X-Next-Cursor"])
    assert not set(vids) & set(ids2)


async def test_M5_everything_watched_does_not_trap_the_viewer(client, db):
    ids = await _spread(db, 400, minutes_apart=1)
    uid, viewer = await make_user(db)
    await db.watch_history.insert_many([{"user_id": ObjectId(uid), "video_id": ObjectId(v),
                                         "watched_at": datetime.utcnow()} for v in ids[:390]])
    seen, pages = await _walk(client, viewer, exclude_watched="true")
    assert seen == ids[390:], (len(seen), pages)
    assert pages < 40


async def test_M6_profile_grid_with_cursor_is_not_capped(client, db):
    c, _ = await make_user(db)
    mine = [await make_video(db, c, created_at=ago(minutes=i), isReadyToStream=True) for i in range(15)]
    ids, _ = await _ids(client, None, cursor="start", user_id=c)
    assert ids == mine[:10]


async def test_M7_cursor_with_other_sorts_or_saved_is_refused(client, db):
    _, h = await make_user(db)
    assert (await _new(client, None, cursor="start", sorted_by="views_count")).status_code == 400
    assert (await _new(client, h, cursor="start", saved="true")).status_code == 400


async def test_M8_featured_shared_video_first_with_cursor(client, db):
    await _spread(db, 12)
    c, _ = await make_user(db)
    shared = await make_video(db, c, created_at=ago(days=30), isReadyToStream=True)
    ids, _ = await _ids(client, None, cursor="start", video_id=shared)
    assert ids[0] == shared and len(ids) == 10


async def test_M9_cursor_is_compact_and_truncated_cursor_rejected(client, db):
    spammer, _ = await make_user(db)
    for i in range(80):
        await make_video(db, spammer, created_at=ago(seconds=i), isReadyToStream=True)
    await _spread(db, 10)
    _, r = await _ids(client, None, cursor="start")
    cur = r.headers["X-Next-Cursor"]
    assert len(cur) <= 512
    assert (await _new(client, None, cursor=cur[:-3])).status_code == 400


async def test_M10_ten_thousand_posts_page_fast(client, db):
    c = [ObjectId((await make_user(db))[0]) for _ in range(50)]
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": c[i % 50], "privacy": "public", "is_active": True,
                                  "isReadyToStream": True, "created_at": now - timedelta(seconds=i),
                                  "views_count": 0, "likes_count": 0, "remoteUrl_CF": "x"}
                                 for i in range(10000)])
    t = time.perf_counter()
    seen, pages = await _walk(client, pages=5)
    assert time.perf_counter() - t < 5 and len(seen) == 50 and len(set(seen)) == 50


async def test_M11_old_skip_app_unaffected_by_cap(client, db):
    spammer, _ = await make_user(db)
    made = [await make_video(db, spammer, created_at=ago(minutes=i), isReadyToStream=True) for i in range(12)]
    p1, _ = await _ids(client, None, skip=0)
    p2, _ = await _ids(client, None, skip=10)
    assert p1 + p2 == made     # old builds keep exact skip paging


async def test_M12_logged_in_skip_feed_hides_not_interested(client, db):
    ids = await _spread(db, 5)
    _, h = await make_user(db)
    await client.post(f"/api/v1/videos/{ids[1]}/not-interested", headers=h)
    page, r = await _ids(client, h, skip=0)
    assert ids[1] not in page and r.headers["X-Has-More"] == "false"
