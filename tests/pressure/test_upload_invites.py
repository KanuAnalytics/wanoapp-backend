"""Admin > Failed uploads: invite creators whose video never uploaded to post
it again. Only admins; only when pressed; each person once, ever; never
someone who posted since, or a banned or deleted account. Attacks first."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
G = "/api/v1/admin/failed-uploads"
P = "/api/v1/admin/failed-uploads/invite"


async def _failed(db, creator, days_ago=3):
    vid = await make_video(db, creator, isReadyToStream=False)
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {
        "upload_check": {"state": "failed", "reason": "timeout"},
        "created_at": datetime.utcnow() - timedelta(days=days_ago)}})
    return vid


async def _setup(db):
    a, _ = await make_user(db, expo_push_tokens=["ExponentPushToken[a]"])       # invite
    b, _ = await make_user(db)                                                  # invite, no push
    c, _ = await make_user(db, expo_push_tokens=["ExponentPushToken[c]"])       # posted since: skip
    d, _ = await make_user(db, is_banned=True, expo_push_tokens=["ExponentPushToken[d]"])  # skip
    e, _ = await make_user(db, expo_push_tokens=["ExponentPushToken[e]"])       # too long ago: skip
    for u in (a, a, b, c, d):
        await _failed(db, u)
    await _failed(db, e, days_ago=90)
    ok = await make_video(db, c, isReadyToStream=True, is_active=True)
    await db.videos.update_one({"_id": ObjectId(ok)}, {"$set": {"created_at": datetime.utcnow()}})
    return a, b, c, d, e


async def test_UI1_admins_only_and_nothing_sent_by_looking(client, db, calls):
    await _setup(db)
    _, uh = await make_user(db)
    _, ah = await make_user(db, user_type="admin")
    assert (await client.get(G, headers=uh)).status_code == 403
    assert (await client.post(P, headers=uh, json={"confirm": True})).status_code == 403
    r = (await client.get(G, headers=ah)).json()
    assert r["failed_posts"] == 5 and r["creators"] == 4 and r["posted_since"] == 1 and r["inactive"] == 1
    assert r["to_invite"] == 2 and r["reachable_by_push"] == 1 and "_ready" not in r
    assert calls["pushes"] == [] and await db.upload_invites.count_documents({}) == 0


async def test_UI2_needs_the_explicit_confirm(client, db, calls):
    await _setup(db)
    _, ah = await make_user(db, user_type="admin")
    for bad in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": True, "message": "hi"}):
        assert (await client.post(P, headers=ah, json=bad)).status_code == 422
    assert calls["pushes"] == []


async def test_UI3_sends_once_to_the_right_people(client, db, calls):
    a, b, c, d, e = await _setup(db)
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(P, headers=ah, json={"confirm": True})
    assert r.json() == {"invited": 2, "pushes": 1, "batches_sent": 1}
    (name, args, _), = calls["pushes"]
    msgs = args[0]
    assert name == "send_push_batch_data" and [m["to"] for m in msgs] == ["ExponentPushToken[a]"]
    assert msgs[0]["data"] == {"screen": "profile_v2", "type": "upload_invite"}
    assert {x["_id"] for x in await db.upload_invites.find({}).to_list(10)} == {ObjectId(a), ObjectId(b)}
    # Second press: nobody left, nothing sent again.
    assert (await client.post(P, headers=ah, json={"confirm": True})).status_code == 409
    assert len(calls["pushes"]) == 1
    assert (await client.get(G, headers=ah)).json()["already_invited"] == 2
    assert await db.admin_log.count_documents({"action": "upload_invites"}) == 1


async def test_UI4_two_presses_at_once_still_send_once(client, db, calls):
    await _setup(db)
    _, ah = await make_user(db, user_type="admin")
    rs = await asyncio.gather(*(client.post(P, headers=ah, json={"confirm": True}) for _ in range(4)))
    assert sum(r.json().get("invited", 0) for r in rs if r.status_code == 200) == 2
    assert sum(len(c[1][0]) for c in calls["pushes"]) == 1


async def test_UI5_a_push_failure_never_undoes_the_record(client, db, calls, monkeypatch):
    await _setup(db)
    import app.services.expo as expo

    def boom(*a, **k):
        raise RuntimeError("expo down")
    monkeypatch.setattr(expo, "send_push_batch_data", boom)
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(P, headers=ah, json={"confirm": True})
    assert r.status_code == 200 and r.json()["batches_sent"] == 0
    assert await db.upload_invites.count_documents({}) == 2      # never sent twice, even after a failure
