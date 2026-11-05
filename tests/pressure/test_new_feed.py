"""Attacks on the New tab: GET /api/v1/feed/ (newest first)."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio

NEW = "/api/v1/feed/"


async def _new(client, headers=None, **params):
    base = {"limit": 10} if headers else {"limit": 10, "authenticated": "false"}
    q = "&".join(f"{k}={v}" for k, v in {**base, **params}.items())
    return await client.get(f"{NEW}?{q}", headers=headers or {})


async def _ids(client, headers=None, **params):
    r = await _new(client, headers, **params)
    assert r.status_code == 200, r.text
    return [v["id"] for v in r.json()], r


async def _spread(db, n, minutes_apart=5, start_minutes=1):
    """n videos from n different creators, newest first."""
    ids = []
    for i in range(n):
        c, _ = await make_user(db)
        ids.append(await make_video(db, c, created_at=ago(minutes=start_minutes + i * minutes_apart),
                                    isReadyToStream=True))
    return ids


# ---------------------------------------------------------------- paging

async def test_N1_new_uploads_do_not_repeat_videos_on_the_next_page(client, db):
    await _spread(db, 30)
    _, viewer = await make_user(db)
    page1, r1 = await _ids(client, viewer, cursor="start")
    cursor = r1.headers.get("X-Next-Cursor")
    assert cursor, "no cursor: the app can only page by skip, which shifts when new videos arrive"
    await _spread(db, 4, start_minutes=0)           # 4 uploads land while the user scrolls
    page2, _ = await _ids(client, viewer, cursor=cursor)
    assert not set(page1) & set(page2), "page 2 repeats page 1"
    assert len(page2) == 10


async def test_N1_cursor_walks_the_whole_feed_exactly_once(client, db):
    made = await _spread(db, 37)
    seen, cursor = [], "start"
    for _ in range(10):
        ids, r = await _ids(client, None, cursor=cursor)
        seen += ids
        cursor = r.headers.get("X-Next-Cursor")
        if not cursor:
            break
    assert seen == made, (len(seen), len(set(seen)))


async def test_N1_old_app_skip_paging_still_works(client, db):
    made = await _spread(db, 25)
    p1, r = await _ids(client, None, skip=0)
    p2, _ = await _ids(client, None, skip=10)
    assert p1 + p2 == made[:20] and r.headers["X-Has-More"] == "true"


@pytest.mark.parametrize("bad", ["garbage", "1.2.3", "9999999999999999999999.aaaaaaaaaaaaaaaaaaaaaaaa",
                                 "-5.000000000000000000000000", "%00", "1700000000000." + "z" * 24])
async def test_N2_hostile_cursor_is_rejected_cleanly(client, db, bad):
    await _spread(db, 3)
    r = await _new(client, None, cursor=bad)
    assert r.status_code in (400, 422), (bad, r.status_code, r.text)


async def test_N2_hostile_skip_and_sort(client, db):
    await _spread(db, 3)
    for params in ({"skip": 10 ** 19}, {"sorted_by": "$where"}, {"sorted_by": "password_hash"}, {"limit": 0}):
        r = await _new(client, None, **params)
        assert r.status_code in (200, 422), (params, r.status_code)


# ---------------------------------------------------------------- what shows up

async def test_N3_one_creator_cannot_take_over_new(client, db):
    spammer, _ = await make_user(db)
    for i in range(30):
        await make_video(db, spammer, created_at=ago(minutes=i), isReadyToStream=True)
    others = await _spread(db, 8, minutes_apart=3, start_minutes=2)
    ids, _ = await _ids(client, None, limit=10, cursor="start")   # the updated app
    docs = {str(d["_id"]): str(d["creator_id"]) async for d in db.videos.find({})}
    creators = [docs[i] for i in ids]
    assert creators.count(spammer) <= 2, f"spammer has {creators.count(spammer)} of 10 slots"
    assert all(a != b for a, b in zip(creators, creators[1:])), "same creator back to back"
    assert len(set(ids) & set(others)) >= 7


async def test_N3_creator_cap_does_not_hide_a_creator_forever(client, db):
    busy, _ = await make_user(db)
    busy_ids = [await make_video(db, busy, created_at=ago(minutes=i), isReadyToStream=True) for i in range(6)]
    await _spread(db, 30, minutes_apart=2, start_minutes=7)
    seen, cursor = [], "start"
    for _ in range(8):
        ids, r = await _ids(client, None, cursor=cursor)
        seen += ids
        cursor = r.headers.get("X-Next-Cursor")
        if not cursor:
            break
    assert len(seen) == len(set(seen))
    assert set(busy_ids) <= set(seen), "capped videos never came back on later pages"


async def test_N4_not_interested_stays_out_of_new(client, db):
    ids = await _spread(db, 12)
    _, viewer = await make_user(db)
    r = await client.post(f"/api/v1/videos/{ids[0]}/not-interested", headers=viewer)
    assert r.status_code == 200, r.text
    page, _ = await _ids(client, viewer)
    assert ids[0] not in page


async def test_N5_exclude_watched_still_fills_the_page(client, db):
    ids = await _spread(db, 40)
    _, viewer = await make_user(db)
    for v in ids[:15]:
        await client.post(f"/api/v1/videos/{v}/watch", headers=viewer, json={"portion": 1})
    page, r = await _ids(client, viewer, exclude_watched="true", cursor="start")
    assert page == ids[15:25], page
    assert r.headers.get("X-Next-Cursor")


async def test_N6_broken_and_processing_videos_never_show(client, db):
    c, _ = await make_user(db)
    good = await make_video(db, c, created_at=ago(minutes=9), isReadyToStream=True)
    legacy_ok = await make_video(db, c, created_at=ago(minutes=8))            # old post, playable link
    broken = await make_video(db, c, created_at=ago(minutes=1), remoteUrl_CF="", remoteUrl=None)
    processing = await make_video(db, c, created_at=ago(minutes=2), isReadyToStream=False)
    photo = await make_video(db, c, created_at=ago(minutes=3), media_type="photo", remoteUrl_CF=None,
                             images=["https://cdn.example/p.jpg"])
    ids, _ = await _ids(client, None)
    assert broken not in ids and processing not in ids
    assert {good, legacy_ok, photo} <= set(ids)


async def test_N7_scheduled_post_shows_at_top_when_it_goes_live(client, db):
    from app.services.scheduled_publisher import publish_post
    await _spread(db, 5)
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(days=3), is_active=False, status="scheduled",
                           scheduled_at=ago(minutes=1).replace(tzinfo=None), isReadyToStream=True)
    assert await publish_post(ObjectId(vid))
    ids, _ = await _ids(client, None)
    assert ids[0] == vid


async def test_N8_private_unlisted_and_blocked_stay_out(client, db):
    c, _ = await make_user(db)
    blocked, _ = await make_user(db)
    blocker, _ = await make_user(db)
    priv = await make_video(db, c, privacy="private", isReadyToStream=True)
    unl = await make_video(db, c, privacy="unlisted", isReadyToStream=True)
    b1 = await make_video(db, blocked, isReadyToStream=True)
    b2 = await make_video(db, blocker, isReadyToStream=True)
    ok = await make_video(db, c, isReadyToStream=True)
    _, viewer = await make_user(db, blocked_users=[ObjectId(blocked)], blocked_by=[ObjectId(blocker)])
    ids, _ = await _ids(client, viewer)
    assert ok in ids and not {priv, unl, b1, b2} & set(ids)


async def test_N9_one_bad_document_does_not_break_the_page(client, db):
    c, _ = await make_user(db)
    await _spread(db, 5)
    await make_video(db, c, created_at=ago(minutes=0), isReadyToStream=True,
                     views_count="lots", likes_count=None, comments_count=-3, title=12345, images="x")
    r = await _new(client, None)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 6


async def test_N10_inactive_creator_hidden(client, db):
    gone, _ = await make_user(db, is_active=False)
    vid = await make_video(db, gone, isReadyToStream=True)
    await _spread(db, 3)
    ids, _ = await _ids(client, None)
    assert vid not in ids


async def test_N11_web_client_can_read_the_cursor_header():
    import inspect
    from app import main
    assert "X-Next-Cursor" in inspect.getsource(main)
