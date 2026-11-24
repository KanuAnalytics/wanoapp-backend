"""Recipient-facing warning explanations, legacy compatibility and validation."""
import pytest
from bson import ObjectId
from tests.pressure.conftest import make_user
from tests.pressure.test_safe_mod import _switch, _web
pytestmark = pytest.mark.asyncio

async def test_custom_warning_persists_for_recipient_and_retry(client, db):
    admin, _ = await make_user(db, user_type="admin")
    user, _ = await make_user(db)
    await _switch(db, safe_ban=True, warnings=True)
    body = {"user_id": user, "reason": "nudity", "message": "We removed your video. Further violations may lead to suspension.", "note": "Private evidence reference", "key": "warning-one"}
    for _ in range(2):
        r = await client.post('/api/v1/mod/actions/warn', json=body, headers=_web(admin))
        assert r.status_code == 200, r.text
    assert await db.warnings.count_documents({"user": ObjectId(user)}) == 1
    w = await db.warnings.find_one({"user": ObjectId(user)})
    assert w['text'] == body['message']
    assert 'Private evidence' not in w['text']
    assert w['rule'] == 'nudity'

async def test_legacy_warning_keeps_template(client, db):
    admin, _ = await make_user(db, user_type="admin")
    user, _ = await make_user(db)
    await _switch(db, safe_ban=True, warnings=True)
    r = await client.post('/api/v1/mod/actions/warn', json={"user_id": user,"reason":"nudity"}, headers=_web(admin))
    assert r.status_code == 200, r.text
    w = await db.warnings.find_one({"user": ObjectId(user)})
    assert w['text'] == "Nudity and sexual content aren't allowed on Wano."

@pytest.mark.parametrize('message', [' ', 'x'*601])
async def test_bad_message_no_warning(client, db, message):
    admin, _ = await make_user(db, user_type="admin")
    user, _ = await make_user(db)
    await _switch(db, safe_ban=True, warnings=True)
    r = await client.post('/api/v1/mod/actions/warn', json={"user_id": user,"reason":"nudity","message":message}, headers=_web(admin))
    assert r.status_code in (400,422), r.text
    assert await db.warnings.count_documents({}) == 0
    assert await db.mod_actions.count_documents({}) == 0
