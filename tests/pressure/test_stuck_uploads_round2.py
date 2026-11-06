"""Round 2: the real Cloudflare call, crashes mid-check, races with the webhook."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401
from tests.pressure.test_stuck_uploads import FakeStream, _doc, _run, _stuck, cf, uid  # noqa: F401

pytestmark = pytest.mark.asyncio


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def creds(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "acct", raising=False)
    monkeypatch.setattr(settings, "CLOUDFLARE_STREAM_API_TOKEN", "tok", raising=False)


@pytest.mark.parametrize("resp,expect", [
    (_Resp(404), {"exists": False}),
    (_Resp(200, {"success": True, "result": {"readyToStream": True, "status": {"state": "ready"}}}), {"ready": True}),
    (_Resp(200, {"success": True, "result": {"readyToStream": False, "status": {"state": "error",
                                                                                 "errorReasonCode": "ERR"}}}),
     {"state": "error", "error": "ERR"}),
])
async def test_UR1_real_fetch_reads_cloudflare(monkeypatch, creds, resp, expect):
    from app.services import stuck_uploads
    monkeypatch.setattr(stuck_uploads.requests, "get", lambda *a, **k: resp)
    got = stuck_uploads.fetch_state(uid(1))
    assert all(got[k] == v for k, v in expect.items()), got


@pytest.mark.parametrize("resp", [_Resp(500), _Resp(200, {"success": False, "errors": ["x"]}),
                                  _Resp(200, ValueError("not json")), _Resp(401)])
async def test_UR2_real_fetch_raises_rather_than_guessing(monkeypatch, creds, resp):
    from app.services import stuck_uploads
    monkeypatch.setattr(stuck_uploads.requests, "get", lambda *a, **k: resp)
    with pytest.raises(Exception):
        stuck_uploads.fetch_state(uid(2))


async def test_UR3_no_credentials_never_fails_posts(client, db, rb, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "CLOUDFLARE_STREAM_API_TOKEN", "", raising=False)
    _, vid, _ = await _stuck(db, 50)
    r = await _run(db)
    assert r["errors"] == 1 and (await _doc(db, vid))["upload_check"]["state"] != "failed"


async def test_UR4_crash_mid_check_is_picked_up_again(client, db, rb, cf):
    _, vid, u = await _stuck(db, 51)
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"upload_check": {
        "state": "checking", "attempts": 1, "next_at": datetime.utcnow() - timedelta(minutes=1)}}})
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    await _run(db)
    assert (await _doc(db, vid))["isReadyToStream"] is True


async def test_UR5_rescue_and_webhook_at_once_notify_once(client, db, rb, cf):
    from app.routes import upload_video
    _, vid, u = await _stuck(db, 52)
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    await asyncio.gather(_run(db), upload_video.mark_stream_ready(u), upload_video.mark_stream_ready(u))
    assert await db.notifications.count_documents({"post_id": ObjectId(vid), "type": "video_ready"}) == 1


async def test_UR6_broken_push_tokens_do_not_stop_the_rescue(client, db, rb, cf, monkeypatch):
    from app.services import expo
    def boom(*a, **k):          # blocking, like the real sender
        raise RuntimeError("expo down")
    monkeypatch.setattr(expo, "send_push_message", boom)
    c, vid, u = await _stuck(db, 53)
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"expo_push_tokens": ["a", "b", "c"]}})
    cf.states[u] = {"exists": False, "ready": False, "state": None}
    _, vid2, u2 = await _stuck(db, 54)
    cf.states[u2] = {"exists": True, "ready": True, "state": "ready"}
    r = await _run(db)
    assert r["failed"] == 1 and r["ready"] == 1


async def test_UR7_failed_upload_push_really_goes_out(client, db, rb, cf, calls, caplog):
    """The real push sender is blocking: awaiting it used to throw after sending
    (and block the server while it sent)."""
    import logging
    caplog.set_level(logging.WARNING)
    c, vid, u = await _stuck(db, 55)
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"expo_push_tokens": ["tok-1", "tok-2"]}})
    cf.states[u] = {"exists": False, "ready": False, "state": None}
    r = await _run(db)
    assert r["failed"] == 1
    sent = [p for p in calls["pushes"] if p[0] == "send_push_message"]
    assert sorted(p[1][0] for p in sent) == ["tok-1", "tok-2"]
    assert "did not send" not in caplog.text, "push sender was awaited"


async def test_UR8_ready_rescue_sends_pushes_off_the_server_loop(client, db, rb, cf, monkeypatch):
    """The 'uploaded' push after a rescue must not run the blocking sender on the event loop."""
    import asyncio
    import threading
    from app.services import expo
    threads = []

    def slow_blocking_send(*a, **k):
        threads.append(threading.current_thread() is threading.main_thread())
    monkeypatch.setattr(expo, "send_push_message", slow_blocking_send)
    from app.routes import upload_video
    monkeypatch.setattr(upload_video, "send_push_message", slow_blocking_send, raising=False)
    c, vid, u = await _stuck(db, 61)
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"expo_push_tokens": ["t1"]}})
    cf.states[u] = {"exists": True, "ready": True, "state": "ready"}
    r = await _run(db)
    for _ in range(50):
        if threads:
            break
        await asyncio.sleep(0.02)
    assert r["ready"] == 1 and threads, "no push sent"
    assert not any(threads), "blocking push ran on the server's event loop"
