"""Round 3 before shipping: features working against each other.

A post held for review, or whose creator was banned, must vanish from every
surface at once: New, the bubble, For You (personal and first viewers),
trending, share page, sitemap, follower alerts. Turning notifications off must
silence every kind."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_growth_notifications import _sent

pytestmark = pytest.mark.asyncio


async def _everywhere(client, db, rb, vid, viewer_h):
    """Where the post shows up right now."""
    from app.services import creators, growth_notifications as g, trending
    trending.clear_cache()
    seen = set()
    new = (await client.get("/api/v1/feed/?cursor=start&limit=50", headers=viewer_h)).json()
    if vid in [v["id"] for v in new]:
        seen.add("new")
    old = str(ObjectId.from_datetime(datetime.now(timezone.utc) - timedelta(days=2)))
    bubble = (await client.get(f"/api/v1/feed/new-count?since={old}", headers=viewer_h)).json()
    if bubble.get("count", 0) and vid in [c["id"] for c in bubble.get("creators", [])] + [vid] and bubble["count"] > 3:
        seen.add("bubble")
    rb.pool = [vid] + rb.pool
    fy = (await _feed(client, viewer_h, limit=20)).json()
    if vid in [v["id"] for v in fy]:
        seen.add("for_you")
    if vid in [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]:
        seen.add("first_viewers")
    tr = (await client.get("/api/v1/feed/trending?limit=100")).json()
    if vid in [v["id"] for v in tr]:
        seen.add("trending")
    share = await client.get(f"/v/{vid}")
    if share.status_code == 200:
        seen.add("share_page")
    page1 = await client.get("/sitemaps/videos-1.xml")
    if vid in page1.text:
        seen.add("sitemap")
    return seen


async def _setup(db, client):
    _, ids = await _catalog(db, n=30, creators=6)
    creator, ch = await make_user(db, created_at=ago(days=60), display_name="Creator")
    fan, fh = await make_user(db, created_at=ago(days=60), localization={"country": "gh"},
                              expo_push_tokens=["fan-token"], following=[ObjectId(creator)])
    await db.users.update_one({"_id": ObjectId(creator)}, {"$set": {"followers": [ObjectId(fan)]}})
    for i in range(3):
        c, _ = await make_user(db)
        await make_video(db, c, created_at=ago(minutes=20 + i), isReadyToStream=True)
    vid = await make_video(db, creator, created_at=ago(minutes=10), isReadyToStream=True,
                           title="the post", description="the post")
    # Real engagement so it would trend.
    for _ in range(8):
        _, h = await make_user(db, created_at=ago(days=60))
        await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": 1})
        await client.post(f"/api/v1/videos/{vid}/like", headers=h)
    return ids, creator, ch, fan, fh, vid


async def test_X1_held_post_vanishes_everywhere_at_once(client, db, rb, calls):
    from app.services import growth_notifications as g, moderation
    ids, creator, ch, fan, fh, vid = await _setup(db, client)
    rb.pool = ids
    _, viewer = await make_user(db)
    before = await _everywhere(client, db, rb, vid, viewer)
    assert {"new", "for_you", "first_viewers", "trending", "share_page", "sitemap"} <= before, before
    # Three trusted accounts report it -> held.
    for _ in range(3):
        _, rh = await make_user(db, created_at=ago(days=30))
        r = await client.post(f"/api/v1/reports/videos/{vid}/report", headers=rh,
                              json={"category": "nudity"})
        assert r.status_code in (200, 201), r.text
    assert moderation.is_held(await db.videos.find_one({"_id": ObjectId(vid)}))
    after = await _everywhere(client, db, rb, vid, viewer)
    assert after == set(), f"held post still visible on: {after}"
    g.set_started(datetime.now(timezone.utc) - timedelta(days=1))
    await g.notify_new_posts(db)
    assert not [s for s in _sent(calls) if s[3].get("video_id") == vid]
    # The creator still sees it, marked under review.
    page = (await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=ch)).json()
    mine = [v for v in page if v["id"] == vid]
    assert mine and mine[0]["status"] == "under_review"


async def test_X2_banned_creator_vanishes_everywhere(client, db, rb, calls):
    from app.services import growth_notifications as g
    ids, creator, ch, fan, fh, vid = await _setup(db, client)
    rb.pool = ids
    _, admin = await make_user(db, user_type="admin")
    r = await client.delete(f"/api/v1/users/ban/{creator}", headers=admin)
    assert r.status_code in (200, 204), r.text
    _, viewer = await make_user(db)
    assert await _everywhere(client, db, rb, vid, viewer) == set()
    g.set_started(datetime.now(timezone.utc) - timedelta(days=1))
    await g.notify_new_posts(db)
    assert not _sent(calls)


async def test_X3_one_report_pauses_first_viewers_only(client, db, rb, calls):
    ids, creator, ch, fan, fh, vid = await _setup(db, client)
    rb.pool = ids
    _, rh = await make_user(db, created_at=ago(days=30))
    r = await client.post(f"/api/v1/reports/videos/{vid}/report", headers=rh, json={"category": "nudity"})
    assert r.status_code in (200, 201), r.text
    _, viewer = await make_user(db)
    seen = await _everywhere(client, db, rb, vid, viewer)
    assert "first_viewers" not in seen and {"new", "share_page"} <= seen, seen


async def test_X4_switching_tips_off_silences_every_creator_notification(client, db, rb, calls, monkeypatch):
    from app.services import creators, growth_notifications as g
    monday = datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc)        # 10:00 Kampala, Monday
    monkeypatch.setattr(g, "_utcnow", lambda: monday)
    c, h = await make_user(db, created_at=monday - timedelta(days=60), localization={"country": "ug"},
                           expo_push_tokens=["creator"])
    for d in (1, 2, 3, 4):
        await make_video(db, c, created_at=monday - timedelta(days=d), isReadyToStream=True)
    vid = await make_video(db, c, created_at=monday - timedelta(hours=1), isReadyToStream=True)
    await client.put("/api/v1/notifications/preferences", headers=h, json={"creator_tips": False})
    for _ in range(creators.STREAK_TEST_AUDIENCE):
        await creators.record_fy_watch(db, vid, 0.9)
    await g.posting_recaps(db)
    monkeypatch.setattr(g, "_utcnow", lambda: monday.replace(hour=17))   # 20:00 Kampala
    await g.streak_reminders(db)
    assert _sent(calls) == []


async def test_X5_late_webhook_after_a_failed_rescue_brings_the_post_back_quietly(client, db, rb, calls):
    """Marked failed (creator told), then Cloudflare's ready message finally lands."""
    from app.routes import upload_video
    from app.services import growth_notifications as g
    creator, _ = await make_user(db, created_at=ago(days=60), expo_push_tokens=["c"])
    fan, _ = await make_user(db, created_at=ago(days=60), localization={"country": "gh"}, expo_push_tokens=["f"])
    await db.users.update_one({"_id": ObjectId(creator)}, {"$set": {"followers": [ObjectId(fan)]}})
    uid = "a" * 32
    vid = await make_video(db, creator, created_at=ago(hours=30), isReadyToStream=False, stream_uid=uid,
                           remoteUrl_CF=f"https://videodelivery.net/{uid}/manifest/video.m3u8",
                           upload_check={"state": "failed", "reason": "timeout"})
    assert await upload_video.mark_stream_ready(uid)
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert doc["isReadyToStream"] is True and doc["upload_check"]["state"] == "rescued"
    g.set_started(datetime.now(timezone.utc) - timedelta(days=3))
    await g.notify_new_posts(db)
    # 30 h old: too old to announce to followers as "just posted".
    assert not [s for s in _sent(calls) if s[0] == "f"]


async def test_X6_tv_link_for_a_logged_out_viewer_cannot_open_another_title(client, db, rb):
    import time
    from app.services import tv_stream
    from tests.pressure.test_tv_protect import FakeSpaces, MASTER, _title
    a = await _title(db, "middleton")
    await _title(db, "one-field")
    token = tv_stream.sign(a, "anon", int(time.time()) + 100)
    r = await client.get(f"/api/v1/tv/stream/{token}/../one-field/master.m3u8")
    assert r.status_code in (400, 403, 404)
