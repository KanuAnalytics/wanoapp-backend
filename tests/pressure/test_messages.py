"""In-house messaging v1: text and shared posts between people who follow each
other; everyone else lands in Requests. Blocks, under-18s, banned accounts,
rate limits, links in requests, reports to the admin page. Off unless the
"messages" switch is on. Attacks written before the code."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

M = "/api/v1/messages"


async def _on(db, on=True):
    await db.config.insert_one({"features": {"messages": on}})


async def _follow(db, a, b):
    await db.users.update_one({"_id": ObjectId(a)}, {"$addToSet": {"following": ObjectId(b)}})
    await db.users.update_one({"_id": ObjectId(b)}, {"$addToSet": {"followers": ObjectId(a)}})


async def _mutual(db, a, b):
    await _follow(db, a, b)
    await _follow(db, b, a)


async def _open(client, h, other):
    return await client.post(f"{M}/conversations", json={"user_id": other}, headers=h)


async def _send(client, h, cid, text="hi", **extra):
    return await client.post(f"{M}/conversations/{cid}/messages", json={"text": text, **extra}, headers=h)


async def _pair(client, db, mutual=True, **b_extra):
    a, ah = await make_user(db, username="amina", display_name="Amina")
    b, bh = await make_user(db, username="kofi", display_name="Kofi", **b_extra)
    if mutual:
        await _mutual(db, a, b)
    r = await _open(client, ah, b)
    return a, ah, b, bh, r


async def _inbox(client, h, folder="inbox"):
    r = await client.get(f"{M}/conversations", params={"folder": folder}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ---------------------------------------------------------------- switch, auth

async def test_MS1_switch_off_everything_is_closed(client, db):
    a, ah = await make_user(db)
    b, _ = await make_user(db)
    await _mutual(db, a, b)
    r = await _open(client, ah, b)
    assert r.status_code == 404
    assert (await client.get(f"{M}/conversations", headers=ah)).status_code == 404
    assert (await client.get(f"{M}/unread", headers=ah)).status_code == 404
    await _on(db, False)
    assert (await _open(client, ah, b)).status_code == 404


async def test_MS2_logged_out_gets_nothing(client, db):
    await _on(db)
    assert (await client.get(f"{M}/conversations")).status_code == 401
    assert (await client.get(f"{M}/conversations", params={"authenticated": "false"})).status_code == 401


async def test_MS3_messages_switch_listed_off_by_default(client, db):
    r = await client.get("/api/v1/config/features")
    assert r.json()["messages"] is False


# ---------------------------------------------------------------- mutual follows chat freely

async def test_MS4_mutual_follows_chat_both_ways_and_see_it_in_inbox(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    assert r.status_code == 200, r.text
    conv = r.json()
    assert conv["status"] == "active" and conv["other"]["username"] == "kofi"
    cid = conv["id"]
    m = await _send(client, ah, cid, "  Habari Kofi  ")
    assert m.status_code == 201, m.text
    assert m.json()["text"] == "Habari Kofi" and m.json()["mine"] is True
    assert (await _send(client, bh, cid, "Poa!")).status_code == 201
    inbox = await _inbox(client, ah)
    assert len(inbox) == 1 and inbox[0]["last_message"]["text"] == "Poa!"
    assert inbox[0]["unread"] == 1
    msgs = (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()
    assert [x["text"] for x in msgs["items"]] == ["Poa!", "Habari Kofi"]    # newest first
    assert [x["mine"] for x in msgs["items"]] == [True, False]


async def test_MS5_opening_twice_gives_the_same_conversation(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    r2 = await _open(client, bh, a)
    r3 = await _open(client, ah, b)
    assert r.json()["id"] == r2.json()["id"] == r3.json()["id"]
    assert await db.conversations.count_documents({}) == 1


async def test_MS6_opening_in_parallel_still_one_conversation(client, db):
    await _on(db)
    a, ah = await make_user(db)
    b, bh = await make_user(db)
    await _mutual(db, a, b)
    rs = await asyncio.gather(*[_open(client, ah if i % 2 else bh, b if i % 2 else a) for i in range(10)])
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert await db.conversations.count_documents({}) == 1


async def test_MS7_cant_message_yourself_or_nobody(client, db):
    await _on(db)
    a, ah = await make_user(db)
    assert (await _open(client, ah, a)).status_code == 400
    assert (await _open(client, ah, str(ObjectId()))).status_code == 404
    assert (await _open(client, ah, "not-an-id")).status_code == 400
    assert (await client.post(f"{M}/conversations", json={"user_id": {"$ne": None}}, headers=ah)).status_code == 422


# ---------------------------------------------------------------- requests

async def test_MS8_not_mutual_goes_to_requests_and_accepting_moves_it(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    await _follow(db, a, b)                                  # one way is not enough
    r = await _open(client, ah, b)
    cid = r.json()["id"]
    assert r.json()["status"] == "request_sent"
    assert (await _send(client, ah, cid, "Hello, I love your videos")).status_code == 201
    assert await _inbox(client, bh) == []
    req = await _inbox(client, bh, "requests")
    assert len(req) == 1 and req[0]["status"] == "request_received"
    assert (await client.get(f"{M}/unread", headers=bh)).json() == {"unread": 0, "requests": 1}
    # the sender sees it in their inbox as sent
    assert (await _inbox(client, ah))[0]["status"] == "request_sent"
    assert (await client.post(f"{M}/conversations/{cid}/accept", headers=bh)).status_code == 200
    assert (await _inbox(client, bh))[0]["status"] == "active"
    assert await _inbox(client, bh, "requests") == []


async def test_MS9_replying_to_a_request_accepts_it(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    assert (await _send(client, bh, cid, "hello")).status_code == 201
    assert (await _inbox(client, bh))[0]["status"] == "active"


async def test_MS10_until_accepted_the_sender_can_send_only_3(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    for i in range(3):
        assert (await _send(client, ah, cid, f"m{i}")).status_code == 201
    r4 = await _send(client, ah, cid, "m3")
    assert r4.status_code == 429 and "accept" in r4.json()["detail"].lower()


async def test_MS11_declined_request_disappears_and_sender_isnt_told(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    assert (await client.post(f"{M}/conversations/{cid}/decline", headers=bh)).status_code == 200
    assert await _inbox(client, bh, "requests") == []
    assert (await _inbox(client, ah))[0]["status"] == "request_sent"     # looks unchanged to the sender
    # further messages from the sender are swallowed quietly (no push, not shown)
    calls["pushes"].clear()
    r2 = await _send(client, ah, cid, "hello??")
    assert r2.status_code in (201, 429)
    assert calls["pushes"] == []
    assert await _inbox(client, bh, "requests") == []
    assert await _inbox(client, bh) == []


async def test_MS12_links_not_allowed_in_requests(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    for bad in ["win money http://x.co", "go to www.scam.com", "bit.ly/abc", "wa.me/256700000000", "t.me/scam"]:
        rr = await _send(client, ah, cid, bad)
        assert rr.status_code == 400, bad
    # but between mutual follows links are fine
    c, ch = await make_user(db)
    await _mutual(db, a, c)
    cid2 = (await _open(client, ah, c)).json()["id"]
    assert (await _send(client, ah, cid2, "see https://wanoafrica.com")).status_code == 201


async def test_MS13_requests_per_day_are_limited_and_tighter_for_new_accounts(client, db):
    await _on(db)
    old, oh = await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(days=60))
    new, nh = await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(hours=5))
    targets = [ (await make_user(db))[0] for _ in range(12)]
    ok_new = [(await _open(client, nh, t)).status_code for t in targets[:5]]
    assert ok_new[:3] == [200, 200, 200] and ok_new[3] == 429
    ok_old = [(await _open(client, oh, t)).status_code for t in targets]
    assert ok_old[:10] == [200] * 10 and ok_old[10] == 429
    # opening one you already have doesn't count again
    assert (await _open(client, oh, targets[0])).status_code == 200


async def test_MS14_people_can_turn_off_requests(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False, message_prefs={"allow_requests": False})
    assert r.status_code == 403
    # mutual follows can still message them
    await _mutual(db, a, b)
    assert (await _open(client, ah, b)).status_code == 200


async def test_MS15_settings_endpoint(client, db):
    await _on(db)
    a, ah = await make_user(db)
    assert (await client.get(f"{M}/settings", headers=ah)).json() == {"allow_requests": True}
    assert (await client.put(f"{M}/settings", json={"allow_requests": False}, headers=ah)).status_code == 200
    assert (await client.get(f"{M}/settings", headers=ah)).json() == {"allow_requests": False}
    assert (await client.put(f"{M}/settings", json={"allow_requests": "no"}, headers=ah)).status_code == 422


# ---------------------------------------------------------------- safety

async def test_MS16_blocks_either_way_stop_everything(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    assert (await client.post(f"/api/v1/users/block_user/{b}", headers=ah)).status_code == 204
    assert (await _send(client, bh, cid, "why")).status_code == 403
    assert (await _send(client, ah, cid, "bye")).status_code == 403
    assert (await _open(client, bh, a)).status_code == 403
    assert await _inbox(client, bh) == []                   # hidden from the blocked person


async def test_MS17_under_18_cant_message_or_be_messaged(client, db):
    await _on(db)
    adult, ah = await make_user(db)
    teen, th = await make_user(db, date_of_birth=datetime.now() - timedelta(days=365 * 15))
    await _mutual(db, adult, teen)
    r1 = await _open(client, ah, teen)
    r2 = await _open(client, th, adult)
    assert r1.status_code == 403 and r2.status_code == 403
    assert "18" in r2.json()["detail"]
    almost, alh = await make_user(db, date_of_birth=datetime.now() - timedelta(days=365 * 18 - 3))
    await _mutual(db, adult, almost)
    assert (await _open(client, ah, almost)).status_code == 403
    # garbage birthdays don't crash
    odd, _ = await make_user(db, date_of_birth="2010-01-01")
    await _mutual(db, adult, odd)
    assert (await _open(client, ah, odd)).status_code in (200, 403)


async def test_MS18_turning_18_later_isnt_blocked_by_old_conversations(client, db):
    await _on(db)
    a, ah = await make_user(db)
    b, bh = await make_user(db, date_of_birth=datetime(1999, 5, 5))
    await _mutual(db, a, b)
    assert (await _open(client, ah, b)).status_code == 200


async def test_MS19_banned_or_deleted_accounts(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    await db.users.update_one({"_id": ObjectId(b)}, {"$set": {"is_active": False}})
    assert (await _send(client, ah, cid, "hello")).status_code == 403
    assert await _inbox(client, ah) == []
    assert (await _open(client, ah, b)).status_code == 404


async def test_MS20_strangers_cant_read_or_write_someone_elses_chat(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    await _send(client, ah, cid, "secret")
    _, xh = await make_user(db)
    for req in [client.get(f"{M}/conversations/{cid}/messages", headers=xh),
                client.post(f"{M}/conversations/{cid}/messages", json={"text": "x"}, headers=xh),
                client.post(f"{M}/conversations/{cid}/read", headers=xh),
                client.post(f"{M}/conversations/{cid}/accept", headers=xh),
                client.post(f"{M}/conversations/{cid}/mute", json={"muted": True}, headers=xh),
                client.delete(f"{M}/conversations/{cid}", headers=xh)]:
        assert (await req).status_code == 404
    assert (await client.get(f"{M}/conversations/not-an-id/messages", headers=ah)).status_code == 404


async def test_MS21_sender_cant_accept_their_own_request(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    assert (await client.post(f"{M}/conversations/{cid}/accept", headers=ah)).status_code == 400
    assert (await _inbox(client, ah))[0]["status"] == "request_sent"


async def test_MS22_message_size_and_shape(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    assert (await _send(client, ah, cid, "")).status_code == 400
    assert (await _send(client, ah, cid, "   \n ")).status_code == 400
    assert (await _send(client, ah, cid, "x" * 1001)).status_code == 400
    assert (await _send(client, ah, cid, "x" * 1000)).status_code == 201
    assert (await client.post(f"{M}/conversations/{cid}/messages", json={"text": {"$gt": ""}}, headers=ah)).status_code == 422
    assert (await client.post(f"{M}/conversations/{cid}/messages", json={}, headers=ah)).status_code == 400
    assert (await _send(client, ah, cid, "ok‮​")).json()["text"] == "ok"      # invisible controls dropped


async def test_MS23_sending_too_fast_is_slowed(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    codes = [(await _send(client, ah, cid, f"m{i}")).status_code for i in range(35)]
    assert codes[:30] == [201] * 30 and 429 in codes[30:]


async def test_MS24_retry_with_same_client_id_doesnt_double_send(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    r1 = await _send(client, ah, cid, "once", client_id="abc-123")
    r2 = await _send(client, ah, cid, "once", client_id="abc-123")
    assert r1.status_code == 201 and r2.status_code in (200, 201)
    assert r1.json()["id"] == r2.json()["id"]
    assert await db.messages.count_documents({}) == 1


# ---------------------------------------------------------------- sharing posts

async def test_MS25_share_a_post(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    c, _ = await make_user(db, username="maker")
    v = await make_video(db, c, thumbnail="https://cdn/x.jpg", title="Kampala nights")
    m = await client.post(f"{M}/conversations/{cid}/messages", json={"post_id": v, "text": "look"}, headers=ah)
    assert m.status_code == 201, m.text
    body = m.json()
    assert body["kind"] == "post" and body["post"]["id"] == v and body["post"]["username"] == "maker"
    assert body["text"] == "look"
    inbox = await _inbox(client, bh)
    assert inbox[0]["last_message"]["kind"] == "post"


async def test_MS26_cant_share_hidden_private_or_removed_posts(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    c, _ = await make_user(db)
    private = await make_video(db, c, privacy="private")
    removed = await make_video(db, c, moderation={"state": "removed"})
    gone = await make_video(db, c, is_active=False)
    for pid in [private, removed, gone, str(ObjectId()), "zzz"]:
        rr = await client.post(f"{M}/conversations/{cid}/messages", json={"post_id": pid}, headers=ah)
        assert rr.status_code in (400, 404), pid


async def test_MS27_shared_post_that_later_disappears_shows_as_unavailable(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    c, _ = await make_user(db)
    v = await make_video(db, c)
    await client.post(f"{M}/conversations/{cid}/messages", json={"post_id": v}, headers=ah)
    await db.videos.update_one({"_id": ObjectId(v)}, {"$set": {"is_active": False}})
    msgs = (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()["items"]
    assert msgs[0]["kind"] == "post" and msgs[0]["post"] is None


# ---------------------------------------------------------------- read, typing, mute, delete

async def test_MS28_read_receipts_and_unread_counts(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    for i in range(3):
        await _send(client, ah, cid, f"m{i}")
    assert (await client.get(f"{M}/unread", headers=bh)).json()["unread"] == 3
    assert (await client.post(f"{M}/conversations/{cid}/read", headers=bh)).status_code == 200
    assert (await client.get(f"{M}/unread", headers=bh)).json()["unread"] == 0
    page = (await client.get(f"{M}/conversations/{cid}/messages", headers=ah)).json()
    assert page["seen_up_to"] == page["items"][0]["id"]       # the newest one was seen


async def test_MS29_typing_shows_for_a_few_seconds(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    assert (await client.post(f"{M}/conversations/{cid}/typing", headers=ah)).status_code == 200
    assert (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()["typing"] is True
    assert (await client.get(f"{M}/conversations/{cid}/messages", headers=ah)).json()["typing"] is False
    await db.conversations.update_one({"_id": ObjectId(cid)},
                                      {"$set": {f"typing.{a}": datetime.utcnow() - timedelta(seconds=30)}})
    assert (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()["typing"] is False


async def test_MS30_push_for_new_messages_respects_mute_and_settings(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, expo_push_tokens=["ExponentPushToken[kofi]"])
    cid = r.json()["id"]
    calls["pushes"].clear()
    await _send(client, ah, cid, "hi there")
    await asyncio.sleep(0.05)
    assert len(calls["pushes"]) == 1
    name, args, kwargs = calls["pushes"][0]
    flat = repr((args, kwargs))
    assert "ExponentPushToken[kofi]" in flat and "hi there" in flat and cid in flat
    calls["pushes"].clear()
    await client.post(f"{M}/conversations/{cid}/mute", json={"muted": True}, headers=bh)
    await _send(client, ah, cid, "again")
    await asyncio.sleep(0.05)
    assert calls["pushes"] == []
    await client.post(f"{M}/conversations/{cid}/mute", json={"muted": False}, headers=bh)
    await db.users.update_one({"_id": ObjectId(b)}, {"$set": {"notification_prefs.messages": False}})
    await _send(client, ah, cid, "and again")
    await asyncio.sleep(0.05)
    assert calls["pushes"] == []


async def test_MS31_request_push_doesnt_show_the_message(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False, expo_push_tokens=["ExponentPushToken[k]"])
    cid = r.json()["id"]
    calls["pushes"].clear()
    await _send(client, ah, cid, "private words")
    await asyncio.sleep(0.05)
    assert len(calls["pushes"]) == 1
    assert "private words" not in repr(calls["pushes"][0])


async def test_MS32_bursts_send_one_push_not_ten(client, db, calls):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, expo_push_tokens=["ExponentPushToken[k]"])
    cid = r.json()["id"]
    calls["pushes"].clear()
    for i in range(10):
        await _send(client, ah, cid, f"m{i}")
    await asyncio.sleep(0.05)
    assert len(calls["pushes"]) <= 2


async def test_MS33_delete_hides_history_for_me_only(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    await _send(client, ah, cid, "old")
    assert (await client.delete(f"{M}/conversations/{cid}", headers=bh)).status_code == 200
    assert await _inbox(client, bh) == []
    assert len((await client.get(f"{M}/conversations/{cid}/messages", headers=ah)).json()["items"]) == 1
    await _send(client, ah, cid, "new")
    items = (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()["items"]
    assert [x["text"] for x in items] == ["new"]
    assert (await _inbox(client, bh))[0]["unread"] == 1


async def test_MS34_paging_through_a_long_chat(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    conv = ObjectId(cid)
    now = datetime.utcnow()
    await db.messages.insert_many([{"conversation_id": conv, "sender_id": ObjectId(a), "kind": "text",
                                    "text": f"m{i}", "created_at": now + timedelta(seconds=i)} for i in range(75)])
    seen, before = [], None
    for _ in range(5):
        p = (await client.get(f"{M}/conversations/{cid}/messages",
                              params={"limit": 30, **({"before": before} if before else {})}, headers=bh)).json()
        seen += [x["text"] for x in p["items"]]
        before = p["next_before"]
        if not before:
            break
    assert seen == [f"m{i}" for i in range(74, -1, -1)]
    assert (await client.get(f"{M}/conversations/{cid}/messages", params={"limit": 500}, headers=bh)).status_code == 422


# ---------------------------------------------------------------- reports and admin

async def test_MS35_report_a_message_reaches_the_admin_page(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    mid = (await _send(client, ah, cid, "nasty words")).json()["id"]
    assert (await client.post(f"{M}/messages/{mid}/report", json={"reason": "harassment"}, headers=bh)).status_code == 201
    # reporting your own message, or twice, or someone else's chat
    assert (await client.post(f"{M}/messages/{mid}/report", json={"reason": "spam"}, headers=ah)).status_code == 400
    assert (await client.post(f"{M}/messages/{mid}/report", json={"reason": "spam"}, headers=bh)).status_code in (200, 201)
    _, xh = await make_user(db)
    assert (await client.post(f"{M}/messages/{mid}/report", json={"reason": "spam"}, headers=xh)).status_code == 404
    assert await db.message_reports.count_documents({}) == 1
    admin, adh = await make_user(db, user_type="admin")
    rows = (await client.get("/api/v1/admin/message-reports", headers=adh)).json()
    assert len(rows["items"]) == 1 and rows["items"][0]["message"]["text"] == "nasty words"
    assert rows["items"][0]["sender"]["username"] == "amina"
    # normal users can't see the admin list
    assert (await client.get("/api/v1/admin/message-reports", headers=bh)).status_code == 403


async def test_MS36_admin_removes_a_reported_message(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    mid = (await _send(client, ah, cid, "nasty")).json()["id"]
    await client.post(f"{M}/messages/{mid}/report", json={"reason": "harassment"}, headers=bh)
    admin, adh = await make_user(db, user_type="admin")
    rid = (await client.get("/api/v1/admin/message-reports", headers=adh)).json()["items"][0]["id"]
    rr = await client.post(f"/api/v1/admin/message-reports/{rid}", json={"action": "remove"}, headers=adh)
    assert rr.status_code == 200, rr.text
    items = (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()["items"]
    assert items[0]["removed"] is True and items[0]["text"] == ""
    assert (await client.get("/api/v1/admin/message-reports", headers=adh)).json()["items"] == []
    assert await db.admin_log.count_documents({}) >= 1 or await db.admin_activity.count_documents({}) >= 1


async def test_MS37_admin_list_shows_only_the_reported_message_and_a_little_context(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    for i in range(20):
        await _send(client, ah if i % 2 else bh, cid, f"m{i}")
    mid = (await _send(client, ah, cid, "bad")).json()["id"]
    await client.post(f"{M}/messages/{mid}/report", json={"reason": "spam"}, headers=bh)
    admin, adh = await make_user(db, user_type="admin")
    row = (await client.get("/api/v1/admin/message-reports", headers=adh)).json()["items"][0]
    assert len(row["context"]) <= 5


# ---------------------------------------------------------------- performance

async def test_MS38_inbox_query_count_stays_flat(client, db):
    await _on(db)
    a, ah = await make_user(db)
    for _ in range(25):
        b, bh = await make_user(db)
        await _mutual(db, a, b)
        cid = (await _open(client, ah, b)).json()["id"]
        await _send(client, bh, cid, "hi")
    import time
    t = time.perf_counter()
    items = await _inbox(client, ah)
    assert len(items) == 20 and time.perf_counter() - t < 1.5
    r = await client.get(f"{M}/conversations", params={"cursor": items[-1]["cursor"]}, headers=ah)
    assert len(r.json()["items"]) == 5


# ---------------------------------------------------------------- round 2 attacks

async def test_MS39_declined_then_the_recipient_replies_reopens_the_chat(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    await client.post(f"{M}/conversations/{cid}/decline", headers=bh)
    assert (await _send(client, bh, cid, "sorry, hello!")).status_code == 201
    assert (await _inbox(client, bh))[0]["status"] == "active"
    assert (await _inbox(client, ah))[0]["status"] == "active"
    # now links are fine for the original sender
    assert (await _send(client, ah, cid, "see wanoafrica.com")).status_code == 201


async def test_MS40_bad_cursors_and_cross_chat_paging(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    mid = (await _send(client, ah, cid, "x")).json()["id"]
    for bad in ["junk", "2026-01-01|nope", "|", "x" * 81]:
        assert (await client.get(f"{M}/conversations", params={"cursor": bad}, headers=ah)).status_code in (400, 422), bad
    c, ch = await make_user(db)
    await _mutual(db, a, c)
    cid2 = (await _open(client, ah, c)).json()["id"]
    await _send(client, ah, cid2, "y")
    # a message id from another chat can't be used to page this one
    assert (await client.get(f"{M}/conversations/{cid2}/messages", params={"before": mid}, headers=ah)).status_code == 400
    assert (await client.get(f"{M}/conversations/{cid}/messages", params={"before": "zz"}, headers=ah)).status_code == 400
    for bad_folder in ["all", "$where", ""]:
        assert (await client.get(f"{M}/conversations", params={"folder": bad_folder}, headers=ah)).status_code == 422


async def test_MS41_inbox_fills_its_page_past_blocked_and_banned_chats(client, db):
    await _on(db)
    me, mh = await make_user(db)
    good = []
    for i in range(45):
        o, oh = await make_user(db)
        await _mutual(db, me, o)
        cid = (await _open(client, mh, o)).json()["id"]
        await _send(client, oh, cid, f"hi {i}")
        if i % 3 == 0:
            good.append(o)
        elif i % 3 == 1:
            await db.users.update_one({"_id": ObjectId(o)}, {"$set": {"is_active": False}})
        else:
            await db.users.update_one({"_id": ObjectId(me)}, {"$addToSet": {"blocked_users": ObjectId(o)}})
    p1 = (await client.get(f"{M}/conversations", headers=mh)).json()
    assert len(p1["items"]) == 15 and p1["next_cursor"] is None
    assert {x["other"]["id"] for x in p1["items"]} == set(good)


async def test_MS42_report_reason_must_be_known_and_hidden_messages_cant_be_reported(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db, mutual=False)
    cid = r.json()["id"]
    await _send(client, ah, cid, "hi")
    await client.post(f"{M}/conversations/{cid}/decline", headers=bh)
    hidden = (await _send(client, ah, cid, "still here")).json()["id"]
    assert (await client.post(f"{M}/messages/{hidden}/report", json={"reason": "spam"}, headers=bh)).status_code == 404
    first = (await db.messages.find_one({"text": "hi"}))["_id"]
    for bad in [{"reason": "meh"}, {"reason": {"$ne": 1}}, {}, {"reason": "spam", "x": 1}]:
        assert (await client.post(f"{M}/messages/{first}/report", json=bad, headers=bh)).status_code == 422, bad


async def test_MS43_emoji_and_local_scripts_survive_cleaning(client, db):
    await _on(db)
    a, ah, b, bh, r = await _pair(client, db)
    cid = r.json()["id"]
    for text in ["Webale nnyo 🙏🏾", "Ẹ ku àárọ̀", "👨‍👩‍👧 family", "سلام", "ሰላም", "Nĩ wega"]:
        assert (await _send(client, ah, cid, text)).json()["text"] == text


async def test_MS44_a_blocked_person_still_cant_open_a_fresh_chat_or_request(client, db):
    await _on(db)
    a, ah = await make_user(db)
    b, bh = await make_user(db, blocked_users=[ObjectId(a)])
    assert (await _open(client, ah, b)).status_code == 403
    assert await db.conversations.count_documents({}) == 0
    # and failing doesn't use up the daily request allowance
    for _ in range(5):
        await _open(client, ah, b)
    c, _ = await make_user(db)
    assert (await _open(client, ah, c)).status_code == 200
