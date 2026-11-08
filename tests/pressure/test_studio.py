"""Studio: posts for Wano's house accounts, approved before they go out."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
S = "/api/v1/admin/studio"
UID = "a" * 32


@pytest.fixture
def cf(monkeypatch):
    import app.services.upload_DO as up
    monkeypatch.setattr(up, "generate_stream_direct_upload_url",
                        lambda filename, folder="videos": {"upload_url": "https://upload.cloudflarestream.com/x",
                                                           "stream_uid": UID, "file_url": "u", "name": filename})
    monkeypatch.setattr(up, "get_stream_video_status", lambda uid: {"readyToStream": False})


async def _setup(client, db):
    _, ah = await make_user(db, user_type="admin")
    acct, _ = await make_user(db, username="ANN", display_name="Africa News now")
    r = await client.post(f"{S}/accounts", json={"creator": "@ANN"}, headers=ah)
    assert r.status_code == 200, r.text
    return ah, acct


async def _draft(client, ah, acct, **extra):
    body = {"account_id": acct, "stream_uid": UID, "title": "Africa in 60 seconds", "caption": "Today #Africa", **extra}
    return await client.post(f"{S}/drafts", json=body, headers=ah)


async def test_ST1_only_admins(client, db, cf):
    _, h = await make_user(db)
    for m, p in [("GET", "/accounts"), ("POST", "/upload-url"), ("GET", "/drafts"), ("POST", "/drafts")]:
        assert (await client.request(m, S + p, headers=h, json={})).status_code in (403, 422), p


async def test_ST2_house_accounts_and_upload_link(client, db, cf):
    ah, acct = await _setup(client, db)
    items = (await client.get(f"{S}/accounts", headers=ah)).json()["items"]
    assert [a["username"] for a in items] == ["ANN"]
    r = await client.post(f"{S}/upload-url", json={"filename": "../../etc/pa ss.mp4"}, headers=ah)
    assert r.status_code == 200 and r.json()["stream_uid"] == UID
    # admins can't be made house accounts
    await make_user(db, username="boss", user_type="admin")
    assert (await client.post(f"{S}/accounts", json={"creator": "@boss"}, headers=ah)).status_code == 400


async def test_ST3_draft_only_for_active_house_accounts_and_real_uploads(client, db, cf):
    ah, acct = await _setup(client, db)
    other, _ = await make_user(db, username="random")
    assert (await _draft(client, ah, other)).status_code == 404
    assert (await _draft(client, ah, acct, stream_uid="not-a-uid")).status_code == 422
    assert (await _draft(client, ah, acct, title="", caption="")).status_code == 422
    assert (await _draft(client, ah, acct, caption={"$ne": 1})).status_code == 422
    assert (await _draft(client, ah, acct)).status_code == 201
    assert await db.videos.count_documents({}) == 0          # nothing posted yet


async def test_ST4_approve_posts_once_on_the_house_account(client, db, cf):
    ah, acct = await _setup(client, db)
    did = (await _draft(client, ah, acct, ai=True)).json()["id"]
    rs = await asyncio.gather(*[client.post(f"{S}/drafts/{did}/approve", json={}, headers=ah) for _ in range(5)])
    assert sorted(r.status_code for r in rs).count(200) == 1
    assert await db.videos.count_documents({}) == 1
    v = await db.videos.find_one({})
    assert v["creator_id"] == ObjectId(acct) and v["title"] == "Africa in 60 seconds" and v["ai_generated"] is True
    assert v["remoteUrl_CF"].endswith(f"{UID}/manifest/video.m3u8")
    d = (await client.get(f"{S}/drafts", params={"state": "approved"}, headers=ah)).json()["items"][0]
    assert d["post_id"] == str(v["_id"])
    assert await db.admin_log.count_documents({"action": "studio_approved"}) == 1


async def test_ST5_scheduled_draft_goes_out_at_its_time(client, db, cf):
    ah, acct = await _setup(client, db)
    at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    did = (await _draft(client, ah, acct, scheduled_for=at)).json()["id"]
    r = await client.post(f"{S}/drafts/{did}/approve", json={}, headers=ah)
    assert r.status_code == 200 and r.json()["scheduled_for"]
    v = await db.videos.find_one({})
    assert v["status"] == "scheduled"


async def test_ST6_reject_and_no_approve_after(client, db, cf):
    ah, acct = await _setup(client, db)
    did = (await _draft(client, ah, acct)).json()["id"]
    assert (await client.post(f"{S}/drafts/{did}/reject", json={"note": "Check the facts"}, headers=ah)).status_code == 200
    assert (await client.post(f"{S}/drafts/{did}/approve", json={}, headers=ah)).status_code == 409
    d = (await client.get(f"{S}/drafts", params={"state": "rejected"}, headers=ah)).json()["items"][0]
    assert d["note"] == "Check the facts"
    assert await db.videos.count_documents({}) == 0


async def test_ST7_account_removed_or_banned_before_approval(client, db, cf):
    ah, acct = await _setup(client, db)
    did = (await _draft(client, ah, acct)).json()["id"]
    await db.users.update_one({"_id": ObjectId(acct)}, {"$set": {"is_active": False}})
    assert (await client.post(f"{S}/drafts/{did}/approve", json={}, headers=ah)).status_code == 400
    assert (await client.get(f"{S}/drafts", params={"state": "pending"}, headers=ah)).json()["items"][0]["id"] == did
    assert await db.videos.count_documents({}) == 0


async def test_ST8_admin_page_allows_only_cloudflare_uploads(client, db):
    r = await client.get("/admin")
    csp = r.headers.get("content-security-policy", "")
    assert "connect-src 'self' https://upload.cloudflarestream.com https://upload.videodelivery.net;" in csp
