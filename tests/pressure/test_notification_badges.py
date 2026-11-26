"""Unread state must be private, monotonic, and independent of chat read state."""
import asyncio
from datetime import datetime, timedelta
import pytest
from bson import ObjectId
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
ROOT = "/api/v1/notifications"


async def activity(db, uid, **extra):
    row = {"recipient_id": ObjectId(uid), "user_id": ObjectId(), "type": "follow", "date": datetime.utcnow()}
    row.update(extra)
    return (await db.notifications.insert_one(row)).inserted_id


async def start(client, headers):
    r = await client.get(ROOT + "/unread", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_old_activity_does_not_create_a_backlog_badge(client, db):
    uid, h = await make_user(db)
    await activity(db, uid, date=datetime.utcnow() - timedelta(days=100))
    assert (await start(client, h))["activity"] == 0
    await activity(db, uid)
    assert (await start(client, h))["activity"] == 1


async def test_read_only_acknowledges_delivered_ids_not_new_arrivals(client, db):
    uid, h = await make_user(db)
    await start(client, h)
    first = await activity(db, uid)
    second = await activity(db, uid)
    r = await client.post(ROOT + "/read", headers=h, json={"ids": [str(first)]})
    assert r.status_code == 200, r.text
    assert (await start(client, h))["activity"] == 1
    assert (await db.notifications.find_one({"_id": first})).get("read_at")
    assert not (await db.notifications.find_one({"_id": second})).get("read_at")


async def test_other_account_cannot_read_or_clear_your_activity(client, db):
    uid, h = await make_user(db)
    _, other = await make_user(db)
    await start(client, h)
    nid = await activity(db, uid)
    await client.post(ROOT + "/read", headers=other, json={"ids": [str(nid)]})
    assert (await start(client, h))["activity"] == 1
    assert (await start(client, other))["activity"] == 0
    assert (await client.get(ROOT + "/unread")).status_code == 401


async def test_100_repeated_reads_are_idempotent(client, db):
    uid, h = await make_user(db)
    await start(client, h)
    nid = await activity(db, uid)
    rs = await asyncio.gather(*[client.post(ROOT + "/read", headers=h, json={"ids": [str(nid)]}) for _ in range(100)])
    assert all(r.status_code == 200 for r in rs)
    assert (await start(client, h))["total"] == 0


async def test_activity_read_does_not_clear_unread_messages(client, db):
    uid, h = await make_user(db)
    other, _ = await make_user(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1", "features": {"messages": True}})
    await db.conversations.insert_one({"members": [ObjectId(uid), ObjectId(other)], "state": "active",
        "last_message": {"text": "test"}, "m": {uid: {"unread": 3}}})
    await start(client, h)
    nid = await activity(db, uid)
    summary = await start(client, h)
    assert summary["messages"] == 3 and summary["activity"] == 1 and summary["total"] == 4
    await client.post(ROOT + "/read", headers=h, json={"ids": [str(nid)]})
    assert (await start(client, h))["total"] == 3


async def test_invalid_ids_and_oversized_batches_are_rejected(client, db):
    _, h = await make_user(db)
    for ids in (["bad"], [str(ObjectId())] * 101):
        assert (await client.post(ROOT + "/read", headers=h, json={"ids": ids})).status_code == 422


async def test_unread_endpoint_never_caches_private_counts(client, db):
    _, h = await make_user(db)
    r = await client.get(ROOT + "/unread", headers=h)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"


async def test_push_rechecks_token_owner_after_account_switch(client, db, calls):
    from app.services.notification_badges import send_user_push
    uid, _ = await make_user(db, expo_push_tokens=[])
    await send_user_push(uid, "ExpoPushToken[transferred]", "test", {}, "test", None)
    assert not calls["pushes"]


async def test_push_carries_actual_count_including_zero(client, db, calls):
    from app.services.notification_badges import send_user_push
    uid, h = await make_user(db, expo_push_tokens=["ExpoPushToken[test]"], badge_capable_tokens=["ExpoPushToken[test]"])
    await start(client, h)
    nid = await activity(db, uid)
    await send_user_push(uid, "ExpoPushToken[test]", "test", {}, "test", None)
    assert calls["pushes"][-1][2]["badge"] == 1
    await client.post(ROOT + "/read", headers=h, json={"ids": [str(nid)]})
    await send_user_push(uid, "ExpoPushToken[test]", "test", {}, "test", None)
    assert calls["pushes"][-1][2]["badge"] == 0


async def test_older_app_push_does_not_gain_a_badge_it_cannot_clear(client, db, calls):
    from app.services.notification_badges import send_user_push
    uid, _ = await make_user(db, expo_push_tokens=["ExpoPushToken[old]"])
    await send_user_push(uid, "ExpoPushToken[old]", "test")
    assert calls["pushes"][-1][2]["badge"] is None


async def test_shared_message_poll_includes_activity_without_scanning_messages_twice(client, db, monkeypatch):
    from app.services import messages
    uid, h = await make_user(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1", "features": {"messages": True}})
    await start(client, h)
    await activity(db, uid)
    scans = []
    async def unread(*args):
        scans.append(True)
        return {"unread": 2, "requests": 1}
    monkeypatch.setattr(messages, "unread", unread)
    r = await client.get("/api/v1/messages/unread?badges=true", headers=h)
    assert r.status_code == 200 and r.json() == {"unread": 2, "requests": 1, "activity": 1, "total": 4}
    assert len(scans) == 1


async def test_push_badge_capability_moves_with_the_current_account(client, db):
    a, ah = await make_user(db)
    b, bh = await make_user(db)
    body = {"expo_push_token": "ExpoPushToken[same]", "badge_version": 1}
    for h in (ah, bh):
        assert (await client.post("/api/v1/users/me/push-token", headers=h, json=body)).status_code == 200
    assert not (await db.users.find_one({"_id": ObjectId(a)})).get("badge_capable_tokens")
    assert (await db.users.find_one({"_id": ObjectId(b)}))["badge_capable_tokens"] == [body["expo_push_token"]]
