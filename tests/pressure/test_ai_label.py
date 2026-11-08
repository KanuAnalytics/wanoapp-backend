"""AI disclosure: creators mark a post "Made with AI", the feed carries the
label, admins can add it to a post that didn't disclose, and people can report
undisclosed AI. Behind the "ai_label" switch. Attacks written with the code."""

import secrets

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_engagement import _post
from tests.pressure.test_replies_hashtags import _stitch_body, stream_not_ready  # noqa: F401

pytestmark = pytest.mark.asyncio
CF = "https://videodelivery.net/{}/manifest/video.m3u8"


async def _on(db, on=True):
    await db.config.insert_one({"features": {"ai_label": on}})


async def _feed(client, **params):
    r = await client.get("/api/v1/feed/", params={"authenticated": "false", "limit": 20, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_AI1_switch_off_nothing_is_saved_or_shown(client, db):
    _, h = await make_user(db)
    vid = (await _post(client, h, ai_generated=True, isReadyToStream=True)).json()["video_id"]
    assert "ai_generated" not in await db.videos.find_one({"_id": ObjectId(vid)})
    assert (await client.get("/api/v1/config/features")).json()["ai_label"] is False
    assert (await _feed(client))[0].get("ai_generated") is False


async def test_AI2_creator_discloses_and_the_feed_shows_it(client, db):
    await _on(db)
    _, h = await make_user(db)
    a = (await _post(client, h, ai_generated=True, isReadyToStream=True)).json()["video_id"]
    b = (await _post(client, h, isReadyToStream=True)).json()["video_id"]
    assert (await db.videos.find_one({"_id": ObjectId(a)}))["ai_generated"] is True
    assert "ai_generated" not in await db.videos.find_one({"_id": ObjectId(b)})
    by_id = {v["id"]: v for v in await _feed(client)}
    assert by_id[a]["ai_generated"] is True and by_id[b]["ai_generated"] is False


async def test_AI3_photo_and_edited_multi_clip_posts_keep_the_label(client, db, stream_not_ready):
    await _on(db)
    from app.services import stitch_jobs
    _, h = await make_user(db)
    p = await client.post("/api/v1/videos/post", headers=h, json={
        "media_type": "photo", "images": ["https://wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg"],
        "description": "x", "ai_generated": True})
    assert (await db.videos.find_one({"_id": ObjectId(p.json()["video_id"])}))["ai_generated"] is True
    r = await client.post("/api/v1/videos/stitch", headers=h, json=_stitch_body(ai_generated=True))
    job = await db.pending_videos.find_one({"_id": ObjectId(r.json()["job_id"])})
    vid = await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["ai_generated"] is True


async def test_AI4_hostile_values_are_refused_or_ignored(client, db, stream_not_ready):
    await _on(db)
    from app.services import stitch_jobs
    _, h = await make_user(db)
    for bad in ["yes", 1, {"$ne": None}, []]:
        assert (await _post(client, h, ai_generated=bad)).status_code == 422, bad
    r = await client.post("/api/v1/videos/stitch", headers=h, json=_stitch_body())
    job = await db.pending_videos.find_one({"_id": ObjectId(r.json()["job_id"])})
    job["post"]["ai_generated"] = {"$set": 1}            # a tampered job
    vid = await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    assert "ai_generated" not in await db.videos.find_one({"_id": ObjectId(vid)})


async def test_AI5_admin_labels_a_post_that_didnt_disclose(client, db):
    await _on(db)
    c, ch = await make_user(db, username="maker")
    vid = (await _post(client, ch, isReadyToStream=True)).json()["video_id"]
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/admin/videos/{vid}/ai-label", json={"on": True}, headers=ah)
    assert r.status_code == 200, r.text
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["ai_generated"] is True and v["ai_label_by_admin"] is True
    assert (await _feed(client))[0]["ai_generated"] is True
    assert await db.admin_log.count_documents({"action": "ai_label_on"}) == 1
    # taking it off again
    assert (await client.post(f"/api/v1/admin/videos/{vid}/ai-label", json={"on": False}, headers=ah)).status_code == 200
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v.get("ai_generated") is not True and v.get("ai_label_by_admin") is not True
    assert (await client.post(f"/api/v1/admin/videos/{ObjectId()}/ai-label", json={"on": True}, headers=ah)).status_code == 404
    assert (await client.post(f"/api/v1/admin/videos/junk/ai-label", json={"on": True}, headers=ah)).status_code == 404
    assert (await client.post(f"/api/v1/admin/videos/{vid}/ai-label", json={"on": True}, headers=ch)).status_code == 403


async def test_AI6_creator_can_add_the_label_later_but_not_remove_an_admin_one(client, db):
    await _on(db)
    c, ch = await make_user(db)
    other, oh = await make_user(db)
    vid = (await _post(client, ch, isReadyToStream=True)).json()["video_id"]
    assert (await client.put(f"/api/v1/videos/{vid}/ai-label", json={"on": True}, headers=oh)).status_code == 403
    assert (await client.put(f"/api/v1/videos/{vid}/ai-label", json={"on": True}, headers=ch)).status_code == 200
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["ai_generated"] is True
    assert (await client.put(f"/api/v1/videos/{vid}/ai-label", json={"on": False}, headers=ch)).status_code == 200
    _, ah = await make_user(db, user_type="admin")
    await client.post(f"/api/v1/admin/videos/{vid}/ai-label", json={"on": True}, headers=ah)
    r = await client.put(f"/api/v1/videos/{vid}/ai-label", json={"on": False}, headers=ch)
    assert r.status_code == 403 and "Wano" in r.json()["detail"]
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["ai_generated"] is True


async def test_AI7_report_undisclosed_ai(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    _, rh = await make_user(db)
    r = await client.post(f"/api/v1/reports/videos/{vid}/report",
                          json={"category": "undisclosed_ai", "comment": "Looks AI generated"}, headers=rh)
    assert r.status_code in (200, 201), r.text
    assert await db.reports.count_documents({"category": "undisclosed_ai"}) == 1


async def test_AI8_switch_off_later_keeps_old_labels_but_hides_them(client, db):
    await _on(db)
    _, h = await make_user(db)
    vid = (await _post(client, h, ai_generated=True, isReadyToStream=True)).json()["video_id"]
    await db.config.update_many({}, {"$set": {"features.ai_label": False}})
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["ai_generated"] is True
    assert (await _feed(client))[0]["ai_generated"] is False
    assert (await client.put(f"/api/v1/videos/{vid}/ai-label", json={"on": True}, headers=h)).status_code == 404
