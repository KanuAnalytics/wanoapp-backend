"""Counting posting attempts: each upload address handed out, whether a post
followed, and why a post was refused. Counting never changes or breaks a
request, keeps no captions, and only admins can read it."""

import secrets
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import upload_attempts as ua
from tests.pressure.conftest import make_user
from tests.pressure.test_replies_hashtags import _stitch_body, stream_not_ready  # noqa: F401

pytestmark = pytest.mark.asyncio
CF = "https://videodelivery.net/{}/manifest/video.m3u8"
ANDROID = "okhttp/4.12.0"
IPHONE = "Wano/2.3.41 CFNetwork/1568.200.51 Darwin/24.1.0"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ua._indexed["done"] = False
    import app.routes.upload_video as uv
    monkeypatch.setattr(uv, "generate_cf_tus_upload_url",
                        lambda **kw: {"upload_url": "https://upload.example/tus/x", "stream_uid": secrets.token_hex(16),
                                      "file_url": "x", "name": "n"})


async def _address(client, h=None, ua_header=ANDROID, size=20_000_000, folder=None):
    body = {"filename": "v.mp4", "fileSize": size, **({"folder": folder} if folder else {})}
    r = await client.post("/video/v2/presigned-upload", headers={**(h or {}), "User-Agent": ua_header}, json=body)
    assert r.status_code == 200, r.text
    return r.json()["data"]["stream_uid"]


async def _post(client, h, uid, **over):
    body = {"remoteUrl": f"https://x/{uid}.mp4", "remoteUrl_CF": CF.format(uid), "description": "secret caption",
            "isReadyToStream": False, **over}
    return await client.post("/api/v1/videos/post", headers=h, json=body)


async def test_UA1_address_noted_with_phone_type_size_and_who(client, db):
    uid_user, h = await make_user(db)
    a = await _address(client, h)
    b = await _address(client, None, IPHONE, 5_000_000)        # the editVideo Lambda: no login
    ra, rb = await db.upload_attempts.find_one({"_id": a}), await db.upload_attempts.find_one({"_id": b})
    assert ra["platform"] == "android" and ra["size"] == 20_000_000 and ra["user"] == ObjectId(uid_user)
    assert rb["platform"] == "ios" and rb["user"] is None and "posted_at" not in ra


async def test_UA2_a_post_marks_its_attempt(client, db):
    _, h = await make_user(db)
    uid = await _address(client, h)
    r = await _post(client, h, uid)
    assert r.status_code == 201, r.text
    assert (await db.upload_attempts.find_one({"_id": uid}))["posted_at"]


async def test_UA3_a_stitch_job_marks_its_clips(client, db, stream_not_ready):
    _, h = await make_user(db)
    uid = await _address(client, h, folder="clips")
    body = _stitch_body()
    body["videoUrls"][0]["url"] = CF.format(uid)
    r = await client.post("/api/v1/videos/stitch", headers=h, json=body)
    assert r.status_code == 202, r.text
    assert (await db.upload_attempts.find_one({"_id": uid}))["posted_at"]


async def test_UA4_refusals_keep_the_reason_never_the_caption(client, db):
    owner, oh = await make_user(db)
    _, h = await make_user(db)
    uid = await _address(client, oh)
    assert (await _post(client, oh, uid)).status_code == 201
    r = await _post(client, h, uid)                                     # someone else's video
    assert r.status_code == 400
    r = await client.post("/api/v1/videos/post", headers=h, json={"description": "secret caption", "duration": "x"})
    assert r.status_code == 422
    rows = await db.post_refusals.find({}).to_list(10)
    reasons = sorted(x["reason"] for x in rows)
    assert reasons[0].startswith("This video is already used") and reasons[1].startswith("invalid ")
    assert "secret caption" not in str(rows)
    assert {x["path"] for x in rows} == {"post"} and any(x["user"] for x in rows)


async def test_UA5_counting_never_breaks_posting(client, db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db.upload_attempts, "update_one", boom, raising=False)
    monkeypatch.setattr(db.upload_attempts, "update_many", boom, raising=False)
    _, h = await make_user(db)
    uid = await _address(client, h)
    assert (await _post(client, h, uid)).status_code == 201


async def test_UA6_summary_sorts_why_attempts_stopped(client, db):
    _, ah = await make_user(db, user_type="admin")
    _, uh = await make_user(db)
    now = datetime.utcnow()
    u1, u2, u3 = ObjectId(), ObjectId(), ObjectId()
    rows = [
        {"_id": "a", "user": u1, "at": now - timedelta(hours=3), "size": 9e6, "folder": "videos", "platform": "android",
         "posted_at": now},
        {"_id": "b", "user": u2, "at": now - timedelta(hours=3), "size": 30e6, "folder": "videos", "platform": "ios"},
        {"_id": "c", "user": u3, "at": now - timedelta(hours=3), "size": 30e6, "folder": "videos", "platform": "android"},
        {"_id": "d", "user": u3, "at": now - timedelta(hours=2), "size": 30e6, "folder": "videos", "platform": "android",
         "posted_at": now},
        {"_id": "e", "user": None, "at": now - timedelta(hours=1), "size": 1e6, "folder": "videos", "platform": "other"},
        {"_id": "f", "user": u1, "at": now - timedelta(hours=1), "size": 1e6, "folder": "diag", "platform": "other"},
    ]
    await db.upload_attempts.insert_many(rows)
    await db.post_refusals.insert_one({"user": u2, "at": now - timedelta(hours=3) + timedelta(minutes=2), "path": "post",
                                       "status": 400, "reason": "Pick a category"})
    assert (await client.get("/api/v1/admin/upload-attempts", headers=uh)).status_code == 403
    s = (await client.get("/api/v1/admin/upload-attempts", headers=ah)).json()
    assert (s["attempts"], s["posted"], s["refused"], s["tried_again"], s["no_post"]) == (5, 2, 1, 1, 1)
    assert s["by_platform"]["android"] == {"attempts": 3, "posted": 2}
    assert s["refusal_reasons"] == [{"reason": "400: Pick a category", "count": 1}]
    assert s["size_mb"]["posted"] in (9.0, 30.0) and s["size_mb"]["not_posted"] == 30.0


async def test_UC1_upload_locked_only_the_video_editor_uploads_without_an_account(client, db):
    from bson import ObjectId
    from tests.pressure.conftest import make_user
    ah = {"X-Forwarded-For": "7.7.7.7"}
    job = (await db.pending_videos.insert_one({"status": "processing"})).inserted_id
    done = (await db.pending_videos.insert_one({"status": "completed"})).inserted_id

    async def ask(name, h=ah):
        return (await client.post("/video/v2/presigned-upload", headers=h,
                                  json={"filename": name, "fileSize": 20_000_000})).status_code

    # switch off: open as today, but each accountless address is noted (editor or not)
    assert await ask("cat.mp4") == 200
    assert await ask(f"stitched_{job}.mp4") == 200
    _, admin_h = await make_user(db, user_type="admin")
    s = (await client.get("/api/v1/admin/upload-attempts?days=7", headers=admin_h)).json()
    assert s["without_account"] == {"video_editor": 1, "others": 1}

    await db.config.insert_one({"features": {"upload_locked": True}})
    assert await ask("cat.mp4") == 401                              # a stranger
    assert await ask(f"stitched_{done}.mp4") == 401                 # a finished job
    assert await ask(f"stitched_{ObjectId()}.mp4") == 401           # a made-up job
    assert await ask(f"stitched_{job}.mp4") == 200                  # the editor, a retry
    assert await ask(f"stitched_{job}.mp4") == 200
    assert await ask(f"stitched_{job}.mp4") == 401                  # a running job can't be farmed
    _, h = await make_user(db)
    assert await ask("cat.mp4", {**h, **ah}) == 200                 # signed in: never limited


async def test_UC2_video_status_unknown_is_404_and_one_account_cant_spend_the_cloudflare_allowance(client, db, monkeypatch):
    from tests.pressure.conftest import make_user
    import app.routes.upload_video as uv
    uv._status_hits.clear()

    def fake(uid):
        if uid.startswith("0"):
            raise LookupError(uid)
        return {"uid": uid, "readyToStream": True}
    monkeypatch.setattr(uv, "get_stream_video_status", fake)
    _, h = await make_user(db)
    assert (await client.get("/video/" + "0" * 32, headers=h)).status_code == 404
    codes = [(await client.get("/video/" + "a" * 32, headers=h)).status_code for _ in range(uv.STATUS_CHECKS_PER_MINUTE + 2)]
    assert codes.count(429) >= 2 and codes[0] == 200
    _, h2 = await make_user(db)
    assert (await client.get("/video/" + "a" * 32, headers=h2)).status_code == 200
    uv._status_hits.clear()
