"""Safe moderation (safe_ban), warnings, staff sessions, staff_view and
staff_quiet. Attacks first: a stolen app token, a replayed code, a double tap,
a moderator reaching for admin powers, undo after the window, a wipe too soon."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import create_access_token
from app.services import safe_mod, staff
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
M = "/api/v1/mod"


async def _switch(db, **on):
    doc = await db.config.find_one({"features": {"$type": "object"}})
    if doc:
        await db.config.update_one({"_id": doc["_id"]}, {"$set": {f"features.{k}": v for k, v in on.items()}})
    else:
        await db.config.insert_one({"features": dict(on)})
    staff.forget()


def _web(uid: str) -> dict:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp())
    return {"Authorization": f"Bearer {create_access_token({'sub': uid, 'exp': exp, 'scope': 'admin_web'})}"}


async def _staff_session(client, db, uid: str, h: dict, device="device-abc-123") -> dict:
    secret = staff.new_secret()
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"staff_totp": {"secret": secret}}})
    code = staff._code(secret, int(time.time() // 30))
    r = await client.post(f"{M}/session", json={"code": code, "device": device}, headers=h)
    assert r.status_code == 200, r.text
    return {**h, "X-Staff-Session": r.json()["session"], "X-Staff-Device": device}


async def _admin(db):
    uid, h = await make_user(db, user_type="admin")
    return uid, _web(uid)


async def _setup_target(db):
    t, th = await make_user(db, expo_push_tokens=["ExponentPushToken[t]"], followers_count=5)
    v1 = await make_video(db, t, privacy="public")
    v2 = await make_video(db, t, privacy="followers")
    sched = await make_video(db, t, status="scheduled", is_active=False, scheduled_at=datetime.utcnow() + timedelta(hours=2))
    other, oh = await make_user(db)
    ov = await make_video(db, other)
    return t, th, v1, v2, sched, other, oh, ov


async def _comment(client, h, vid, text="hello"):
    r = await client.post("/api/v1/", json={"content": text, "video_id": vid}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["_id"]


# ------------------------------------------------------------------ switch off
async def test_SM1_off_everything_404_and_old_ban_unchanged(client, db):
    await _switch(db)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    assert (await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "spam"}, headers=ah)).status_code == 404
    assert (await client.get(f"{M}/me", headers=ah)).json() == {"staff": False}
    _, app_h = await make_user(db, user_type="admin")
    r = await client.delete(f"/api/v1/users/ban/{t}", headers=app_h)
    assert r.status_code == 204
    assert (await db.users.find_one({"_id": ObjectId(t)}))["username"] == f"deleted_user-{t}"


# ------------------------------------------------------------------ freeze and undo
async def test_SM2_ban_from_the_app_freezes_and_undo_restores_exactly(client, db, calls):
    await _switch(db, safe_ban=True, comment_guard=True)
    t, th, v1, v2, sched, other, oh, ov = await _setup_target(db)
    c_on_other = await _comment(client, th, ov, "nice")
    reply_parent = await _comment(client, oh, ov, "parent")
    r = await client.post("/api/v1/", json={"content": "reply", "video_id": ov, "parent_id": reply_parent}, headers=th)
    assert r.status_code == 200
    before = await db.videos.find_one({"_id": ObjectId(ov)})
    assert before["comments_count"] == 3
    appadm, app_h = await make_user(db, user_type="admin")
    assert (await client.delete(f"/api/v1/users/ban/{t}", headers=app_h)).status_code == 204
    u = await db.users.find_one({"_id": ObjectId(t)})
    assert u["is_active"] is False and u["mod_state"]["state"] == "frozen" and u["username"] != f"deleted_user-{t}"
    assert "expo_push_tokens" not in u and u["mod_state"]["push_tokens"] == ["ExponentPushToken[t]"]
    for v, was in ((v1, "public"), (v2, "followers")):
        d = await db.videos.find_one({"_id": ObjectId(v)})
        assert d["privacy"] == "private" and d["is_active"] is True and d["moderation"]["frozen_prev_privacy"] == was
    assert (await db.videos.find_one({"_id": ObjectId(sched)}))["status"] == "paused"
    after = await db.videos.find_one({"_id": ObjectId(ov)})
    assert after["comments_count"] == 1
    assert (await db.comments.find_one({"_id": ObjectId(reply_parent)}))["replies_count"] == 0
    # Signed out reading on the other post: the frozen person's comments are gone.
    lst = (await client.get(f"/api/v1/video/{ov}", params={"authenticated": "false"})).json()
    assert [c["_id"] for c in lst] == [reply_parent]
    # The account can't use the app.
    assert (await client.get(f"{M}/me", headers=th)).status_code == 403
    # Undo within the window puts back exactly what changed.
    act = await db.mod_actions.find_one({"target_user": ObjectId(t), "kind": "freeze"})
    r = await client.post(f"{M}/actions/{act['_id']}/undo", headers=_web(appadm))
    assert r.status_code == 200 and r.json()["state"] == "undone"
    u = await db.users.find_one({"_id": ObjectId(t)})
    assert u["is_active"] is True and "mod_state" not in u and u["expo_push_tokens"] == ["ExponentPushToken[t]"]
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["privacy"] == "public"
    assert (await db.videos.find_one({"_id": ObjectId(v2)}))["privacy"] == "followers"
    assert (await db.videos.find_one({"_id": ObjectId(sched)}))["status"] == "scheduled"
    assert (await db.videos.find_one({"_id": ObjectId(ov)}))["comments_count"] == 3
    assert (await db.comments.find_one({"_id": ObjectId(reply_parent)}))["replies_count"] == 1
    assert c_on_other in [c["_id"] for c in (await client.get(f"/api/v1/video/{ov}", headers=oh)).json()]


async def test_SM3_freeze_deletes_nothing(client, db, monkeypatch):
    await _switch(db, safe_ban=True)
    import app.services.video_deletion as vd
    deleted = []

    async def no(*a, **k):
        deleted.append(a)
    monkeypatch.setattr(vd, "soft_delete_video", no)
    monkeypatch.setattr(safe_mod, "old_ban", no)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    r = await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "spam", "key": "k1"}, headers=ah)
    assert r.status_code == 200, r.text
    assert deleted == []


async def test_SM4_double_tap_does_it_once_and_undo_window_closes(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    body = {"user_id": t, "reason": "spam", "key": "tap-1"}
    r1 = await client.post(f"{M}/actions/freeze", json=body, headers=ah)
    r2 = await client.post(f"{M}/actions/freeze", json=body, headers=ah)
    assert r1.status_code == 200 and r2.status_code == 200 and r1.json()["id"] == r2.json()["id"]
    assert await db.mod_actions.count_documents({"kind": "freeze"}) == 1
    await db.mod_actions.update_one({"_id": ObjectId(r1.json()["id"])},
                                    {"$set": {"undo_until": datetime.utcnow() - timedelta(seconds=1)}})
    assert (await client.post(f"{M}/actions/{r1.json()['id']}/undo", headers=ah)).status_code == 409
    r = await client.post(f"{M}/actions/unfreeze", json={"user_id": t, "reason": "other"}, headers=ah)
    assert r.status_code == 200 and (await db.users.find_one({"_id": ObjectId(t)}))["is_active"] is True


async def test_SM5_reason_required_from_the_list(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    for bad in ({"user_id": t}, {"user_id": t, "reason": "because"}, {"user_id": t, "reason": "spam", "x": 1}):
        assert (await client.post(f"{M}/actions/freeze", json=bad, headers=ah)).status_code == 422


# ------------------------------------------------------------------ who may do it
async def test_SM6_app_token_alone_is_not_enough(client, db):
    await _switch(db, safe_ban=True)
    aid, app_h = await make_user(db, user_type="admin")         # an app token never expires
    t, *_ = await _setup_target(db)
    r = await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "spam"}, headers=app_h)
    assert r.status_code == 401 and r.headers.get("x-staff-session") == "required"
    sh = await _staff_session(client, db, aid, app_h)
    assert (await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "spam"}, headers=sh)).status_code == 200


async def test_SM7_codes_cant_be_replayed_guessed_or_moved_to_another_phone(client, db):
    await _switch(db, safe_ban=True)
    mid, mh = await make_user(db, staff_role="moderator")
    secret = staff.new_secret()
    await db.users.update_one({"_id": ObjectId(mid)}, {"$set": {"staff_totp": {"secret": secret}}})
    code = staff._code(secret, int(time.time() // 30))
    ok = await client.post(f"{M}/session", json={"code": code, "device": "phone-one-123"}, headers=mh)
    assert ok.status_code == 200
    again = await client.post(f"{M}/session", json={"code": code, "device": "phone-one-123"}, headers=mh)
    assert again.status_code == 401                              # same code twice
    sid = ok.json()["session"]
    t, *_ = await _setup_target(db)
    other_phone = {**mh, "X-Staff-Session": sid, "X-Staff-Device": "phone-two-456"}
    assert (await client.post(f"{M}/actions/limit", json={"user_id": t, "reason": "spam", "hours": 24},
                              headers=other_phone)).status_code == 401
    for i in range(staff.CODE_TRIES_PER_HOUR):
        await client.post(f"{M}/session", json={"code": "000000", "device": "phone-one-123"}, headers=mh)
    good = staff._code(secret, int(time.time() // 30) + 1)
    assert (await client.post(f"{M}/session", json={"code": good, "device": "phone-one-123"}, headers=mh)).status_code == 429


async def test_SM8_session_ends_when_idle_and_owner_can_end_all(client, db):
    await _switch(db, safe_ban=True)
    mid, mh = await make_user(db, staff_role="moderator")
    sh = await _staff_session(client, db, mid, mh)
    t, *_ = await _setup_target(db)
    body = {"user_id": t, "reason": "spam", "hours": 24}
    assert (await client.post(f"{M}/actions/limit", json=body, headers=sh)).status_code == 200
    await db.staff_sessions.update_many({}, {"$set": {"last_at": datetime.utcnow() - staff.IDLE - timedelta(seconds=5)}})
    assert (await client.post(f"{M}/actions/limit", json={**body, "user_id": (await make_user(db))[0]},
                              headers=sh)).status_code == 401
    sh2 = await _staff_session(client, db, mid, mh, device="device-zzz-999")
    oid_, oh = await _admin(db)
    assert (await client.post(f"{M}/sessions/end-all", headers=oh)).status_code == 200
    assert (await client.get(f"{M}/inbox", headers=sh2)).status_code == 401


async def test_SM9_moderators_never_reach_admin_powers(client, db):
    await _switch(db, safe_ban=True)
    mid, mh = await make_user(db, staff_role="moderator")
    sh = await _staff_session(client, db, mid, mh)
    assert (await client.get("/api/v1/admin/features", headers=mh)).status_code == 403
    assert (await client.get("/api/v1/admin/features", headers=sh)).status_code == 403
    adm, _ = await make_user(db, user_type="admin")
    r = await client.post(f"{M}/actions/freeze", json={"user_id": adm, "reason": "spam"}, headers=sh)
    assert r.status_code == 403
    big, _ = await make_user(db, followers_count=safe_mod.BIG_ACCOUNT_FOLLOWERS)
    r = await client.post(f"{M}/actions/freeze", json={"user_id": big, "reason": "spam"}, headers=sh)
    assert r.status_code == 403 and "admin" in r.text
    assert (await client.post(f"{M}/staff", json={"user_id": mid, "role": "owner"}, headers=sh)).status_code == 403
    # A staff_role written by hand on an ordinary account never gives admin powers.
    fake, fh = await make_user(db, staff_role="owner")
    assert staff.role_of(await db.users.find_one({"_id": ObjectId(fake)})) == "moderator"


async def test_SM10_staff_must_be_adults_and_owner_bootstrap(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)                                   # no owner yet: admins act as owners
    teen, _ = await make_user(db, date_of_birth=datetime.now() - timedelta(days=365 * 16))
    r = await client.post(f"{M}/staff", json={"user_id": teen, "role": "moderator"}, headers=ah)
    assert r.status_code == 400
    nodob, _ = await make_user(db, date_of_birth=None)
    assert (await client.post(f"{M}/staff", json={"user_id": nodob, "role": "moderator"}, headers=ah)).status_code == 400
    ok, _ = await make_user(db)
    assert (await client.post(f"{M}/staff", json={"user_id": ok, "role": "moderator"}, headers=ah)).status_code == 200
    assert (await client.post(f"{M}/staff", json={"user_id": ok, "role": "admin"}, headers=ah)).status_code == 400
    assert (await client.post(f"{M}/staff", json={"user_id": aid, "role": "owner"}, headers=ah)).status_code == 200
    a2, a2h = await _admin(db)
    # Now there's an owner: other admins are admins.
    assert (await client.post(f"{M}/staff", json={"user_id": ok, "role": None}, headers=a2h)).status_code == 403


async def test_SM11_rate_limit_stops_a_burst_and_tells_owners(client, db, calls):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    await db.users.update_one({"_id": ObjectId(aid)}, {"$set": {"expo_push_tokens": ["ExponentPushToken[o]"]}})
    now = datetime.utcnow()
    await db.mod_actions.insert_many([{"kind": "limit", "by": ObjectId(aid), "at": now, "reason": "spam"}
                                      for _ in range(safe_mod.STAFF_ACTIONS_PER_HOUR)])
    t, *_ = await _setup_target(db)
    r = await client.post(f"{M}/actions/limit", json={"user_id": t, "reason": "spam", "hours": 24}, headers=ah)
    assert r.status_code == 429
    assert len(calls["pushes"]) == 1


# ------------------------------------------------------------------ limits
async def test_SM12_limited_can_still_report_block_and_appeal_but_not_post(client, db):
    await _switch(db, safe_ban=True, comment_guard=True)
    aid, ah = await _admin(db)
    t, th, v1, v2, sched, other, oh, ov = await _setup_target(db)
    c = await _comment(client, oh, ov, "x")
    r = await client.post(f"{M}/actions/limit", json={"user_id": t, "reason": "spam", "hours": 24}, headers=ah)
    assert r.status_code == 200
    r = await client.post("/api/v1/", json={"content": "hi", "video_id": ov}, headers=th)
    assert r.status_code == 403 and "limited" in r.text
    old = (await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(days=30)))
    assert (await client.post(f"/api/v1/{c}/report", json={"reason": "spam"}, headers=th)).status_code == 200
    assert (await client.post(f"/api/v1/users/block_user/{other}", headers=th)).status_code == 204
    assert (await client.delete(f"/api/v1/videos/{v1}", headers=th)).status_code == 204
    act = await db.mod_actions.find_one({"kind": "limit"})
    assert (await client.post(f"{M}/me/appeals", json={"decision_id": str(act["_id"]), "text": "I didn't spam"},
                              headers=th)).status_code == 200
    # Expired: everything works again.
    await db.users.update_one({"_id": ObjectId(t)}, {"$set": {"mod_state.until": datetime.utcnow() - timedelta(minutes=1)}})
    assert (await client.post("/api/v1/", json={"content": "hi", "video_id": ov}, headers=th)).status_code == 200
    assert "mod_state" not in await db.users.find_one({"_id": ObjectId(t)})
    assert old


# ------------------------------------------------------------------ hiding
async def test_SM13_hidden_comment_is_hidden_for_everyone_even_with_guard_off(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, th, v1, *_ = await _setup_target(db)
    c = await _comment(client, th, v1, "rude")
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["comments_count"] == 1
    r = await client.post(f"{M}/actions/hide-comment", json={"comment_id": c, "reason": "harassment"}, headers=ah)
    assert r.status_code == 200
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["comments_count"] == 0
    assert (await client.get(f"/api/v1/video/{v1}", headers=th)).json() == []          # author too
    assert (await client.post(f"{M}/actions/hide-comment", json={"comment_id": c, "reason": "spam"},
                              headers=ah)).status_code == 409                          # already handled
    await client.post(f"{M}/actions/{r.json()['id']}/undo", headers=ah)
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["comments_count"] == 1


async def test_SM14_hidden_post_keeps_media_and_survives_unfreeze(client, db, monkeypatch):
    await _switch(db, safe_ban=True)
    import app.services.video_deletion as vd
    called = []

    async def no(*a, **k):
        called.append(a)
    monkeypatch.setattr(vd, "soft_delete_video", no)
    aid, ah = await _admin(db)
    t, th, v1, v2, *_ = await _setup_target(db)
    assert (await client.post(f"{M}/actions/hide-post", json={"post_id": v1, "reason": "nudity"}, headers=ah)).status_code == 200
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["privacy"] == "private" and called == []
    await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "nudity"}, headers=ah)
    await client.post(f"{M}/actions/unfreeze", json={"user_id": t}, headers=ah)
    assert (await db.videos.find_one({"_id": ObjectId(v1)}))["privacy"] == "private"     # still hidden by staff
    assert (await db.videos.find_one({"_id": ObjectId(v2)}))["privacy"] == "followers"


# ------------------------------------------------------------------ wipe and evidence
async def test_SM15_wipe_only_by_owner_on_web_after_30_days_and_never_evidence(client, db, monkeypatch):
    await _switch(db, safe_ban=True)
    wiped = []

    async def fake_old_ban(db_, oid):
        wiped.append(oid)
    monkeypatch.setattr(safe_mod, "old_ban", fake_old_ban)
    aid, ah = await _admin(db)
    await db.users.update_one({"_id": ObjectId(aid)}, {"$set": {"staff_role": "owner"}})
    t, *_ = await _setup_target(db)
    uname = (await db.users.find_one({"_id": ObjectId(t)}))["username"]
    await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "scam"}, headers=ah)
    assert (await client.post(f"{M}/users/{t}/wipe", json={"confirm": uname}, headers=ah)).status_code == 409
    await db.users.update_one({"_id": ObjectId(t)}, {"$set": {"mod_state.since": datetime.utcnow() - timedelta(days=31)}})
    await client.post(f"{M}/users/{t}/evidence", json={"on": True}, headers=ah)
    assert (await client.post(f"{M}/users/{t}/wipe", json={"confirm": uname}, headers=ah)).status_code == 409
    await client.post(f"{M}/users/{t}/evidence", json={"on": False}, headers=ah)
    assert (await client.post(f"{M}/users/{t}/wipe", json={"confirm": "wrong"}, headers=ah)).status_code == 400
    app_h = {"Authorization": f"Bearer {create_access_token({'sub': aid})}"}
    sh = await _staff_session(client, db, aid, app_h)
    assert (await client.post(f"{M}/users/{t}/wipe", json={"confirm": uname}, headers=sh)).status_code == 403
    assert (await client.post(f"{M}/users/{t}/wipe", json={"confirm": uname}, headers=ah)).status_code == 200
    assert wiped == [ObjectId(t)]


# ------------------------------------------------------------------ warnings
async def test_SM16_warnings_ladder_notice_and_appeal(client, db):
    await _switch(db, safe_ban=True, warnings=True)
    aid, ah = await _admin(db)
    t, th, v1, *_ = await _setup_target(db)
    s = (await client.get(f"{M}/suggest", params={"user_id": t, "reason": "spam"}, headers=ah)).json()
    assert s["step"] == "warn"
    r = await client.post(f"{M}/actions/warn", json={"user_id": t, "reason": "spam", "post_id": v1}, headers=ah)
    assert r.status_code == 200
    # Not shown during the undo window (and its random delay).
    assert (await client.get(f"{M}/me/notices", headers=th)).json()["items"] == []
    await db.warnings.update_many({}, {"$set": {"visible_after": datetime.utcnow() - timedelta(seconds=1)}})
    n = (await client.get(f"{M}/me/notices", headers=th)).json()
    item, = n["items"]
    assert item["rule"] == "spam" and item["copy"]["kind"] == "post" and "never ask" in item["say"]
    assert "by" not in item and aid not in str(n)                    # staff never named
    assert (await client.get(f"{M}/suggest", params={"user_id": t, "reason": "spam"}, headers=ah)).json()["step"] == "limit"
    assert (await client.get(f"{M}/suggest", params={"user_id": t, "reason": "child_safety"}, headers=ah)).json()["step"] == "suspend_silent"
    assert (await client.post(f"{M}/me/notices/{item['id']}/seen", headers=th)).status_code == 200
    assert (await client.get(f"{M}/me/notices", headers=th)).json()["items"] == []
    assert (await client.post(f"{M}/me/appeals", json={"decision_id": item["action"], "text": "It was a joke"},
                              headers=th)).status_code == 200
    assert (await client.post(f"{M}/me/appeals", json={"decision_id": item["action"], "text": "again"},
                              headers=th)).status_code == 409
    ap = (await client.get(f"{M}/appeals", headers=ah)).json()["items"][0]
    assert (await client.post(f"{M}/appeals/{ap['id']}", json={"outcome": "reversed"}, headers=ah)).status_code == 200
    assert (await client.get(f"{M}/suggest", params={"user_id": t, "reason": "spam"}, headers=ah)).json()["step"] == "warn"


async def test_SM17_child_safety_is_never_a_warning_and_teens_get_gentle_words(client, db):
    await _switch(db, safe_ban=True, warnings=True)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    r = await client.post(f"{M}/actions/warn", json={"user_id": t, "reason": "child_safety"}, headers=ah)
    assert r.status_code == 400
    teen, teen_h = await make_user(db, date_of_birth=datetime.now() - timedelta(days=365 * 15))
    await client.post(f"{M}/actions/warn", json={"user_id": teen, "reason": "harassment"}, headers=ah)
    w = await db.warnings.find_one({"user": ObjectId(teen)})
    assert w["teen"] is True and "okay" in w["text"] and "allowed" not in w["text"]


async def test_SM18_warnings_need_their_switch_and_reports_never_warn(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    assert (await client.post(f"{M}/actions/warn", json={"user_id": t, "reason": "spam"}, headers=ah)).status_code == 404
    _, ah2 = await make_user(db, user_type="admin")
    r = await client.put("/api/v1/admin/features", json={"key": "warnings", "on": True}, headers=_web(_))
    assert r.status_code in (200, 409)


async def test_SM19_a_suspended_person_can_read_why_and_appeal(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, th, *_ = await _setup_target(db)
    await client.post(f"{M}/actions/freeze", json={"user_id": t, "reason": "scam"}, headers=ah)
    d = (await client.get(f"{M}/me/decisions", headers=th)).json()["decision"]
    assert d["kind"] == "freeze" and d["reason"] == "scam" and "by" not in d
    assert (await client.get(f"{M}/me/notices", headers=th)).json()["state"]["state"] == "frozen"
    assert (await client.post(f"{M}/me/appeals", json={"decision_id": d["id"], "text": "Not a scam, it was a real sale"},
                              headers=th)).status_code == 200
    assert (await client.get(f"{M}/me", headers=th)).status_code == 403   # nothing else


# ------------------------------------------------------------------ staff_view and staff_quiet
async def test_SM20_staff_see_past_blocks_with_a_note_others_dont(client, db):
    await _switch(db, safe_ban=True, comment_guard=True, staff_view=True)
    aid, _ = await make_user(db, user_type="admin")
    app_admin = {"Authorization": f"Bearer {create_access_token({'sub': aid})}"}
    bad, bh = await make_user(db)
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)
    await client.post(f"/api/v1/users/block_user/{aid}", headers=bh)        # the bad actor blocks Wano Team
    c = await _comment(client, bh, vid, "nasty")
    rows = (await client.get(f"/api/v1/video/{vid}", headers=app_admin)).json()
    assert [r["_id"] for r in rows] == [c] and rows[0]["staff_note"] == "Hidden from you by a block"
    plain, ph = await make_user(db)
    await client.post(f"/api/v1/users/block_user/{plain}", headers=bh)
    assert (await client.get(f"/api/v1/video/{vid}", headers=ph)).json() == []
    await _switch(db, staff_view=False)
    assert (await client.get(f"/api/v1/video/{vid}", headers=app_admin)).json() == []


async def test_SM21_staff_views_and_recommender_events_count_nowhere(client, db, monkeypatch):
    await _switch(db, staff_quiet=True)
    aid, ah = await make_user(db, user_type="admin")
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    from app.services import trending
    assert await trending.count_view_once(db, viewer=aid, video_id=vid, ip=None) is False
    user, _ = await make_user(db)
    assert await trending.count_view_once(db, viewer=user, video_id=vid, ip=None) is True
    from app.services import recombee_service
    from recombee_api_client.api_requests import SetViewPortion, AddRating, RecommendItemsToUser
    assert await recombee_service.staff_skip(SetViewPortion(aid, vid, 0.5)) is True
    assert await recombee_service.staff_skip(AddRating(aid, vid, 1.0)) is True
    assert await recombee_service.staff_skip(SetViewPortion(user, vid, 0.5)) is False
    assert await recombee_service.staff_skip(RecommendItemsToUser(aid, 5)) is False      # reads are fine
    await _switch(db, staff_quiet=False)
    assert await trending.count_view_once(db, viewer=aid, video_id=str(ObjectId()), ip=None) is True


async def test_SM22_account_card_masks_and_logs_every_look(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    t, *_ = await _setup_target(db)
    await db.users.update_one({"_id": ObjectId(t)}, {"$set": {"email": "amara@example.com", "phone_number": "+256700123456"}})
    card = (await client.get(f"{M}/users/{t}/card", headers=ah)).json()
    assert card["email"] == "a•••@example.com" and card["phone"] == "•••456"
    assert "amara" not in str(card) and "700123" not in str(card)
    assert await db.staff_views.count_documents({"user": ObjectId(t)}) == 1


async def test_SM23_inbox_puts_child_safety_first(client, db):
    await _switch(db, safe_ban=True, comment_guard=True)
    aid, ah = await _admin(db)
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)
    a = await _comment(client, ch, vid, "one")
    b = await _comment(client, ch, vid, "two")
    _, rh = await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(days=30))
    await client.post(f"/api/v1/{a}/report", json={"reason": "spam"}, headers=rh)
    await client.post(f"/api/v1/{b}/report", json={"reason": "child_safety"}, headers=rh)
    items = (await client.get(f"{M}/inbox", headers=ah)).json()["items"]
    assert items[0]["id"] == b and items[0]["urgent"] and items[0]["reporter_days"] == [30]


async def test_SM24_a_token_that_merely_expires_is_not_an_admin_page_sign_in(client, db):
    await _switch(db, safe_ban=True)
    aid, _ = await make_user(db, user_type="admin")
    exp = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp())
    h = {"Authorization": f"Bearer {create_access_token({'sub': aid, 'exp': exp})}"}
    assert (await client.get(f"{M}/inbox", headers=h)).status_code == 401
    r = await client.post("/api/v1/admin/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
