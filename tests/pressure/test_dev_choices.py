"""
Behaviour deliberately kept from dev when the security fixes were merged
(see FIXES_AND_DEPLOY_NOTES.md): no Lambda callback secret, tokens that
don't expire, and replies to replies.
"""

import pytest
from jose import jwt

from app.core.security import create_access_token
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def test_lambda_can_presign_without_credentials(client, db, monkeypatch):
    import app.routes.upload_video as uv

    monkeypatch.setattr(
        uv, "generate_cf_tus_upload_url",
        lambda **kw: {"uploadURL": "https://upload.example/tus", "uid": "abc"},
    )
    r = await client.post(
        "/video/v2/presigned-upload",
        json={"filename": "stitched.mp4", "fileSize": 10},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["uid"] == "abc"


async def test_presign_still_rejects_a_bad_token(client, db):
    r = await client.post(
        "/video/v2/presigned-upload",
        headers={"Authorization": "Bearer not-a-token"},
        json={"filename": "a.mp4", "fileSize": 10},
    )
    assert r.status_code == 401, r.status_code


async def test_tokens_do_not_expire_even_with_a_short_setting(monkeypatch):
    from app.core import security

    monkeypatch.setattr(security.settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 30)
    claims = jwt.get_unverified_claims(create_access_token({"sub": "x"}))
    assert "exp" not in claims and "iat" in claims


async def test_reply_to_a_reply_is_allowed(client, db):
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)

    top = await client.post("/api/v1/", headers=ch, json={"video_id": vid, "content": "top"})
    assert top.status_code == 200, top.text
    reply = await client.post(
        "/api/v1/", headers=ch,
        json={"video_id": vid, "content": "reply", "parent_id": top.json()["_id"]},
    )
    assert reply.status_code == 200, reply.text
    nested = await client.post(
        "/api/v1/", headers=ch,
        json={"video_id": vid, "content": "reply to reply", "parent_id": reply.json()["_id"]},
    )
    assert nested.status_code == 200, nested.text
