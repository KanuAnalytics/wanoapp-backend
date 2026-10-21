"""DELETE /api/v1/videos/{id}: idempotent, never deletes the placeholder, and
cleans up legacy Spaces files and thumbnails without touching shared ones.
Runs without MongoDB, Cloudflare or Spaces: all of those are faked."""

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.v1 import videos as videos_api
from app.services import upload_DO
from app.services.upload_DO import (
    DELETED_VIDEO_PLACEHOLDER_UID,
    DELETED_VIDEO_PLACEHOLDER_URL,
)
from app.services.video_posts import DEFAULT_THUMBNAIL

CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"
CREATOR_ID = ObjectId()


class FakeVideos:
    def __init__(self, docs):
        self.docs = {d["_id"]: d for d in docs}
        self.updates = []

    async def find_one(self, query, projection=None):
        if set(query) == {"_id"}:
            return self.docs.get(query["_id"])
        # Shared-file check: {"_id": {"$ne": oid}, field: url}
        exclude = query["_id"]["$ne"]
        (field, url), = [(k, v) for k, v in query.items() if k != "_id"]
        for oid, d in self.docs.items():
            if oid == exclude:
                continue
            value = (d.get("urls") or {}).get("thumbnail") if field == "urls.thumbnail" else d.get(field)
            if value == url:
                return {"_id": oid}
        return None

    async def update_one(self, query, update):
        self.updates.append((query, update))
        self.docs[query["_id"]].update(update["$set"])


class FakeUsers:
    def __init__(self):
        self.incs = []

    async def find_one(self, query, projection=None):
        return {"_id": query["_id"], "user_type": "standard"}

    async def update_one(self, query, update):
        self.incs.append((query, update))


class FakeDB:
    def __init__(self, videos):
        self.videos = FakeVideos(videos)
        self.users = FakeUsers()


@pytest.fixture
def calls(monkeypatch):
    rec = {"stream": [], "spaces": [], "recombee": 0}
    monkeypatch.setattr(videos_api, "delete_stream_video", lambda uid: rec["stream"].append(uid))
    monkeypatch.setattr(videos_api, "delete_from_spaces", lambda url: rec["spaces"].append(url))

    async def fake_recombee(req):
        rec["recombee"] += 1

    monkeypatch.setattr(videos_api, "recombee_send", fake_recombee)
    return rec


def _video(**overrides):
    doc = {
        "_id": ObjectId(),
        "creator_id": CREATOR_ID,
        "is_active": True,
        "remoteUrl": "https://videodelivery.net/aaaa1111/manifest/video.m3u8",
        "remoteUrl_CF": "https://videodelivery.net/aaaa1111/manifest/video.m3u8",
        "urls": {"thumbnail": f"{CDN}/thumbnails/own.jpg"},
    }
    doc.update(overrides)
    return doc


async def _delete(db, video, monkeypatch):
    monkeypatch.setattr(videos_api, "get_database", lambda: db)
    return await videos_api.delete_video(str(video["_id"]), current_user=str(CREATOR_ID))


@pytest.mark.asyncio
async def test_invalid_id_is_400(monkeypatch, calls):
    monkeypatch.setattr(videos_api, "get_database", lambda: FakeDB([]))
    with pytest.raises(HTTPException) as exc:
        await videos_api.delete_video("not-an-id", current_user=str(CREATOR_ID))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_repeat_delete_is_a_no_op(monkeypatch, calls):
    video = _video(
        is_active=False,
        remoteUrl=DELETED_VIDEO_PLACEHOLDER_URL,
        remoteUrl_CF=DELETED_VIDEO_PLACEHOLDER_URL,
    )
    db = FakeDB([video])
    await _delete(db, video, monkeypatch)

    assert calls["stream"] == []          # placeholder not deleted
    assert calls["spaces"] == []
    assert db.users.incs == []            # video count not lowered again
    assert db.videos.updates == []


@pytest.mark.asyncio
async def test_stream_video_delete(monkeypatch, calls):
    video = _video()
    db = FakeDB([video])
    await _delete(db, video, monkeypatch)

    assert calls["stream"] == ["aaaa1111"]
    assert db.videos.docs[video["_id"]]["remoteUrl_CF"] == DELETED_VIDEO_PLACEHOLDER_URL
    assert db.videos.docs[video["_id"]]["is_active"] is False
    assert db.users.incs == [({"_id": CREATOR_ID}, {"$inc": {"videos_count": -1}})]
    # remoteUrl is a Stream link (ignored by the real helper) plus the thumbnail.
    assert f"{CDN}/thumbnails/own.jpg" in calls["spaces"]
    assert calls["recombee"] == 1


@pytest.mark.asyncio
async def test_legacy_spaces_original_is_deleted(monkeypatch, calls):
    legacy = f"{CDN}/videos/original.mp4"
    video = _video(remoteUrl=legacy)
    db = FakeDB([video])
    await _delete(db, video, monkeypatch)

    assert legacy in calls["spaces"]
    assert calls["stream"] == ["aaaa1111"]


@pytest.mark.asyncio
async def test_shared_and_default_thumbnails_are_kept(monkeypatch, calls):
    shared = f"{CDN}/thumbnails/shared.jpg"
    default_thumb = _video(urls={"thumbnail": DEFAULT_THUMBNAIL})
    shared_a = _video(urls={"thumbnail": shared})
    shared_b = _video(urls={"thumbnail": shared})
    db = FakeDB([default_thumb, shared_a, shared_b])

    await _delete(db, default_thumb, monkeypatch)
    await _delete(db, shared_a, monkeypatch)

    assert DEFAULT_THUMBNAIL not in calls["spaces"]
    assert shared not in calls["spaces"]


@pytest.mark.asyncio
async def test_photo_post_image_deleted_once(monkeypatch, calls):
    img = f"{CDN}/photos/p1.jpg"
    video = _video(remoteUrl=None, remoteUrl_CF=None, images=[img], urls={"thumbnail": img})
    db = FakeDB([video])
    await _delete(db, video, monkeypatch)

    assert calls["spaces"].count(img) == 1
    assert calls["stream"] == []


def test_stream_helper_refuses_to_delete_placeholder(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call Cloudflare for the placeholder")

    monkeypatch.setattr(upload_DO.requests, "delete", boom)
    upload_DO.delete_stream_video(DELETED_VIDEO_PLACEHOLDER_UID)
    upload_DO.delete_stream_video("")


def test_placeholder_url_uses_new_stream():
    assert DELETED_VIDEO_PLACEHOLDER_UID == "97002814c3643d7bba41a9311db4e610"
    assert upload_DO.extract_stream_uid(DELETED_VIDEO_PLACEHOLDER_URL) == DELETED_VIDEO_PLACEHOLDER_UID
