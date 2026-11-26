"""Device-session contract: mocked Mongo and external services; no live accounts."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import Request

from app.core.security import create_access_token
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
ROOT = "/api/v1/auth/sessions"
PROFILE = "/api/v1/users/me/profile"


@pytest.fixture(autouse=True)
def isolated_signin(monkeypatch):
    from app.api.v1 import auth
    async def noop(*args, **kwargs):
        return None
    monkeypatch.setattr(auth.recommendations, "merge_device_into_user", noop)


async def signin(db, uid, device):
    from app.api.v1.auth import _signed_in
    req = Request({"type": "http", "headers": [
        (b"x-device-id", device.encode()), (b"x-wano-device-name", b"Android test phone")
    ], "client": ("127.0.0.1", 123)})
    user = await db.users.find_one({"_id": ObjectId(uid)})
    result = await _signed_in(user, req)
    return {"Authorization": "Bearer " + result["access_token"], "X-Device-Id": device}


async def listing(client, headers):
    r = await client.get(ROOT, headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    return r.json()["sessions"]


async def test_two_phones_keep_access_and_have_distinct_tokens(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "phone-a"), await signin(db, uid, "phone-b")
    assert a["Authorization"] != b["Authorization"]
    assert (await client.get(PROFILE, headers=a)).status_code == 200
    assert (await client.get(PROFILE, headers=b)).status_code == 200
    rows = await listing(client, a)
    assert len(rows) == 2 and sum(x["is_current"] for x in rows) == 1
    assert "Bearer" not in str(rows) and "token_hash" not in str(rows)


async def test_revoke_one_leaves_current_phone_working(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    other = next(x for x in await listing(client, a) if not x["is_current"])
    r = await client.delete(ROOT + "/" + other["id"], headers=a)
    assert r.status_code == 200, r.text
    assert (await client.get(PROFILE, headers=b)).status_code == 401
    assert (await client.get(PROFILE, headers=a)).status_code == 200
    # Replaying the old token must not recreate a session.
    assert (await client.get(PROFILE, headers=b)).status_code == 401


async def test_another_account_cannot_list_or_revoke_your_sessions(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    _, outsider = await make_user(db)
    row = (await listing(client, a))[0]
    assert (await client.delete(ROOT + "/" + row["id"], headers=outsider)).status_code == 404
    assert (await client.get(PROFILE, headers=a)).status_code == 200
    assert (await client.get(ROOT)).status_code == 401


async def test_revoke_others_catches_unseen_legacy_tokens_and_preserves_current(client, db):
    uid, legacy = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    r = await client.post(ROOT + "/revoke-others", headers=a)
    assert r.status_code == 200, r.text
    assert (await client.get(PROFILE, headers=a)).status_code == 200
    for headers in (b, legacy):
        assert (await client.get(PROFILE, headers=headers)).status_code == 401
    # A fresh login after the deliberate revocation still works.
    fresh = await signin(db, uid, "b")
    assert (await client.get(PROFILE, headers=fresh)).status_code == 200


async def test_legacy_users_are_not_forced_out_by_deployment(client, db):
    uid, legacy = await make_user(db)
    for _ in range(3):
        assert (await client.get(PROFILE, headers=legacy)).status_code == 200
    rows = await listing(client, legacy)
    assert len(rows) == 1 and rows[0]["is_current"]


async def test_current_logout_revokes_server_token(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    assert (await client.delete(ROOT + "/current", headers=a)).status_code == 200
    assert (await client.get(PROFILE, headers=a)).status_code == 401
    assert (await client.get(PROFILE, headers=b)).status_code == 200


async def test_revocation_also_covers_graphql(client, db):
    from app.graphql.schema import _authenticated_user_id
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    token = a["Authorization"][7:]
    assert await _authenticated_user_id(token) == uid
    assert (await client.delete(ROOT + "/current", headers=a)).status_code == 200
    assert await _authenticated_user_id(token) is None


async def test_password_change_still_rejects_old_tokens(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {
        "password_changed_at": datetime.now(timezone.utc) + timedelta(seconds=2)}})
    assert (await client.get(PROFILE, headers=a)).status_code == 401


async def test_parallel_registration_of_legacy_token_is_idempotent(client, db):
    _, legacy = await make_user(db)
    replies = await asyncio.gather(*[client.get(PROFILE, headers=legacy) for _ in range(25)])
    assert all(r.status_code == 200 for r in replies)
    assert len(await listing(client, legacy)) == 1


async def test_account_switch_moves_push_token_without_removing_other_phone(client, db):
    uid, _ = await make_user(db)
    vid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    c = await signin(db, vid, "a")
    path = "/api/v1/users/me/push-token"
    for headers, token in [(a, "ExpoPushToken[a]"), (b, "ExpoPushToken[b]"), (c, "ExpoPushToken[a]")]:
        assert (await client.post(path, headers=headers, json={"expo_push_token": token})).status_code == 200
    u = await db.users.find_one({"_id": ObjectId(uid)})
    assert u["expo_push_tokens"] == ["ExpoPushToken[b]"]
    # A delayed logout from the old account cannot remove the new account's token.
    await client.request("DELETE", path, headers=a, json={"expo_push_token": "ExpoPushToken[a]"})
    v = await db.users.find_one({"_id": ObjectId(vid)})
    assert v["expo_push_tokens"] == ["ExpoPushToken[a]"]


async def test_remote_signout_removes_only_target_devices_push(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    path = "/api/v1/users/me/push-token"
    for headers, token in [(a, "ExpoPushToken[a]"), (b, "ExpoPushToken[b]")]:
        await client.post(path, headers=headers, json={"expo_push_token": token})
    other = next(x for x in await listing(client, a) if not x["is_current"])
    assert (await client.delete(ROOT + "/" + other["id"], headers=a)).status_code == 200
    u = await db.users.find_one({"_id": ObjectId(uid)})
    assert u["expo_push_tokens"] == ["ExpoPushToken[a]"]


async def test_revoked_token_cannot_access_optional_auth_routes(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    assert (await client.delete(ROOT + "/current", headers=a)).status_code == 200
    from app.api.deps import get_optional_active_user
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await get_optional_active_user(auth_required=True, token=a["Authorization"][7:])
    assert exc.value.status_code == 401


async def test_legacy_revoke_others_rotates_shared_token(client, db):
    _, legacy = await make_user(db)
    r = await client.post(ROOT + "/revoke-others", headers=legacy)
    assert r.status_code == 200, r.text
    replacement = r.json()["access_token"]
    assert replacement and "Bearer " + replacement != legacy["Authorization"]
    assert (await client.get(PROFILE, headers=legacy)).status_code == 401
    assert (await client.get(PROFILE, headers={"Authorization": "Bearer " + replacement})).status_code == 200


async def test_forged_device_header_does_not_authorize_revocation(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    a["X-Device-Id"] = "b"
    other = next(x for x in await listing(client, a) if not x["is_current"])
    assert (await client.delete(ROOT + "/" + other["id"], headers=a)).status_code == 200
    assert (await client.get(PROFILE, headers=b)).status_code == 401
    assert (await client.get(PROFILE, headers=a)).status_code == 200


async def test_100_replays_never_resurrect_revoked_session(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    await client.delete(ROOT + "/current", headers=a)
    replies = await asyncio.gather(*[client.get(PROFILE, headers=a) for _ in range(100)])
    assert all(r.status_code == 401 for r in replies)


async def test_revoke_others_keeps_only_current_push_and_new_login_can_register(client, db):
    uid, _ = await make_user(db)
    a, b = await signin(db, uid, "a"), await signin(db, uid, "b")
    path = "/api/v1/users/me/push-token"
    for headers, token in [(a, "ExpoPushToken[a]"), (b, "ExpoPushToken[b]")]:
        await client.post(path, headers=headers, json={"expo_push_token": token})
    assert (await client.post(ROOT + "/revoke-others", headers=a)).status_code == 200
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["expo_push_tokens"] == ["ExpoPushToken[a]"]
    assert (await client.post(path, headers=b, json={"expo_push_token": "ExpoPushToken[b]"})).status_code == 401
    fresh = await signin(db, uid, "b")
    assert (await client.post(path, headers=fresh, json={"expo_push_token": "ExpoPushToken[b]"})).status_code == 200


async def test_new_device_alert_history_and_same_device_no_repeat(client, db):
    uid, _ = await make_user(db)
    await signin(db, uid, "a")
    await signin(db, uid, "a")
    assert await db.security_login_alerts.count_documents({}) == 0
    b = await signin(db, uid, "b")
    r = await client.get(ROOT, headers=b)
    assert len(r.json()["alerts"]) == 1
    assert r.json()["alerts"][0]["device_name"] == "Android test phone"


async def test_stale_logout_same_account_does_not_remove_new_login_push(client, db):
    uid, _ = await make_user(db)
    old, fresh = await signin(db, uid, "a"), await signin(db, uid, "a")
    path, body = "/api/v1/users/me/push-token", {"expo_push_token": "ExpoPushToken[a]"}
    await client.post(path, headers=old, json=body)
    await client.post(path, headers=fresh, json=body)
    await client.delete(ROOT + "/current", headers=old)
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["expo_push_tokens"] == [body["expo_push_token"]]


async def test_unknown_or_bad_session_ids_cannot_sign_out_current(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    for invalid in ("not-an-id", str(ObjectId())):
        assert (await client.delete(ROOT + "/" + invalid, headers=a)).status_code == 404
    current = next(x for x in await listing(client, a) if x["is_current"])
    assert (await client.delete(ROOT + "/" + current["id"], headers=a)).status_code == 400
    assert (await client.get(PROFILE, headers=a)).status_code == 200


async def test_open_graphql_subscription_rechecks_revocation(client, db):
    from types import SimpleNamespace
    from app.graphql.subscriptions import _check_live_session
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    request = Request({"type": "http", "headers": [(b"authorization", a["Authorization"].encode())]})
    info = SimpleNamespace(context={"user_id": uid, "request": request})
    await _check_live_session(info)
    await client.delete(ROOT + "/current", headers=a)
    with pytest.raises(Exception, match="Authentication required"):
        await _check_live_session(info)


async def test_pagination_no_missing_sessions_or_cross_account_rows(client, db):
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    for n in range(55):
        await signin(db, uid, f"phone-{n}")
    other_uid, _ = await make_user(db)
    await signin(db, other_uid, "private")
    first = (await client.get(ROOT, headers=a)).json()
    assert len(first["sessions"]) == 50 and first["next_before"]
    second = (await client.get(ROOT, params={"before": first["next_before"]}, headers=a)).json()
    assert len(second["sessions"]) == 6 and second["next_before"] is None
    ids = [s["id"] for s in first["sessions"] + second["sessions"]]
    assert len(set(ids)) == 56
    assert (await client.get(ROOT, params={"before": "invalid"}, headers=a)).status_code == 400


async def test_unique_login_tokens_even_for_100_simultaneous_signins(client, db):
    uid, _ = await make_user(db)
    headers = await asyncio.gather(*[signin(db, uid, f"phone-{n}") for n in range(100)])
    assert len({h["Authorization"] for h in headers}) == 100
    assert await db.device_sessions.count_documents({"user_id": ObjectId(uid)}) == 100


async def test_new_style_token_cannot_become_legacy_if_session_row_missing(client, db):
    uid, _ = await make_user(db)
    token = create_access_token({"sub": uid, "sid": "not-registered", "sg": 0})
    assert (await client.get(PROFILE, headers={"Authorization": "Bearer " + token})).status_code == 401


async def test_new_device_push_is_throttled_and_failure_does_not_cancel_login(client, db, monkeypatch):
    from app.services.device_sessions import deliver_alert
    from app.services import growth_notifications
    uid, _ = await make_user(db, expo_push_tokens=["ExpoPushToken[test]"])
    await signin(db, uid, "a")
    b = await signin(db, uid, "b")
    event = await db.security_login_alerts.find_one({"user_id": ObjectId(uid)})
    attempts = []
    async def broken(*args):
        attempts.append(args)
        raise RuntimeError("mock offline")
    monkeypatch.setattr(growth_notifications, "_send_one", broken)
    await deliver_alert(uid, str(event["_id"]))
    await deliver_alert(uid, str(event["_id"]))
    assert len(attempts) == 1
    assert (await client.get(PROFILE, headers=b)).status_code == 200


async def test_ordinary_legacy_logout_does_not_eject_other_phone_sharing_token(client, db):
    _, legacy = await make_user(db)
    r = await client.delete(ROOT + "/current", headers=legacy)
    assert r.status_code == 200 and r.json()["server_revoked"] is False
    assert (await client.get(PROFILE, headers=legacy)).status_code == 200


async def test_graphql_fails_closed_if_session_validator_cannot_import(client, db, monkeypatch):
    import sys
    from app.graphql.schema import _authenticated_user_id
    uid, _ = await make_user(db)
    a = await signin(db, uid, "a")
    monkeypatch.setitem(sys.modules, "app.services.device_sessions", None)
    assert await _authenticated_user_id(a["Authorization"][7:]) is None


async def test_legacy_revoke_others_does_not_keep_other_phones_push_tokens(client, db):
    uid, legacy = await make_user(db)
    for push in ("ExpoPushToken[this]", "ExpoPushToken[other]"):
        await client.post("/api/v1/users/me/push-token", headers=legacy,
            json={"expo_push_token": push, "badge_version": 1})
    r = await client.post(ROOT + "/revoke-others", headers=legacy,
        json={"current_push_token": "ExpoPushToken[this]"})
    assert r.status_code == 200, r.text
    user = await db.users.find_one({"_id": ObjectId(uid)})
    assert user["expo_push_tokens"] == ["ExpoPushToken[this]"]
    assert user["badge_capable_tokens"] == ["ExpoPushToken[this]"]
    assert (await client.get(PROFILE, headers=legacy)).status_code == 401
