"""Scheduled posts never come back from the dead: a post scheduled by someone
who then deletes their account or is switched off is cancelled, not published."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def _scheduled(db, uid):
    return await make_video(db, uid, status="scheduled", is_active=False,
                            scheduled_at=datetime.utcnow() - timedelta(minutes=1))


async def test_SS1_deleted_account_scheduled_post_never_goes_live(client, db):
    from app.services.scheduled_publisher import publish_due_posts
    uid, h = await make_user(db)
    vid = await _scheduled(db, uid)
    r = await client.delete("/api/v1/users/delete", headers=h)
    assert r.status_code in (200, 204), r.text
    await publish_due_posts()
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["is_active"] is False and v["status"] == "cancelled"


async def test_SS2_switched_off_or_banned_creator_too_and_others_still_publish(client, db):
    from app.services.scheduled_publisher import publish_due_posts
    off, _ = await make_user(db)
    banned, _ = await make_user(db)
    fine, _ = await make_user(db)
    v1, v2, v3 = await _scheduled(db, off), await _scheduled(db, banned), await _scheduled(db, fine)
    await db.users.update_one({"_id": ObjectId(off)}, {"$set": {"is_active": False}})
    await db.users.update_one({"_id": ObjectId(banned)}, {"$set": {"is_banned": True}})
    await publish_due_posts()
    got = {str(v["_id"]): (v["status"], v["is_active"]) async for v in db.videos.find({})}
    assert got[v1] == ("cancelled", False) and got[v2] == ("cancelled", False)
    assert got[v3] == ("published", True)


async def test_SS3_posting_limits_stop_a_flood_not_a_creator(client, db):
    from app.services import post_limits
    uid, h = await make_user(db)
    body = {"media_type": "photo", "images": ["https://wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg"],
            "description": "hi", "privacy": "public"}
    for _ in range(post_limits.POSTS_PER_HOUR):
        await make_video(db, uid, created_at=datetime.utcnow())
    r = await client.post("/api/v1/videos/post", json=body, headers=h)
    assert r.status_code != 429                                  # switch off: as today
    await db.config.insert_one({"features": {"post_limits": True}})
    r = await client.post("/api/v1/videos/post", json=body, headers=h)
    assert r.status_code == 429 and "posted a lot" in r.json()["detail"]
    r = await client.post("/api/v1/videos/stitch", json={"clips": []}, headers=h)
    assert r.status_code in (429, 422)
    # an hour later the hourly limit is clear, the daily one still counts
    await db.videos.update_many({"creator_id": ObjectId(uid)}, {"$set": {"created_at": datetime.utcnow() - timedelta(hours=2)}})
    r = await client.post("/api/v1/videos/post", json=body, headers=h)
    assert r.status_code != 429, r.text
    for _ in range(post_limits.POSTS_PER_DAY):
        await make_video(db, uid, created_at=datetime.utcnow() - timedelta(hours=3))
    assert (await client.post("/api/v1/videos/post", json=body, headers=h)).status_code == 429
    admin, ah = await make_user(db, user_type="admin")
    for _ in range(post_limits.POSTS_PER_HOUR + 1):
        await make_video(db, admin, created_at=datetime.utcnow())
    assert (await client.post("/api/v1/videos/post", json=body, headers=ah)).status_code != 429
