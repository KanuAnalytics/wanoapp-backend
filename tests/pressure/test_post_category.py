"""Picking the Wano points category while posting (instead of only later from
the post's menu). Attacks written before the code."""

import secrets

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_engagement import _post
from tests.pressure.test_replies_hashtags import _stitch_body, stream_not_ready  # noqa: F401

pytestmark = pytest.mark.asyncio
CF = "https://videodelivery.net/{}/manifest/video.m3u8"


async def _doc(db, vid):
    return await db.videos.find_one({"_id": ObjectId(vid)})


async def test_PC1_direct_video_post_saves_the_category(client, db):
    _, h = await make_user(db)
    r = await _post(client, h, points_category="music")
    assert r.status_code == 201, r.text
    assert (await _doc(db, r.json()["video_id"]))["category"] == "music"


async def test_PC2_photo_post_saves_the_category(client, db):
    _, h = await make_user(db)
    r = await client.post("/api/v1/videos/post", headers=h, json={
        "media_type": "photo", "images": ["https://wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg"],
        "description": "x", "points_category": "food_culture"})
    assert r.status_code == 201, r.text
    assert (await _doc(db, r.json()["video_id"]))["category"] == "food_culture"


async def test_PC3_no_category_means_no_field_like_before(client, db):
    _, h = await make_user(db)
    r = await _post(client, h)
    assert "category" not in await _doc(db, r.json()["video_id"])
    r = await _post(client, h, points_category=None)
    assert r.status_code == 201 and "category" not in await _doc(db, r.json()["video_id"])


async def test_PC4_unknown_or_hostile_category_is_refused(client, db):
    _, h = await make_user(db)
    for bad in ["money", "", "MUSIC", {"$ne": 1}, ["music"], 5]:
        assert (await _post(client, h, points_category=bad)).status_code == 422, bad
    assert await db.videos.count_documents({}) == 0


async def test_PC5_edited_multi_clip_post_keeps_the_category(client, db, stream_not_ready):
    from app.services import stitch_jobs
    _, h = await make_user(db)
    r = await client.post("/api/v1/videos/stitch", headers=h, json=_stitch_body(points_category="comedy"))
    assert r.status_code == 202, r.text
    job = await db.pending_videos.find_one({"_id": ObjectId(r.json()["job_id"])})
    vid = await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    assert (await _doc(db, vid))["category"] == "comedy"
    r = await client.post("/api/v1/videos/stitch", headers=h, json=_stitch_body(points_category="hack"))
    assert r.status_code == 422


async def test_PC6_a_tampered_job_cant_slip_in_a_bad_category(client, db, stream_not_ready):
    from app.services import stitch_jobs
    _, h = await make_user(db)
    r = await client.post("/api/v1/videos/stitch", headers=h, json=_stitch_body())
    job = await db.pending_videos.find_one({"_id": ObjectId(r.json()["job_id"])})
    job["post"]["points_category"] = {"$set": "x"}
    vid = await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    assert "category" not in await _doc(db, vid)


async def test_PC7_category_set_at_posting_counts_for_points_like_one_set_later(client, db):
    from app.services import points
    uid, h = await make_user(db)
    a = (await _post(client, h, points_category="sports")).json()["video_id"]
    b = (await _post(client, h)).json()["video_id"]
    assert (await client.put(f"/api/v1/points/videos/{b}/category", json={"category": "sports"},
                             headers=h)).status_code == 200
    da, dbb = await _doc(db, a), await _doc(db, b)
    assert da["category"] == dbb["category"] == "sports"
