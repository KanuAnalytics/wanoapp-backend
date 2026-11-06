"""Round 2: deeper attacks on the bubble and pinned posts."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_new_feed import _ids, _spread
from tests.pressure.test_new_interactive import _ok

pytestmark = pytest.mark.asyncio


async def _walk(client, h, limit=10, **extra):
    q = "&".join(f"{k}={v}" for k, v in extra.items())
    seen, cursor = [], "start"
    for _ in range(50):
        r = await client.get(f"/api/v1/feed/?cursor={cursor}&limit={limit}&{q}", headers=h)
        assert r.status_code == 200, r.text
        seen += [v["id"] for v in r.json() if not v.get("pinned")]
        cursor = r.headers.get("X-Next-Cursor")
        if not cursor:
            break
    return seen


async def test_Q1_small_page_with_pins_never_loses_a_post(client, db):
    made = await _spread(db, 12)
    me, h = await make_user(db)
    for i in range(3):
        await make_video(db, me, created_at=ago(minutes=i), isReadyToStream=True)
    seen = await _walk(client, h, limit=2, include_own="true")
    assert seen == made, (len(seen), len(made))


async def test_Q2_shared_video_and_pins_never_lose_a_post(client, db):
    made = await _spread(db, 25)
    me, h = await make_user(db)
    await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    seen = await _walk(client, h, limit=10, include_own="true", video_id=made[17])
    assert sorted(set(seen) | {made[17]}) == sorted(made) and len(seen) == len(set(seen))


async def test_Q3_shared_video_is_my_own_post_no_duplicate(client, db):
    await _spread(db, 10)
    me, h = await make_user(db)
    mine = await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    page = (await client.get(f"/api/v1/feed/?cursor=start&limit=10&include_own=true&video_id={mine}",
                             headers=h)).json()
    ids = [v["id"] for v in page]
    assert ids[0] == mine and ids.count(mine) == 1


async def test_Q4_future_dated_post_cannot_squat_the_top(client, db):
    made = await _spread(db, 5)
    c, _ = await make_user(db)
    squat = await make_video(db, c, created_at=datetime.now(timezone.utc) + timedelta(days=30), isReadyToStream=True)
    ids, _ = await _ids(client, None, cursor="start")
    assert squat not in ids and ids[:5] == made
    body, _ = await _ok(client, made[0])
    assert body["count"] == 0


async def test_Q5_everything_new_is_not_interested(client, db):
    top = (await _spread(db, 2, start_minutes=60))[0]
    _, h = await make_user(db)
    news = await _spread(db, 4, start_minutes=1)
    for v in news:
        await client.post(f"/api/v1/videos/{v}/not-interested", headers=h)
    body, _ = await _ok(client, top, h)
    assert body["count"] == 0 and body["creators"] == []


async def test_Q6_huge_block_list(client, db):
    top = (await _spread(db, 1, start_minutes=60))[0]
    blocked = [ObjectId() for _ in range(20000)]
    _, h = await make_user(db, blocked_users=blocked)
    await _spread(db, 3, start_minutes=1)
    body, _ = await _ok(client, top, h)
    assert body["count"] == 3


async def test_Q7_logged_out_without_flag_follows_app_contract(client, db):
    top = (await _spread(db, 1))[0]
    r = await client.get(f"/api/v1/feed/new-count?since={top}")
    assert r.status_code == 401
    r = await client.get(f"/api/v1/feed/new-count?since={top}", headers={"Authorization": "Bearer junk"})
    assert r.status_code in (401, 403)


async def test_Q8_pins_respect_blocking_of_nobody_and_show_my_live_counts(client, db):
    me, h = await make_user(db)
    vid = await make_video(db, me, created_at=ago(minutes=5), isReadyToStream=True)
    for _ in range(3):
        _, other = await make_user(db)
        await client.post(f"/api/v1/videos/{vid}/watch", headers=other, json={"portion": 1})
    page = (await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)).json()
    pin = next(v for v in page if v["id"] == vid)
    assert pin["status"] == "live" and pin["views_count"] + pin["buffered_views"] == 3


async def test_Q9_processing_becomes_live_on_webhook(client, db):
    me, h = await make_user(db)
    vid = await make_video(db, me, created_at=ago(minutes=2), isReadyToStream=False)
    p1 = (await client.get("/api/v1/feed/?cursor=start&limit=5&include_own=true", headers=h)).json()
    assert p1[0]["status"] == "processing"
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"isReadyToStream": True}})
    p2 = (await client.get("/api/v1/feed/?cursor=start&limit=5&include_own=true", headers=h)).json()
    assert p2[0]["status"] == "live"


async def test_Q10_bubble_after_refresh_resets_to_zero(client, db):
    await _spread(db, 5, start_minutes=30)
    _, h = await make_user(db)
    await _spread(db, 2, start_minutes=1)
    page, _ = await _ids(client, h, cursor="start")
    body, _ = await _ok(client, page[0], h)
    assert body["count"] == 0


async def test_Q11_count_ignores_pinned_and_own(client, db):
    me, h = await make_user(db)
    top = (await _spread(db, 3, start_minutes=30))[0]
    await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    body, _ = await _ok(client, top, h)
    assert body["count"] == 0
    page = (await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)).json()
    first_not_pinned = next(v["id"] for v in page if not v["pinned"])
    assert first_not_pinned == top
