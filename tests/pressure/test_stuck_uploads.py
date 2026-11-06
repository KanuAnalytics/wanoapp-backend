"""Attacks on the stuck-upload rescue: posts whose 'video ready' message was missed."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from recombee_api_client.api_requests import SetItemValues

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


def uid(n):
    return "%032x" % (0xabc000 + n)


class FakeStream:
    """Cloudflare's status API as the rescue sees it."""

    def __init__(self):
        self.states = {}      # uid -> {"exists", "ready", "state"} or Exception
        self.calls = []

    def fetch(self, u):
        self.calls.append(u)
        s = self.states.get(u, {"exists": True, "ready": False, "state": "inprogress"})
        if isinstance(s, Exception):
            raise s
        return s


@pytest.fixture
def cf(monkeypatch):
    from app.services import stuck_uploads
    fake = FakeStream()
    monkeypatch.setattr(stuck_uploads, "fetch_state", fake.fetch)
    return fake


async def _stuck(db, n, minutes_old=30, **extra):
    c, _ = await make_user(db)
    u = uid(n)
    vid = await make_video(db, c, created_at=ago(minutes=minutes_old), isReadyToStream=False, stream_uid=u,
                           remoteUrl_CF=f"https://videodelivery.net/{u}/manifest/video.m3u8", **extra)
    return c, vid, u


async def _run(db):
    from app.services import stuck_uploads
    return await stuck_uploads.rescue_once(db)


async def _doc(db, vid):
    return await db.videos.find_one({"_id": ObjectId(vid)})


async def test_U1_missed_ready_message_is_rescued(client, db, rb, cf):
    c, vid, u = await _stuck(db, 1, urls={"thumbnail": None})
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    r = await _run(db)
    d = await _doc(db, vid)
    assert r["ready"] == 1 and d["isReadyToStream"] is True and u in d["urls"]["thumbnail"]
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_ready"}) == 1
    assert any(x.item_id == vid for x in rb.of(SetItemValues))
    await _run(db)
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_ready"}) == 1


async def test_U2_still_processing_is_left_alone_and_not_hammered(client, db, rb, cf):
    _, vid, u = await _stuck(db, 2)
    await _run(db)
    await _run(db)
    await _run(db)
    assert cf.calls.count(u) == 1, "re-checked Cloudflare without waiting"
    assert (await _doc(db, vid))["isReadyToStream"] is False


async def test_U3_failed_at_cloudflare_tells_the_creator_once(client, db, rb, cf):
    c, vid, u = await _stuck(db, 3)
    cf.states[u] = {"exists": True, "ready": False, "state": "error", "error": "ERR_NON_VIDEO"}
    await _run(db)
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"upload_check.next_at": ago(hours=1)}})
    await _run(db)
    d = await _doc(db, vid)
    assert d["upload_check"]["state"] == "failed" and d["isReadyToStream"] is False
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_failed"}) == 1
    assert cf.calls.count(u) == 1


async def test_U3_creator_sees_failed_on_their_pinned_post(client, db, rb, cf):
    from app.core.security import create_access_token
    c, vid, u = await _stuck(db, 13)
    cf.states[u] = {"exists": True, "ready": False, "state": "error"}
    await _run(db)
    h = {"Authorization": f"Bearer {create_access_token({'sub': c})}"}
    page = (await client.get("/api/v1/feed/?cursor=start&limit=5&include_own=true", headers=h)).json()
    assert page and page[0]["id"] == vid and page[0]["status"] == "failed"


async def test_U4_video_missing_at_cloudflare_fails(client, db, rb, cf):
    _, vid, u = await _stuck(db, 4)
    cf.states[u] = {"exists": False, "ready": False, "state": None}
    await _run(db)
    assert (await _doc(db, vid))["upload_check"]["state"] == "failed"


async def test_U5_processing_for_over_a_day_gives_up(client, db, rb, cf):
    _, vid, u = await _stuck(db, 5, minutes_old=60 * 25)
    await _run(db)
    d = await _doc(db, vid)
    assert d["upload_check"]["state"] == "failed" and d["upload_check"]["reason"] == "timeout"


async def test_U6_cloudflare_down_never_fails_a_post(client, db, rb, cf):
    _, vid, u = await _stuck(db, 6)
    cf.states[u] = RuntimeError("Cloudflare 503")
    r = await _run(db)
    d = await _doc(db, vid)
    assert r["errors"] == 1 and (d.get("upload_check") or {}).get("state") != "failed"
    assert d["upload_check"]["next_at"] > datetime.utcnow()


async def test_U7_leaves_everything_else_alone(client, db, rb, cf):
    c, _ = await make_user(db)
    fresh = await make_video(db, c, created_at=ago(minutes=3), isReadyToStream=False, stream_uid=uid(70))
    ready = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True, stream_uid=uid(71))
    photo = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=False, media_type="photo",
                             images=["https://cdn.example/p.jpg"])
    gone = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=False, stream_uid=uid(72), is_active=False)
    ancient = await make_video(db, c, created_at=ago(days=9), isReadyToStream=False, stream_uid=uid(73))
    no_uid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=False, stream_uid=None,
                              remoteUrl_CF=None, remoteUrl="https://spaces.example/v.mp4")
    bad_uid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=False, stream_uid="../../etc",
                               remoteUrl_CF="https://videodelivery.net/../x/manifest/video.m3u8")
    r = await _run(db)
    assert cf.calls == [] and r["checked"] == 0
    for v in (fresh, photo, no_uid, bad_uid):
        assert "upload_check" not in await _doc(db, v)
    assert ready and gone and ancient


async def test_U8_capped_per_run(client, db, rb, cf):
    from app.services import stuck_uploads
    for i in range(stuck_uploads.MAX_PER_RUN + 40):
        await _stuck(db, 100 + i)
    await _run(db)
    assert len(cf.calls) == stuck_uploads.MAX_PER_RUN


async def test_U9_two_servers_check_each_post_once(client, db, rb, cf):
    for i in range(20):
        await _stuck(db, 300 + i)
    await asyncio.gather(_run(db), _run(db), _run(db))
    assert len(cf.calls) == 20 and len(set(cf.calls)) == 20


async def test_U10_scheduled_post_rescued_quietly(client, db, rb, cf):
    _, vid, u = await _stuck(db, 10, status="scheduled", is_active=False)
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    await _run(db)
    # Scheduled posts are saved hidden; they're published (and announced) later.
    assert await db.notifications.count_documents({"post_id": ObjectId(vid)}) == 0


async def test_U11_backoff_grows(client, db, rb, cf):
    from app.services import stuck_uploads
    _, vid, u = await _stuck(db, 11)
    gaps = []
    for _ in range(4):
        await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"upload_check.next_at": ago(seconds=1)}})
        before = datetime.utcnow()
        await _run(db)
        gaps.append(((await _doc(db, vid))["upload_check"]["next_at"] - before).total_seconds())
    assert gaps == sorted(gaps) and gaps[-1] <= stuck_uploads.BACKOFF_MINUTES[-1] * 60 + 5


async def test_U12_admin_run_now(client, db, rb, cf):
    _, vid, u = await _stuck(db, 12)
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/rescue-uploads", headers=h)).status_code == 403
    _, ah = await make_user(db, user_type="admin")
    r = await client.post("/api/v1/recombee/rescue-uploads", headers=ah)
    assert r.status_code == 200 and r.json()["ready"] == 1


async def test_U13_failed_post_comes_back_if_cloudflare_finishes_later(client, db, rb, cf):
    from app.routes import upload_video
    _, vid, u = await _stuck(db, 14, minutes_old=60 * 25)
    await _run(db)
    assert (await _doc(db, vid))["upload_check"]["state"] == "failed"
    await upload_video.mark_stream_ready(u)        # the late webhook still wins
    d = await _doc(db, vid)
    assert d["isReadyToStream"] is True and d["upload_check"]["state"] == "rescued"
