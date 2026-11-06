"""Attacks on the interactive New tab: the "new videos" bubble and your own pinned posts."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_new_feed import _ids, _spread

pytestmark = pytest.mark.asyncio

COUNT = "/api/v1/feed/new-count"


async def _count(client, since, headers=None, **params):
    base = {} if headers else {"authenticated": "false"}
    q = "&".join(f"{k}={v}" for k, v in {"since": since, **base, **params}.items() if v is not None)
    return await client.get(f"{COUNT}?{q}", headers=headers or {})


async def _ok(client, since, headers=None, **params):
    r = await _count(client, since, headers, **params)
    assert r.status_code == 200, r.text
    return r.json(), r


# ---------------------------------------------------------------- the bubble

async def test_I1_counts_new_posts_and_shows_three_newest_creators(client, db):
    top = (await _spread(db, 5, start_minutes=30))[0]
    new_creators = []
    for i in range(4):
        c, _ = await make_user(db, username=f"newc{i}")
        new_creators.append(c)
        await make_video(db, c, created_at=ago(minutes=10 - i), isReadyToStream=True)
    body, _ = await _ok(client, top)
    assert body["count"] == 4 and body["more"] is False
    assert [c["id"] for c in body["creators"]] == new_creators[::-1][:3]


async def test_I2_only_counts_what_new_would_actually_show(client, db):
    top = (await _spread(db, 3, start_minutes=60))[0]
    me, mh = await make_user(db)
    blocked, _ = await make_user(db)
    blocker, _ = await make_user(db)
    gone, _ = await make_user(db, is_active=False)
    c, _ = await make_user(db)
    t = ago(minutes=5)
    await make_video(db, c, created_at=t, privacy="private", isReadyToStream=True)
    await make_video(db, c, created_at=t, privacy="unlisted", isReadyToStream=True)
    await make_video(db, c, created_at=t, isReadyToStream=False)                 # still processing
    await make_video(db, c, created_at=t, is_active=False, isReadyToStream=True)  # deleted
    await make_video(db, c, created_at=t, is_active=False, status="scheduled", isReadyToStream=True)
    await make_video(db, me, created_at=t, isReadyToStream=True)                 # my own
    await make_video(db, blocked, created_at=t, isReadyToStream=True)
    await make_video(db, blocker, created_at=t, isReadyToStream=True)
    await make_video(db, gone, created_at=t, isReadyToStream=True)
    meh = await make_video(db, c, created_at=t, isReadyToStream=True)
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"blocked_users": [ObjectId(blocked)],
                                                                "blocked_by": [ObjectId(blocker)]}})
    await client.post(f"/api/v1/videos/{meh}/not-interested", headers=mh)
    body, _ = await _ok(client, top, mh)
    assert body["count"] == 0, body
    real = await make_video(db, c, created_at=ago(minutes=1), isReadyToStream=True)
    body, _ = await _ok(client, top, mh)
    assert body["count"] == 1 and body["creators"][0]["id"] == c
    assert real


async def test_I3_count_is_capped_and_fast_with_a_flood(client, db):
    top = (await _spread(db, 1, start_minutes=600))[0]
    cs = [ObjectId((await make_user(db))[0]) for _ in range(40)]
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": cs[i % 40], "privacy": "public", "is_active": True,
                                  "isReadyToStream": True, "created_at": now - timedelta(seconds=i),
                                  "remoteUrl_CF": "x"} for i in range(5000)])
    t = time.perf_counter()
    body, _ = await _ok(client, top)
    assert time.perf_counter() - t < 2
    assert body["count"] == 99 and body["more"] is True and len(body["creators"]) == 3


async def test_I4_creator_payload_leaks_nothing_private(client, db):
    top = (await _spread(db, 1, start_minutes=60))[0]
    c, _ = await make_user(db, email="secret@x.com", phone_number="+256700000000", password_hash="hash")
    await make_video(db, c, created_at=ago(minutes=1), isReadyToStream=True)
    await make_video(db, c, created_at=ago(minutes=2), isReadyToStream=True)
    body, r = await _ok(client, top)
    assert len(body["creators"]) == 1
    assert set(body["creators"][0]) <= {"id", "username", "display_name", "profile_picture"}
    assert "secret@x.com" not in r.text and "+256700000000" not in r.text and "hash" not in r.text


@pytest.mark.parametrize("since", ["garbage", "", "0" * 25, "zzzzzzzzzzzzzzzzzzzzzzzz", "%24where"])
async def test_I5_bad_since_is_rejected(client, db, since):
    r = await _count(client, since)
    assert r.status_code in (400, 422), (since, r.status_code)


async def test_I5_missing_since_is_rejected(client, db):
    r = await client.get(f"{COUNT}?authenticated=false")
    assert r.status_code == 422


async def test_I6_private_video_id_cannot_be_used_to_probe_its_date(client, db):
    await _spread(db, 3, start_minutes=30)            # public posts 30-40 min ago
    c, _ = await make_user(db)
    secret = ObjectId()                               # id minted now...
    await db.videos.insert_one({"_id": secret, "creator_id": ObjectId(c), "privacy": "private",
                                "is_active": True, "isReadyToStream": True,
                                "created_at": ago(hours=5)})   # ...but dated 5 h ago
    body, _ = await _ok(client, str(secret))
    assert body["count"] == 0, "the private post's own date was used"


async def test_I6_unknown_or_future_ids_are_safe(client, db):
    await _spread(db, 3, start_minutes=30)
    old = ObjectId.from_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    future = ObjectId.from_datetime(datetime.now(timezone.utc) + timedelta(days=3))
    assert (await _ok(client, str(old)))[0]["count"] == 3
    assert (await _ok(client, str(future)))[0]["count"] == 0


async def test_I7_bubble_count_matches_what_refresh_shows(client, db):
    _, viewer = await make_user(db)
    page, _ = await _ids(client, viewer, cursor="start")
    await _spread(db, 12, start_minutes=60)
    page, _ = await _ids(client, viewer, cursor="start")
    news = await _spread(db, 3, minutes_apart=1, start_minutes=1)
    body, _ = await _ok(client, page[0], viewer)
    fresh, _ = await _ids(client, viewer, cursor="start")
    assert body["count"] == 3 and fresh[:3] == news and fresh[3:] == page[:7]


async def test_I8_caught_up_still_says_when_the_last_post_was(client, db):
    top = (await _spread(db, 3, start_minutes=180))[0]
    body, r = await _ok(client, top)
    assert body["count"] == 0 and body["creators"] == []
    last = datetime.fromisoformat(body["last_post_at"].replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=170) < datetime.now(timezone.utc) - last < timedelta(minutes=190)
    assert body["poll_after_seconds"] >= 30
    assert "private" in r.headers.get("cache-control", "")


async def test_I9_scheduled_post_that_goes_live_is_counted(client, db):
    from app.services.scheduled_publisher import publish_post
    top = (await _spread(db, 2, start_minutes=30))[0]
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(days=2), is_active=False, status="scheduled",
                           scheduled_at=ago(minutes=1).replace(tzinfo=None), isReadyToStream=True)
    assert await publish_post(ObjectId(vid))
    assert (await _ok(client, top))[0]["count"] == 1


async def test_I10_polling_storm_is_cheap_and_writes_nothing(client, db):
    top = (await _spread(db, 5, start_minutes=30))[0]
    _, h = await make_user(db)
    before = {n: await db[n].count_documents({}) for n in ("videos", "watch_history", "feed_impressions",
                                                            "video_stats_hourly", "view_dedupe")}
    t = time.perf_counter()
    for i in range(300):
        r = await _count(client, top, h if i % 2 else None)
        assert r.status_code == 200
    assert time.perf_counter() - t < 15
    after = {n: await db[n].count_documents({}) for n in before}
    assert before == after


async def test_I11_same_timestamp_ties_are_counted_correctly(client, db):
    t = ago(minutes=10)
    ids = []
    for _ in range(5):
        c, _ = await make_user(db)
        ids.append(await make_video(db, c, created_at=t, isReadyToStream=True))
    ids.sort()
    body, _ = await _ok(client, ids[2])
    assert body["count"] == 2


# ---------------------------------------------------------------- your own posts, pinned

async def test_P1_own_recent_posts_are_pinned_with_status(client, db):
    await _spread(db, 15, start_minutes=1)
    me, h = await make_user(db)
    live = await make_video(db, me, created_at=ago(hours=2), isReadyToStream=True, views_count=12)
    processing = await make_video(db, me, created_at=ago(minutes=3), isReadyToStream=False,
                                  remoteUrl_CF=None, remoteUrl=None)
    await make_video(db, me, created_at=ago(days=2), isReadyToStream=True)            # too old
    await make_video(db, me, created_at=ago(hours=1), privacy="private", isReadyToStream=True)
    await make_video(db, me, created_at=ago(hours=1), is_active=False, isReadyToStream=True)
    await make_video(db, me, created_at=ago(hours=1), is_active=False, status="scheduled")
    r = await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)
    assert r.status_code == 200, r.text
    page = r.json()
    assert [v["id"] for v in page[:2]] == [processing, live]
    assert page[0]["pinned"] and page[0]["status"] == "processing"
    assert page[1]["pinned"] and page[1]["status"] == "live" and page[1]["views_count"] == 12
    assert not any(v["pinned"] for v in page[2:]) and len(page) == 10


async def test_P1_at_most_three_pinned(client, db):
    await _spread(db, 10)
    me, h = await make_user(db)
    mine = [await make_video(db, me, created_at=ago(minutes=i + 1), isReadyToStream=True) for i in range(6)]
    page = (await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)).json()
    assert [v["id"] for v in page if v["pinned"]] == mine[:3]


async def test_P2_old_app_without_flag_is_unchanged(client, db):
    await _spread(db, 5)
    me, h = await make_user(db)
    mine = await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    for q in ("skip=0", "cursor=start"):
        page = (await client.get(f"/api/v1/feed/?{q}&limit=10", headers=h)).json()
        assert mine not in [v["id"] for v in page]


async def test_P3_logged_out_include_own_is_ignored(client, db):
    await _spread(db, 5)
    r = await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true&authenticated=false")
    assert r.status_code == 200 and not any(v.get("pinned") for v in r.json())


async def test_P4_pinned_only_on_the_first_page(client, db):
    await _spread(db, 30)
    me, h = await make_user(db)
    mine = await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    r1 = await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)
    r2 = await client.get(f"/api/v1/feed/?cursor={r1.headers['X-Next-Cursor']}&limit=10&include_own=true", headers=h)
    r3 = await client.get("/api/v1/feed/?skip=10&limit=10&include_own=true", headers=h)
    assert mine in [v["id"] for v in r1.json()]
    assert mine not in [v["id"] for v in r2.json()] and mine not in [v["id"] for v in r3.json()]
    ids = [v["id"] for v in r1.json()] + [v["id"] for v in r2.json()]
    assert len(ids) == len(set(ids))


async def test_P5_shared_video_stays_first_and_limit_holds(client, db):
    await _spread(db, 20)
    me, h = await make_user(db)
    await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    c, _ = await make_user(db)
    shared = await make_video(db, c, created_at=ago(days=9), isReadyToStream=True)
    page = (await client.get(f"/api/v1/feed/?cursor=start&limit=10&include_own=true&video_id={shared}",
                             headers=h)).json()
    assert page[0]["id"] == shared and page[1]["pinned"] and len(page) == 10


async def test_P6_nobody_else_sees_my_processing_post(client, db):
    await _spread(db, 5)
    me, _ = await make_user(db)
    proc = await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=False)
    _, other = await make_user(db)
    for h, extra in ((other, "include_own=true"), (None, "authenticated=false&include_own=true")):
        r = await client.get(f"/api/v1/feed/?cursor=start&limit=10&{extra}", headers=h or {})
        assert proc not in [v["id"] for v in r.json()]
    body, _ = await _ok(client, str(ObjectId.from_datetime(datetime.now(timezone.utc) - timedelta(hours=1))))
    assert proc not in [c["id"] for c in body["creators"]]


async def test_P7_profile_and_saved_ignore_include_own(client, db):
    me, h = await make_user(db)
    c, _ = await make_user(db)
    theirs = [await make_video(db, c, created_at=ago(minutes=i), isReadyToStream=True) for i in range(3)]
    await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=True)
    page = (await client.get(f"/api/v1/feed/?skip=0&limit=10&include_own=true&user_id={c}", headers=h)).json()
    assert [v["id"] for v in page] == theirs


async def test_P8_odd_own_post_does_not_break_the_page(client, db):
    await _spread(db, 5)
    me, h = await make_user(db)
    await make_video(db, me, created_at=ago(minutes=1), isReadyToStream=False, views_count="x",
                     title=None, remoteUrl_CF=None, images=None, urls=None)
    r = await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=h)
    assert r.status_code == 200, r.text
