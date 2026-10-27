"""Video deletion (DELETE /videos/{id}) and banning (DELETE /users/ban/{id}).

Both go through app.services.video_deletion.soft_delete_video. These tests run
without MongoDB, Cloudflare, Spaces or Recombee: all of those are faked, and
every delete call is recorded instead of performed."""

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.v1 import users as users_api
from app.api.v1 import videos as videos_api
from app.services import upload_DO, video_deletion
from app.services.upload_DO import (
    DELETED_VIDEO_PLACEHOLDER_UID,
    DELETED_VIDEO_PLACEHOLDER_URL,
)
from app.services.video_posts import DEFAULT_THUMBNAIL

CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"


def _field(doc, field):
    if field == "urls.thumbnail":
        return (doc.get("urls") or {}).get("thumbnail")
    return doc.get(field)


def _matches_reference(doc, query):
    """Evaluate the {"$or": [...], "_id": {"$ne": x}} reference queries."""
    if "_id" in query and doc["_id"] == query["_id"]["$ne"]:
        return False
    for cond in query["$or"]:
        (field, url), = cond.items()
        value = _field(doc, field)
        if value == url or (isinstance(value, list) and url in value):
            return True
    return False


class FakeCollection:
    def __init__(self, docs):
        self.docs = {d["_id"]: d for d in docs}
        self.updates = []

    async def find_one(self, query, projection=None):
        if "$or" in query:
            return next((d for d in self.docs.values() if _matches_reference(d, query)), None)
        return self.docs.get(query["_id"])

    def find(self, query):
        creator = query["creator_id"]

        def selected(d):
            # The ban's query: active videos, plus scheduled posts.
            active = d.get("is_active") is not False
            if "$or" not in query:
                return active
            statuses = query["$or"][1]["status"]["$in"]
            return active or d.get("status") in statuses

        hits = [d for d in self.docs.values() if d.get("creator_id") == creator and selected(d)]

        async def gen():
            for d in hits:
                yield d

        return gen()

    async def update_one(self, query, update):
        self.updates.append((query, update))
        doc = self.docs.get(query["_id"])
        status_filter = query.get("status")
        if status_filter is not None and (doc is None or doc.get("status") not in status_filter["$in"]):
            doc = None
        if doc is not None and "$set" in update:
            doc.update(update["$set"])
        return UpdateResult(1 if doc is not None else 0)


class UpdateResult:
    def __init__(self, modified_count):
        self.modified_count = modified_count


class FakeDB:
    def __init__(self, videos=(), users=()):
        self.videos = FakeCollection(list(videos))
        self.users = FakeCollection(list(users))


@pytest.fixture
def calls(monkeypatch):
    rec = {"stream": [], "spaces": [], "recombee": []}
    monkeypatch.setattr(video_deletion, "delete_stream_video", lambda uid: rec["stream"].append(uid))
    monkeypatch.setattr(video_deletion, "delete_from_spaces", lambda url: rec["spaces"].append(url))

    async def fake_recombee(req):
        rec["recombee"].append(req)

    monkeypatch.setattr(video_deletion, "recombee_send", fake_recombee)
    return rec


def _user(**overrides):
    doc = {"_id": ObjectId(), "user_type": "standard", "is_active": True,
           "profile_picture": f"{CDN}/profile-pictures/{ObjectId()}.jpg"}
    doc.update(overrides)
    return doc


def _video(creator, **overrides):
    uid = str(ObjectId())
    doc = {
        "_id": ObjectId(),
        "creator_id": creator["_id"],
        "is_active": True,
        "remoteUrl": f"https://videodelivery.net/{uid}/manifest/video.m3u8",
        "remoteUrl_CF": f"https://videodelivery.net/{uid}/manifest/video.m3u8",
        "urls": {"thumbnail": f"{CDN}/profile-pictures/thumb-{uid}.jpg"},
    }
    doc.update(overrides)
    return doc


def _uid(video):
    return upload_DO.extract_stream_uid(video["remoteUrl_CF"])


async def _delete(db, video, requester, monkeypatch):
    monkeypatch.setattr(videos_api, "get_database", lambda: db)
    return await videos_api.delete_video(str(video["_id"]), current_user=str(requester["_id"]))


# ---- DELETE /videos/{id} ---------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_id_is_400(monkeypatch, calls):
    monkeypatch.setattr(videos_api, "get_database", lambda: FakeDB())
    with pytest.raises(HTTPException) as exc:
        await videos_api.delete_video("not-an-id", current_user=str(ObjectId()))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_repeat_delete_is_a_no_op(monkeypatch, calls):
    creator = _user()
    video = _video(creator, is_active=False,
                   remoteUrl=DELETED_VIDEO_PLACEHOLDER_URL,
                   remoteUrl_CF=DELETED_VIDEO_PLACEHOLDER_URL)
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    assert calls == {"stream": [], "spaces": [], "recombee": []}
    assert db.videos.updates == [] and db.users.updates == []


@pytest.mark.asyncio
async def test_stream_video_delete_removes_stream_thumbnail_and_recombee(monkeypatch, calls):
    creator = _user()
    video = _video(creator)
    stream, thumb = _uid(video), video["urls"]["thumbnail"]
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    assert calls["stream"] == [stream]
    assert thumb in calls["spaces"]
    assert len(calls["recombee"]) == 1
    assert db.videos.docs[video["_id"]]["remoteUrl_CF"] == DELETED_VIDEO_PLACEHOLDER_URL
    assert db.videos.docs[video["_id"]]["is_active"] is False
    assert ({"_id": creator["_id"]}, {"$inc": {"videos_count": -1}}) in db.users.updates


@pytest.mark.asyncio
async def test_legacy_spaces_original_is_deleted(monkeypatch, calls):
    creator = _user()
    legacy = f"{CDN}/videos/original.mp4"
    video = _video(creator, remoteUrl=legacy)
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    assert legacy in calls["spaces"]


@pytest.mark.asyncio
async def test_photo_post_images_deleted_once_each(monkeypatch, calls):
    creator = _user()
    imgs = [f"{CDN}/profile-pictures/p1.jpg", f"{CDN}/profile-pictures/p2.jpg"]
    video = _video(creator, media_type="photo", remoteUrl=None, remoteUrl_CF=None,
                   images=imgs, urls={"thumbnail": imgs[0]})
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    assert sorted(calls["spaces"]) == sorted(imgs)
    assert calls["stream"] == []


@pytest.mark.asyncio
async def test_shared_and_default_files_are_kept(monkeypatch, calls):
    creator = _user()
    shared = f"{CDN}/profile-pictures/shared.jpg"
    default_thumb = _video(creator, urls={"thumbnail": DEFAULT_THUMBNAIL})
    shared_a = _video(creator, urls={"thumbnail": shared})
    shared_b = _video(creator, urls={"thumbnail": shared})
    # A thumbnail that happens to be someone's profile picture.
    other_user = _user()
    avatar_thumb = _video(creator, urls={"thumbnail": other_user["profile_picture"]})
    db = FakeDB([default_thumb, shared_a, shared_b, avatar_thumb], [creator, other_user])

    for v in (default_thumb, shared_a, avatar_thumb):
        await _delete(db, v, creator, monkeypatch)

    assert DEFAULT_THUMBNAIL not in calls["spaces"]
    assert shared not in calls["spaces"]
    assert other_user["profile_picture"] not in calls["spaces"]


@pytest.mark.asyncio
async def test_scheduled_post_is_cancelled_and_its_media_deleted(monkeypatch, calls):
    creator = _user()
    video = _video(creator, is_active=False, status="scheduled")
    stream = _uid(video)
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    doc = db.videos.docs[video["_id"]]
    assert doc["status"] == "cancelled" and doc["is_active"] is False
    assert calls["stream"] == [stream]
    # It never went live, so it was never counted on the creator.
    assert db.users.updates == []


@pytest.mark.asyncio
async def test_post_published_since_it_was_read_is_deleted_as_live(monkeypatch, calls):
    creator = _user()
    video = _video(creator, is_active=True, status="published")
    db = FakeDB([video], [creator])
    stale = {**video, "is_active": False, "status": "scheduled"}
    await video_deletion.soft_delete_video(db, stale)

    doc = db.videos.docs[video["_id"]]
    assert doc["status"] == "published" and doc["is_active"] is False
    assert ({"_id": creator["_id"]}, {"$inc": {"videos_count": -1}}) in db.users.updates


@pytest.mark.asyncio
async def test_cancelled_scheduled_post_is_a_no_op(monkeypatch, calls):
    creator = _user()
    video = _video(creator, is_active=False, status="cancelled",
                   remoteUrl=DELETED_VIDEO_PLACEHOLDER_URL,
                   remoteUrl_CF=DELETED_VIDEO_PLACEHOLDER_URL)
    db = FakeDB([video], [creator])
    await _delete(db, video, creator, monkeypatch)

    assert calls == {"stream": [], "spaces": [], "recombee": []}
    assert db.users.updates == []


# ---- DELETE /users/ban/{id} --------------------------------------------------

async def _ban(db, target, admin, monkeypatch):
    monkeypatch.setattr(users_api, "get_database", lambda: db)
    return await users_api.ban_user(str(target["_id"]), current_user=str(admin["_id"]))


@pytest.mark.asyncio
async def test_ban_deletes_videos_photos_and_profile_picture(monkeypatch, calls):
    admin = _user(user_type="admin")
    target = _user(cover_picture=f"{CDN}/profile-pictures/cover.jpg")
    v1, v2 = _video(target), _video(target)
    photo = _video(target, media_type="photo", remoteUrl=None, remoteUrl_CF=None,
                   images=[f"{CDN}/profile-pictures/ph.jpg"],
                   urls={"thumbnail": f"{CDN}/profile-pictures/ph.jpg"})
    already_deleted = _video(target, is_active=False,
                             remoteUrl=DELETED_VIDEO_PLACEHOLDER_URL,
                             remoteUrl_CF=DELETED_VIDEO_PLACEHOLDER_URL)
    someone_elses = _video(admin)
    db = FakeDB([v1, v2, photo, already_deleted, someone_elses], [admin, target])
    profile_picture = target["profile_picture"]
    streams = sorted([_uid(v1), _uid(v2)])   # before the ban swaps in the placeholder

    await _ban(db, target, admin, monkeypatch)

    assert sorted(calls["stream"]) == streams
    assert v1["urls"]["thumbnail"] in calls["spaces"]
    assert f"{CDN}/profile-pictures/ph.jpg" in calls["spaces"]
    assert profile_picture in calls["spaces"]
    assert f"{CDN}/profile-pictures/cover.jpg" in calls["spaces"]
    assert len(calls["recombee"]) == 3          # v1, v2, photo
    for v in (v1, v2, photo):
        assert db.videos.docs[v["_id"]]["is_active"] is False
    assert db.videos.docs[someone_elses["_id"]]["is_active"] is True
    assert DELETED_VIDEO_PLACEHOLDER_UID not in calls["stream"]
    t = db.users.docs[target["_id"]]
    assert t["is_active"] is False and t["profile_picture"] is None


@pytest.mark.asyncio
async def test_ban_cancels_scheduled_posts(monkeypatch, calls):
    admin = _user(user_type="admin")
    target = _user()
    scheduled = _video(target, is_active=False, status="scheduled")
    db = FakeDB([scheduled], [admin, target])

    await _ban(db, target, admin, monkeypatch)

    assert db.videos.docs[scheduled["_id"]]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_ban_keeps_shared_cover_and_handles_missing_pictures(monkeypatch, calls):
    admin = _user(user_type="admin")
    shared_cover = f"{CDN}/profile-pictures/shared-cover.jpg"
    target = _user(profile_picture=None, cover_picture=shared_cover)
    other = _user(cover_picture=shared_cover)
    db = FakeDB([], [admin, target, other])

    await _ban(db, target, admin, monkeypatch)

    assert calls["spaces"] == []


@pytest.mark.asyncio
async def test_ban_requires_admin(monkeypatch, calls):
    target, not_admin = _user(), _user()
    db = FakeDB([_video(target)], [target, not_admin])
    with pytest.raises(HTTPException) as exc:
        await _ban(db, target, not_admin, monkeypatch)
    assert exc.value.status_code == 403
    assert calls == {"stream": [], "spaces": [], "recombee": []}


# ---- storage helpers ---------------------------------------------------------

def test_stream_helper_refuses_to_delete_placeholder(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call Cloudflare for the placeholder")

    monkeypatch.setattr(upload_DO.requests, "delete", boom)
    upload_DO.delete_stream_video(DELETED_VIDEO_PLACEHOLDER_UID)
    upload_DO.delete_stream_video("")


def test_spaces_helper_accepts_cdn_and_origin_urls(monkeypatch):
    deleted = []

    class FakeS3:
        def delete_object(self, Bucket, Key):
            deleted.append(Key)

    monkeypatch.setattr(upload_DO.boto3, "client", lambda *a, **k: FakeS3())
    s = upload_DO.settings
    upload_DO.delete_from_spaces(f"{s.DO_SPACES_CDN_URL}/{s.DO_SPACES_BUCKET_NAME}/profile-pictures/a.jpg")
    upload_DO.delete_from_spaces(f"{s.DO_SPACES_ENDPOINT}/{s.DO_SPACES_BUCKET_NAME}/profile-pictures/b.jpg")
    upload_DO.delete_from_spaces("https://videodelivery.net/abc/manifest/video.m3u8")
    upload_DO.delete_from_spaces(None)
    assert deleted == ["profile-pictures/a.jpg", "profile-pictures/b.jpg"]


def test_placeholder_url_uses_new_stream():
    assert DELETED_VIDEO_PLACEHOLDER_UID == "97002814c3643d7bba41a9311db4e610"
    assert upload_DO.extract_stream_uid(DELETED_VIDEO_PLACEHOLDER_URL) == DELETED_VIDEO_PLACEHOLDER_UID
