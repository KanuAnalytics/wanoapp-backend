"""Fixes after the 2.3.59 review: comment_plus can't run without Comment safety,
the teen row never promises protections that are off, and "Involves a child"
reports reach staff at once without showing anything on a lock screen."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.services import staff_alerts
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
C = "/api/v1/"


async def _features(db, **on):
    await db.config.insert_one({"features": dict(on)})


async def _old_user(db, **kw):
    return await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(days=30), **kw)


async def test_PA1_comment_plus_needs_comment_guard(client, db):
    await _features(db)
    _, ah = await make_user(db, user_type="admin")
    r = await client.put("/api/v1/admin/features", json={"key": "comment_plus", "on": True}, headers=ah)
    assert r.status_code == 409 and "comment_guard" in r.text
    assert (await client.put("/api/v1/admin/features", json={"key": "comment_guard", "on": True}, headers=ah)).status_code == 200
    assert (await client.put("/api/v1/admin/features", json={"key": "comment_plus", "on": True}, headers=ah)).status_code == 200
    f = (await client.get("/api/v1/config/features")).json()
    assert f["comment_plus"] and f["comment_guard"]
    # Turning Comment safety off turns the new comments off too.
    await client.put("/api/v1/admin/features", json={"key": "comment_guard", "on": False}, headers=ah)
    f = (await client.get("/api/v1/config/features")).json()
    assert f["comment_plus"] is False and f["comment_guard"] is False


async def test_PA2_a_stored_plus_without_guard_is_off_to_apps_and_server(client, db):
    # Set before this fix (or by hand): apps and the server both treat it as off.
    await _features(db, comment_plus=True)
    assert (await client.get("/api/v1/config/features")).json()["comment_plus"] is False
    from app.services import comment_guard
    assert await comment_guard.plus_on(db) is False
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    r = await client.get(f"{C}video/{vid}", params={"authenticated": "false"})
    assert r.status_code == 401                      # signed-out reading still needs the guard


async def test_PA3_teen_row_only_when_rules_are_enforced(client, db):
    await _features(db, comment_guard=True, comment_plus=True)
    born = datetime.now() - timedelta(days=365 * 14 + 30)
    _, th = await make_user(db, date_of_birth=born)
    assert (await client.get(f"{C}me/teen-comments", headers=th)).json()["teen"] is False
    await db.config.update_one({}, {"$set": {"features.teen_comments": True}})
    assert (await client.get(f"{C}me/teen-comments", headers=th)).json() == {"teen": True, "who": "friends", "locked": True}


async def test_PA4_child_safety_reason_goes_first_and_alerts_staff_once(client, db, calls):
    await _features(db, comment_guard=True, safe_ban=True)
    _, adm = await make_user(db, user_type="admin", expo_push_tokens=["ExponentPushToken[staff]"])
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)
    r = await client.post(f"{C}", json={"content": "first", "video_id": vid}, headers=ch)
    c1 = r.json()["_id"]
    r = await client.post(f"{C}", json={"content": "second", "video_id": vid}, headers=ch)
    c2 = r.json()["_id"]
    _, rh = await _old_user(db)
    _, rh2 = await _old_user(db)
    assert (await client.post(f"{C}{c1}/report", json={"reason": "spam"}, headers=rh)).status_code == 200
    assert (await client.post(f"{C}{c2}/report", json={"reason": "child_safety"}, headers=rh)).status_code == 200
    assert (await client.post(f"{C}{c2}/report", json={"reason": "child_safety"}, headers=rh2)).status_code == 200
    pushes = calls.get("pushes", [])
    assert len(pushes) == 1
    (name, args, _), = pushes
    msg = args[0][0]
    assert msg["to"] == "ExponentPushToken[staff]" and msg["body"] == staff_alerts.BODY
    assert "second" not in str(msg) and creator not in str(msg)      # nothing on the lock screen
    items = (await client.get(f"{C}admin/comment-reports", headers=adm)).json()["items"]
    assert items[0]["comment_id"] == c2 and items[0]["urgent"] is True and items[1]["urgent"] is False


async def test_PA5_alert_flood_is_capped(db, calls):
    await _features(db, safe_ban=True)
    await make_user(db, user_type="admin", expo_push_tokens=["ExponentPushToken[s]"])
    sent = [await staff_alerts.urgent(db, f"comment:{ObjectId()}") for _ in range(staff_alerts.ALERTS_PER_HOUR + 4)]
    assert sum(sent) == staff_alerts.ALERTS_PER_HOUR
    assert await db.staff_alerts.count_documents({}) == staff_alerts.ALERTS_PER_HOUR + 4   # all still recorded


async def test_PA6_push_failure_never_breaks_reporting(client, db, monkeypatch):
    await _features(db, comment_guard=True)
    await make_user(db, user_type="admin", expo_push_tokens=["ExponentPushToken[s]"])
    import app.services.expo as expo

    def boom(*a, **k):
        raise RuntimeError("expo down")
    monkeypatch.setattr(expo, "send_push_batch_data", boom)
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)
    c = (await client.post(f"{C}", json={"content": "x", "video_id": vid}, headers=ch)).json()["_id"]
    _, rh = await _old_user(db)
    assert (await client.post(f"{C}{c}/report", json={"reason": "child_safety"}, headers=rh)).status_code == 200


async def test_PA7_video_child_exploitation_report_alerts_staff(client, db, calls):
    await _features(db, safe_ban=True)
    await make_user(db, user_type="admin", expo_push_tokens=["ExponentPushToken[s]"])
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, rh = await _old_user(db)
    r = await client.post(f"/api/v1/reports/videos/{vid}/report", json={"category": "child_exploitation"}, headers=rh)
    assert r.status_code == 201, r.text
    assert len(calls.get("pushes", [])) == 1
    _, rh2 = await _old_user(db)
    await client.post(f"/api/v1/reports/videos/{vid}/report", json={"category": "spam"}, headers=rh2)
    assert len(calls.get("pushes", [])) == 1


async def test_PA8_no_staff_pushes_while_safe_moderation_is_off(client, db, calls):
    await make_user(db, user_type="admin", expo_push_tokens=["ExponentPushToken[s]"])
    assert await staff_alerts.urgent(db, f"comment:{ObjectId()}") is False
    assert calls.get("pushes", []) == []
