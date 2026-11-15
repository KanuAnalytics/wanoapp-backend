"""Message extras ("msg_plus"): reactions, edit, unsend, replies, and the chat's
check for what changed. Voice messages ("msg_voice"). Both off by default.
Attacks and races first."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import messages as svc
from tests.pressure.conftest import make_user
from tests.pressure.test_quick_posts import m4a

pytestmark = pytest.mark.asyncio

M = "/api/v1/messages"
HEART, LAUGH = "❤️", "\U0001F602"


async def _on(db, **extra):
    await db.config.insert_one({"features": {"messages": True, **extra}})


async def _mutual(db, a, b):
    for x, y in ((a, b), (b, a)):
        await db.users.update_one({"_id": ObjectId(x)}, {"$addToSet": {"following": ObjectId(y), "followers": ObjectId(y)}})


async def _chat(client, db, mutual=True):
    a, ah = await make_user(db, username="amina")
    b, bh = await make_user(db, username="kofi")
    if mutual:
        await _mutual(db, a, b)
    cid = (await client.post(f"{M}/conversations", json={"user_id": b}, headers=ah)).json()["id"]
    return a, ah, b, bh, cid


async def _send(client, h, cid, text="hi", **extra):
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": text, **extra}, headers=h)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _page(client, h, cid, **params):
    r = await client.get(f"{M}/conversations/{cid}/messages", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _react(client, h, mid, emoji):
    return client.post(f"{M}/messages/{mid}/react", json={"emoji": emoji}, headers=h)


def _edit(client, h, mid, text):
    return client.patch(f"{M}/messages/{mid}", json={"text": text}, headers=h)


def _unsend(client, h, mid):
    return client.post(f"{M}/messages/{mid}/unsend", headers=h)


async def _age(db, mid, minutes):
    await db.messages.update_one({"_id": ObjectId(mid)},
                                 {"$set": {"created_at": datetime.utcnow() - timedelta(minutes=minutes)}})


# ---------------------------------------------------------------- switches

async def test_X1_off_extras_closed_messages_still_work(client, db):
    await _on(db)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "hello")
    assert (await _react(client, bh, m["id"], HEART)).status_code == 404
    assert (await _edit(client, ah, m["id"], "hey")).status_code == 404
    assert (await _unsend(client, ah, m["id"])).status_code == 404
    # a reply from an app that has the switch cached on still sends, just without the quote
    r = await _send(client, bh, cid, "yo", reply_to=m["id"])
    assert "reply_to" not in r
    feats = (await client.get("/api/v1/config/features")).json()
    assert feats["msg_plus"] is False and feats["msg_voice"] is False
    # replies keep their old shape while the switch is off
    page = await _page(client, ah, cid)
    assert "sync" not in page
    lite = await _page(client, ah, cid, after=m["id"], since="2026-01-01T00:00:00")
    assert "sync" not in lite and "changed" not in lite


# ---------------------------------------------------------------- reactions

async def test_X2_react_change_remove_and_both_see_it(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid)
    r = await _react(client, bh, m["id"], HEART)
    assert r.status_code == 200 and r.json()["reactions"] == [{"emoji": HEART, "mine": True}]
    await _react(client, bh, m["id"], LAUGH)
    await _react(client, ah, m["id"], HEART)
    mine = {x["emoji"]: x["mine"] for x in (await _page(client, ah, cid))["items"][0]["reactions"]}
    assert mine == {HEART: True, LAUGH: False}
    await _react(client, bh, m["id"], None)
    assert (await _page(client, bh, cid))["items"][0]["reactions"] == [{"emoji": HEART, "mine": False}]


async def test_X3_react_attacks(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid)
    for bad in ("🍆", "x", HEART * 2, "", "$set"):
        assert (await _react(client, bh, m["id"], bad)).status_code == 400, bad
    assert (await client.post(f"{M}/messages/{m['id']}/react", json={"emoji": {"$ne": 1}}, headers=bh)).status_code == 422
    stranger, sh = await make_user(db)
    assert (await _react(client, sh, m["id"], HEART)).status_code == 404
    assert (await _react(client, bh, "notanid", HEART)).status_code == 404
    assert (await _react(client, bh, str(ObjectId()), HEART)).status_code == 404
    # field-name tricks can't write anywhere else in the document
    doc = await db.messages.find_one({"_id": ObjectId(m["id"])})
    assert set((doc.get("reactions") or {}).keys()) <= {a, b}


async def test_X4_no_reactions_in_requests_or_when_blocked(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db, mutual=False)
    m = await _send(client, ah, cid, "hi there")
    assert (await _react(client, bh, m["id"], HEART)).status_code == 400
    await client.post(f"{M}/conversations/{cid}/accept", headers=bh)
    assert (await _react(client, bh, m["id"], HEART)).status_code == 200
    await db.users.update_one({"_id": ObjectId(b)}, {"$addToSet": {"blocked_users": ObjectId(a)}})
    assert (await _react(client, ah, m["id"], HEART)).status_code == 403


async def test_X5_two_reacting_at_once_both_count(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid)
    await asyncio.gather(_react(client, ah, m["id"], HEART), _react(client, bh, m["id"], LAUGH))
    doc = await db.messages.find_one({"_id": ObjectId(m["id"])})
    assert doc["reactions"] == {a: HEART, b: LAUGH}


# ---------------------------------------------------------------- edit

async def test_X6_edit_shows_edited_and_keeps_original_privately(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "see you at 5")
    r = await _edit(client, ah, m["id"], "see you at 6")
    assert r.status_code == 200 and r.json()["text"] == "see you at 6" and r.json()["edited"] is True
    theirs = (await _page(client, bh, cid))["items"][0]
    assert theirs["text"] == "see you at 6" and theirs["edited"] is True and "edits" not in theirs
    doc = await db.messages.find_one({"_id": ObjectId(m["id"])})
    assert doc["edits"][0]["text"] == "see you at 5"
    inbox = (await client.get(f"{M}/conversations", headers=bh)).json()["items"]
    assert inbox[0]["last_message"]["text"] == "see you at 6"


async def test_X7_edit_rules(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "first")
    assert (await _edit(client, bh, m["id"], "hacked")).status_code == 403          # not theirs
    assert (await _edit(client, ah, m["id"], "   ")).status_code == 400
    assert (await _edit(client, ah, m["id"], "x" * 1001)).status_code == 400
    for i in range(svc.MAX_EDITS):
        assert (await _edit(client, ah, m["id"], f"v{i}")).status_code == 200
    assert (await _edit(client, ah, m["id"], "one more")).status_code == 400
    m2 = await _send(client, ah, cid, "old one")
    await _age(db, m2["id"], svc.EDIT_MINUTES + 1)
    assert (await _edit(client, ah, m2["id"], "changed")).status_code == 400


async def test_X8_edit_cant_sneak_a_link_into_a_request(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db, mutual=False)
    m = await _send(client, ah, cid, "hello")
    r = await _edit(client, ah, m["id"], "check bit.ly/xyz")
    assert r.status_code == 400 and "Links" in r.json()["detail"]


async def test_X9_two_edits_at_once_one_wins_history_consistent(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "base")
    rs = await asyncio.gather(*[_edit(client, ah, m["id"], f"v{i}") for i in range(4)])
    assert sum(r.status_code == 200 for r in rs) >= 1
    doc = await db.messages.find_one({"_id": ObjectId(m["id"])})
    chain = [e["text"] for e in doc["edits"]] + [doc["text"]]
    assert chain[0] == "base" and len(set(chain)) == len(chain)


async def test_X10_cant_edit_voice_post_or_unsent(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "oops")
    await _unsend(client, ah, m["id"])
    assert (await _edit(client, ah, m["id"], "back")).status_code == 400


# ---------------------------------------------------------------- unsend

async def test_X11_unsend_for_everyone(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "wrong chat sorry")
    await _react(client, bh, m["id"], LAUGH)
    r = await _unsend(client, ah, m["id"])
    assert r.status_code == 200 and r.json()["unsent"] is True
    theirs = (await _page(client, bh, cid))["items"][0]
    assert theirs["unsent"] is True and theirs["text"] == svc.UNSENT_TEXT and "reactions" not in theirs
    assert "wrong chat" not in str(await _page(client, bh, cid))
    inbox = (await client.get(f"{M}/conversations", headers=bh)).json()["items"]
    assert "wrong chat" not in str(inbox) and inbox[0]["last_message"]["text"] == svc.UNSENT_TEXT
    # kept privately for reports
    doc = await db.messages.find_one({"_id": ObjectId(m["id"])})
    assert doc["text"] == "wrong chat sorry"
    # again is harmless
    assert (await _unsend(client, ah, m["id"])).status_code == 200


async def test_X12_unsend_rules(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "mine")
    assert (await _unsend(client, bh, m["id"])).status_code == 403
    await _age(db, m["id"], svc.UNSEND_MINUTES + 1)
    r = await _unsend(client, ah, m["id"])
    assert r.status_code == 400 and "1 hour" in r.json()["detail"]
    await db.messages.update_one({"_id": ObjectId(m["id"])}, {"$set": {"removed": True}})
    assert (await _unsend(client, ah, m["id"])).status_code == 400


async def test_X13_unsent_can_still_be_reported_and_admin_sees_the_original(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "something nasty")
    await _edit(client, ah, m["id"], "something nice")
    await _unsend(client, ah, m["id"])
    r = await client.post(f"{M}/messages/{m['id']}/report", json={"reason": "harassment"}, headers=bh)
    assert r.status_code == 201
    rep = (await svc.admin_reports(db))[0]
    assert rep["message"]["text"] == "something nice" and rep["message"]["unsent"] is True
    assert rep["message"]["edits"] == ["something nasty"]


async def test_X14_react_and_unsend_race_never_leaves_reaction_on_deleted(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid)
    await asyncio.gather(_unsend(client, ah, m["id"]), *[_react(client, bh, m["id"], HEART) for _ in range(3)])
    out = (await _page(client, bh, cid))["items"][0]
    assert out["unsent"] is True and "reactions" not in out


async def test_X15_unsend_of_a_swallowed_request_message_doesnt_touch_the_inbox(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db, mutual=False)
    await _send(client, ah, cid, "hello")
    await client.post(f"{M}/conversations/{cid}/decline", headers=bh)
    hidden = await _send(client, ah, cid, "pls")
    before = (await db.conversations.find_one({"_id": ObjectId(cid)}))["last_message"]
    assert (await _unsend(client, ah, hidden["id"])).status_code == 200
    assert (await db.conversations.find_one({"_id": ObjectId(cid)}))["last_message"] == before


# ---------------------------------------------------------------- replies

async def test_X16_reply_shows_the_quoted_message(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    q = await _send(client, ah, cid, "what time is the show?")
    r = await _send(client, bh, cid, "8pm", reply_to=q["id"])
    assert r["reply_to"] == {"id": q["id"], "mine": False, "kind": "text", "text": "what time is the show?", "gone": False}
    mine = (await _page(client, ah, cid))["items"][0]["reply_to"]
    assert mine["mine"] is True
    # the quoted message is unsent later: the quote goes blank for both
    await _unsend(client, ah, q["id"])
    quote = (await _page(client, bh, cid))["items"][0]["reply_to"]
    assert quote["gone"] is True and quote["text"] == ""


async def test_X17_reply_attacks(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    c, ch = await make_user(db)
    await _mutual(db, a, c)
    other_cid = (await client.post(f"{M}/conversations", json={"user_id": c}, headers=ah)).json()["id"]
    secret = await _send(client, ch, other_cid, "private to amina")
    # quoting a message from another chat leaks nothing
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "x", "reply_to": secret["id"]}, headers=bh)
    assert r.status_code == 400 and "private" not in r.text
    for bad in ("nope", str(ObjectId()), "$ne"):
        r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "x", "reply_to": bad}, headers=bh)
        assert r.status_code == 400
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "x", "reply_to": {"$ne": 1}}, headers=bh)
    assert r.status_code == 422


async def test_X18_cant_quote_a_message_hidden_from_me(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db, mutual=False)
    await _send(client, ah, cid, "hello")
    await client.post(f"{M}/conversations/{cid}/decline", headers=bh)
    hidden = await _send(client, ah, cid, "you can't see this")
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "hm", "reply_to": hidden["id"]}, headers=bh)
    assert r.status_code == 400


# ---------------------------------------------------------------- what changed

async def test_X19_changes_since_reach_the_other_phone(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    old = await _send(client, ah, cid, "first")
    newest = await _send(client, ah, cid, "second")
    first = await _page(client, bh, cid)
    sync = first["sync"]
    # nothing changed yet
    p = await _page(client, bh, cid, after=newest["id"], since=sync)
    assert p["changed"] == [] and p["items"] == []
    await _edit(client, ah, old["id"], "first (fixed)")
    await _react(client, ah, newest["id"], HEART)
    p = await _page(client, bh, cid, after=newest["id"], since=sync)
    ch = {c["id"]: c for c in p["changed"]}
    assert ch[old["id"]]["text"] == "first (fixed)" and ch[newest["id"]]["reactions"][0]["emoji"] == HEART
    # the next check from the new mark doesn't repeat them forever
    await asyncio.sleep(0)
    later = (datetime.utcnow() + timedelta(seconds=1)).isoformat()
    assert (await _page(client, bh, cid, after=newest["id"], since=later))["changed"] == []


async def test_X20_too_many_changes_means_reload(client, db, monkeypatch):
    monkeypatch.setattr(svc, "CHANGES_MAX", 3)
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    ids = [(await _send(client, ah, cid, f"m{i}"))["id"] for i in range(5)]
    sync = (await _page(client, bh, cid))["sync"]
    for i in ids:
        await _react(client, bh, i, HEART)
    p = await _page(client, bh, cid, after=ids[-1], since=sync)
    assert p["gap"] is True


async def test_X21_changes_respect_hidden_and_cleared(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "before clear")
    sync = (await _page(client, bh, cid))["sync"]
    await client.delete(f"{M}/conversations/{cid}", headers=bh)
    last = await _send(client, ah, cid, "after clear")
    await _edit(client, ah, m["id"], "edited after clear")
    p = await _page(client, bh, cid, after=last["id"], since=sync)
    assert all(c["id"] != m["id"] for c in p["changed"])


async def test_X22_bad_since_refused(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid)
    for bad in ("yesterday", "2026-13-40T00:00:00", "{'$gt':1}"):
        r = await client.get(f"{M}/conversations/{cid}/messages", params={"after": m["id"], "since": bad}, headers=bh)
        assert r.status_code == 400, bad


async def test_X23_older_apps_see_readable_text(client, db):
    """Apps before 2.3.37 only know text and posts."""
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m = await _send(client, ah, cid, "secret plan")
    await _unsend(client, ah, m["id"])
    item = (await _page(client, bh, cid))["items"][0]
    assert item["kind"] == "text" and item["text"] == svc.UNSENT_TEXT and item["removed"] is False


# ---------------------------------------------------------------- voice

@pytest.fixture
def store(monkeypatch):
    from app.services import quick_posts
    saved, deleted = {}, []
    state = {"fail_put": False}

    def put(key, data):
        if state["fail_put"]:
            raise RuntimeError("spaces down")
        saved[key] = data

    def delete(key):
        deleted.append(key)
    monkeypatch.setattr(quick_posts, "_put", put)
    monkeypatch.setattr(quick_posts, "_delete", delete)
    return {"saved": saved, "deleted": deleted, "state": state}


def _voice(client, h, cid, data=None, ms=12000, **form):
    return client.post(f"{M}/conversations/{cid}/voice",
                       files={"file": ("v.m4a", data if data is not None else m4a(ms=ms), "audio/mp4")},
                       data={"duration_ms": str(ms), **form}, headers=h)


async def test_V1_off_by_default(client, db, store):
    await _on(db)
    a, ah, b, bh, cid = await _chat(client, db)
    assert (await _voice(client, ah, cid)).status_code == 404 and not store["saved"]


async def test_V2_voice_message_end_to_end(client, db, store):
    await _on(db, msg_voice=True)
    a, ah, b, bh, cid = await _chat(client, db)
    r = await _voice(client, ah, cid, client_id="c1")
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["kind"] == "voice" and v["voice"]["duration_ms"] == 12000 and v["voice"]["url"].endswith(".m4a")
    assert len(store["saved"]) == 1 and list(store["saved"])[0].startswith(f"chat-voice/{cid}/")
    theirs = (await _page(client, bh, cid))["items"][0]
    assert theirs["voice"]["url"] == v["voice"]["url"] and "update Wano" in theirs["text"]
    inbox = (await client.get(f"{M}/conversations", headers=bh)).json()["items"]
    assert inbox[0]["last_message"]["text"] == svc.VOICE_TEXT
    # a retry with the same client id doesn't make a second message or keep a second file
    r2 = await _voice(client, ah, cid, client_id="c1")
    assert r2.status_code == 200 and r2.json()["id"] == v["id"]
    assert await db.messages.count_documents({"kind": "voice"}) == 1


async def test_V3_voice_attacks(client, db, store):
    await _on(db, msg_voice=True)
    a, ah, b, bh, cid = await _chat(client, db)
    assert (await _voice(client, ah, cid, data=b"not audio at all" * 100)).status_code == 400
    assert (await _voice(client, ah, cid, data=m4a(video=True))).status_code == 400
    assert (await _voice(client, ah, cid, data=m4a(ms=200), ms=1000)).status_code == 400
    assert (await _voice(client, ah, cid, data=m4a(ms=200_000), ms=120_000)).status_code == 400
    assert (await _voice(client, ah, cid, data=m4a(size=svc.VOICE_MAX_BYTES + 10))).status_code == 413
    stranger, sh = await make_user(db)
    assert (await _voice(client, sh, cid)).status_code == 404
    assert not store["saved"]


async def test_V4_no_voice_in_a_request_until_accepted(client, db, store):
    await _on(db, msg_voice=True)
    a, ah, b, bh, cid = await _chat(client, db, mutual=False)
    await _send(client, ah, cid, "hi")
    r = await _voice(client, ah, cid)
    assert r.status_code == 400 and "accept" in r.json()["detail"] and not store["saved"]
    # the person who got the request can answer by voice (that accepts it)
    assert (await _voice(client, bh, cid)).status_code == 201
    assert (await db.conversations.find_one({"_id": ObjectId(cid)}))["state"] == "active"


async def test_V5_storage_down_nothing_half_sent(client, db, store):
    await _on(db, msg_voice=True)
    a, ah, b, bh, cid = await _chat(client, db)
    store["state"]["fail_put"] = True
    assert (await _voice(client, ah, cid)).status_code == 502
    assert await db.messages.count_documents({}) == 0


async def test_V6_blocked_after_upload_check_file_is_cleaned(client, db, store, monkeypatch):
    """If the send itself fails after the file was stored, the file goes."""
    await _on(db, msg_voice=True)
    a, ah, b, bh, cid = await _chat(client, db)

    async def no(*a, **k):
        return False
    monkeypatch.setattr(svc, "_rate_ok", no)
    assert (await _voice(client, ah, cid)).status_code == 429
    assert len(store["saved"]) == 1 and store["deleted"] == list(store["saved"])


async def test_V7_unsent_voice_hides_the_file_and_purge_deletes_it_later(client, db, store):
    await _on(db, msg_voice=True, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    v = (await _voice(client, ah, cid)).json()
    await _unsend(client, ah, v["id"])
    theirs = (await _page(client, bh, cid))["items"][0]
    assert "voice" not in theirs and v["voice"]["url"] not in str(theirs)
    assert await svc.purge_messages(db) == 0                       # kept 30 days for reports
    later = datetime.utcnow() + timedelta(days=svc.KEEP_ORIGINALS_DAYS + 1)
    assert await svc.purge_messages(db, now=later) == 1
    assert store["deleted"] == list(store["saved"])
    doc = await db.messages.find_one({"_id": ObjectId(v["id"])})
    assert doc["purged"] is True and "audio_url" not in doc


async def test_V8_reply_by_voice(client, db, store):
    await _on(db, msg_voice=True, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    q = await _send(client, ah, cid, "send me the song")
    r = await _voice(client, bh, cid, reply_to=q["id"])
    assert r.status_code == 201 and r.json()["reply_to"]["id"] == q["id"]


# ---------------------------------------------------------------- purge

async def test_P1_purge_wipes_originals_after_30_days_but_not_while_reported(client, db):
    await _on(db, msg_plus=True)
    a, ah, b, bh, cid = await _chat(client, db)
    m1 = await _send(client, ah, cid, "gone one")
    m2 = await _send(client, ah, cid, "reported one")
    m3 = await _send(client, ah, cid, "typo")
    await _unsend(client, ah, m1["id"])
    await _unsend(client, ah, m2["id"])
    await _edit(client, ah, m3["id"], "typo fixed")
    await client.post(f"{M}/messages/{m2['id']}/report", json={"reason": "harassment"}, headers=bh)
    later = datetime.utcnow() + timedelta(days=svc.KEEP_ORIGINALS_DAYS + 1)
    await svc.purge_messages(db, now=later)
    d1, d2, d3 = [await db.messages.find_one({"_id": ObjectId(x["id"])}) for x in (m1, m2, m3)]
    assert d1["text"] == "" and d1["purged"] is True
    assert d2["text"] == "reported one"
    assert "edits" not in d3 and d3["text"] == "typo fixed"
