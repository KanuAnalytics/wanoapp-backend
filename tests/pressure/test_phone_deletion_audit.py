"""Actual routes with mocked storage; no live video mutations."""
import asyncio
import pytest
from bson import ObjectId
from app.services.video_deletion import soft_delete_video
from tests.pressure.conftest import make_user, make_video
pytestmark = pytest.mark.asyncio
UID = "049497c556913ec2ffdb46c2678f68e5"

async def test_legacy_upload_lookup(client, db):
    _, h = await make_user(db, user_type="admin")
    owner, _ = await make_user(db, username="uploader")
    await db.upload_attempts.insert_one({"_id": UID, "user": ObjectId(owner), "platform": "android"})
    r = await client.get(f"/api/v1/admin/video-lookup?stream_uid={UID}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["accounts"][0]["id"] == owner
    assert r.json()["accounts"][0]["matched_by"] == ["upload_history"]
    assert "email" not in r.text and "phone_number" not in r.text

async def test_permissions_and_input(client, db):
    _, h = await make_user(db)
    assert (await client.get(f"/api/v1/admin/video-lookup?stream_uid={UID}", headers=h)).status_code == 403
    _, h = await make_user(db, user_type="admin")
    assert (await client.get("/api/v1/admin/video-lookup?stream_uid=.*", headers=h)).status_code == 422

async def test_phone_audit_and_repeat(client, db):
    admin, h = await make_user(db, user_type="admin")
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, stream_uid=UID)
    r = await client.delete(f"/api/v1/videos/{vid}", headers=h)
    assert r.status_code == 204, r.text
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert doc["deletion"]["stream_uid"] == UID
    assert doc["deletion"]["by"] == ObjectId(admin)
    assert doc["deletion"]["reason"] == "not_supplied"
    assert doc["deletion"]["source"] == "phone_admin"
    assert doc["is_active"] is False
    found = await client.get(f"/api/v1/admin/video-lookup?stream_uid={UID}", headers=h)
    assert found.json()["accounts"][0]["id"] == owner
    assert found.json()["posts"][0]["deleted_by"] == admin
    assert (await client.delete(f"/api/v1/videos/{vid}", headers=h)).status_code == 204
    assert await db.admin_log.count_documents({"action": "phone_post_delete"}) == 1

async def test_reason_and_creator(client, db):
    owner, h = await make_user(db)
    vid = await make_video(db, owner, stream_uid=UID)
    assert (await client.delete(f"/api/v1/videos/{vid}?reason=made_up", headers=h)).status_code == 422
    assert (await client.delete(f"/api/v1/videos/{vid}?reason=nudity", headers=h)).status_code == 204
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert doc["deletion"]["reason"] == "nudity"
    assert doc["deletion"]["source"] == "creator"

async def test_concurrent_delete_once(db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, stream_uid=UID)
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    results = await asyncio.gather(*(soft_delete_video(db, dict(doc), actor_id=owner) for _ in range(10)))
    assert results.count(True) == 1
    assert await db.admin_log.count_documents({"action": "phone_post_delete"}) == 1
