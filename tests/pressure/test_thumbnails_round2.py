"""Round 2: webhook replays, scheduled posts, scale."""

import time
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_thumbnails import CF, UID, _placeholder

pytestmark = pytest.mark.asyncio


async def test_TR1_replayed_ready_event_does_not_override_a_later_custom_thumbnail(client, db, rb):
    from app.routes import upload_video
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=False, stream_uid=UID, urls={"thumbnail": _placeholder()},
                           remoteUrl_CF=CF)
    await upload_video.mark_stream_ready(UID)
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"urls.thumbnail": "https://cdn.example/mine.jpg"}})
    assert await upload_video.mark_stream_ready(UID) is None          # already ready: no-op
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["urls"]["thumbnail"] == "https://cdn.example/mine.jpg"


async def test_TR2_scheduled_post_gets_its_frame_but_no_announcement(client, db, rb):
    from app.routes import upload_video
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=False, stream_uid=UID, urls={"thumbnail": None},
                           remoteUrl_CF=CF, status="scheduled", is_active=False)
    await upload_video.mark_stream_ready(UID)
    d = await db.videos.find_one({"_id": ObjectId(vid)})
    assert UID in d["urls"]["thumbnail"]
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_ready"}) == 0


async def test_TR3_ready_still_announces_and_notifies_once(client, db, rb):
    from app.routes import upload_video
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=False, stream_uid=UID, remoteUrl_CF=CF)
    for _ in range(3):
        await upload_video.mark_stream_ready(UID)
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_ready"}) == 1


async def test_TR4_backfill_in_batches(client, db, rb):
    from app.services import thumbnails
    c, _ = await make_user(db)
    await db.videos.insert_many([{"creator_id": ObjectId(c), "is_active": True, "urls": {"thumbnail": _placeholder()},
                                  "remoteUrl_CF": f"https://videodelivery.net/{'%032x' % i}/manifest/video.m3u8"}
                                 for i in range(3000)])
    t = time.perf_counter()
    r = await thumbnails.backfill(db, apply=True)
    assert r["fixed"] == 3000 and r["errors"] == 0 and time.perf_counter() - t < 30
