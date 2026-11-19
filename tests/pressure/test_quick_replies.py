"""Quick post replies (quick_replies): a private reply carries a preview of the
card or voice note into messages; public comments only when the poster turned
them on. Attacks: switch off means nothing changes; replies can't point at
someone else's or an unseen quick post; comments respect blocks, links,
limits and who may delete; reports hide; counts stay right."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

Q = "/api/v1/quick"
M = "/api/v1/messages"
OLD = datetime.utcnow() - timedelta(days=30)


async def _on(db, replies=True):
    await db.config.insert_one({"features": {"quick_posts": True, "messages": True, "quick_replies": replies}})


async def _mutual(db, a, b):
    for x, y in ((a, b), (b, a)):
        await db.users.update_one({"_id": ObjectId(x)}, {"$addToSet": {"following": ObjectId(y), "followers": ObjectId(y)}})


async def _card(client, h, comments=None, text="Shooting in Yaba tomorrow, who's around?"):
    body = {"text": text, "color": "plum", **({"comments": comments} if comments is not None else {})}
    r = await client.post(f"{Q}/card", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def _chat(client, a_h, b_id):
    return (await client.post(f"{M}/conversations", json={"user_id": b_id}, headers=a_h)).json()["id"]


async def test_QR1_private_reply_carries_a_preview_both_sides_see(client, db):
    await _on(db)
    owner, oh = await make_user(db, username="kofi")
    fan, fh = await make_user(db, username="amina")
    await _mutual(db, owner, fan)
    card = await _card(client, oh)
    assert card["comments"] is False and "comments_count" not in card
    cid = await _chat(client, fh, owner)
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "I'm around!", "quick_id": card["id"]}, headers=fh)
    assert r.status_code in (200, 201), r.text
    assert r.json()["quick"] == {"id": card["id"], "kind": "card", "text": card["text"], "color": "plum"}
    page = (await client.get(f"{M}/conversations/{cid}/messages", headers=oh)).json()["items"]
    assert page[0]["quick"]["kind"] == "card" and page[0]["text"] == "I'm around!" and page[0]["mine"] is False


async def test_QR2_switch_off_reply_is_a_plain_message_and_comment_routes_are_closed(client, db):
    await _on(db, replies=False)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    await _mutual(db, owner, fan)
    card = await _card(client, oh, comments=True)
    assert card["comments"] is False            # can't turn comments on while the switch is off
    cid = await _chat(client, fh, owner)
    m = (await client.post(f"{M}/conversations/{cid}/messages", json={"text": "hey", "quick_id": card["id"]}, headers=fh)).json()
    assert "quick" not in m
    assert (await client.get(f"{Q}/{card['id']}/comments", headers=fh)).status_code == 404
    assert (await client.post(f"{Q}/{card['id']}/comments", json={"text": "x"}, headers=fh)).status_code == 404


async def test_QR3_reply_must_point_at_the_other_persons_visible_quick_post(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    third, th = await make_user(db)
    await _mutual(db, owner, fan)
    theirs = await _card(client, th)
    cid = await _chat(client, fh, owner)
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "hi", "quick_id": theirs["id"]}, headers=fh)
    assert r.status_code == 400
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "hi", "quick_id": str(ObjectId())}, headers=fh)
    assert r.status_code == 404
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": "hi", "quick_id": "../../x"}, headers=fh)
    assert r.status_code == 404


async def test_QR4_comments_only_when_the_poster_turns_them_on(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db, username="zawadi")
    card = await _card(client, oh)
    r = await client.post(f"{Q}/{card['id']}/comments", json={"text": "Nice"}, headers=fh)
    assert r.status_code == 403
    assert (await client.get(f"{Q}/{card['id']}/comments", headers=fh)).json() == {"on": False, "items": [], "count": 0}
    # Only the poster can switch them on.
    assert (await client.post(f"{Q}/{card['id']}/comments-on", json={"on": True}, headers=fh)).status_code == 404
    assert (await client.post(f"{Q}/{card['id']}/comments-on", json={"on": True}, headers=oh)).json()["comments"] is True
    c = (await client.post(f"{Q}/{card['id']}/comments", json={"text": "  Nice one  "}, headers=fh)).json()
    assert c["text"] == "Nice one" and c["mine"] is True and c["user"]["username"] == "zawadi"
    seen = (await client.get(f"{Q}/users/{owner}", headers=fh)).json()["items"][0]
    assert seen["comments"] is True and seen["comments_count"] == 1
    listed = (await client.get(f"{Q}/{card['id']}/comments", headers=oh)).json()
    assert listed["count"] == 1 and listed["items"][0]["can_delete"] is True and listed["items"][0]["mine"] is False
    # Turned off again: nobody else can add or read them; the poster still can read.
    await client.post(f"{Q}/{card['id']}/comments-on", json={"on": False}, headers=oh)
    assert (await client.post(f"{Q}/{card['id']}/comments", json={"text": "more"}, headers=fh)).status_code == 403
    assert (await client.get(f"{Q}/{card['id']}/comments", headers=fh)).json()["items"] == []
    assert len((await client.get(f"{Q}/{card['id']}/comments", headers=oh)).json()["items"]) == 1


async def test_QR5_comment_attacks(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    card = await _card(client, oh, comments=True)
    u = f"{Q}/{card['id']}/comments"
    for bad in ("", "   ", "x" * 151, "go to bit.ly/abc", "see www.scam.com", "a\nb\nc\nd", "​​"):
        assert (await client.post(u, json={"text": bad}, headers=fh)).status_code == 400, bad
    assert (await client.post(u, json={"text": 5}, headers=fh)).status_code == 422
    assert (await client.post(u, json={"text": "hi", "user_id": owner}, headers=fh)).status_code == 422
    assert (await client.post(u, json={"text": "hi"})).status_code in (401, 403)


async def test_QR6_blocks_hide_comments_and_stop_commenting(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    a, ah = await make_user(db)
    b, bh = await make_user(db)
    card = await _card(client, oh, comments=True)
    u = f"{Q}/{card['id']}/comments"
    await client.post(u, json={"text": "from a"}, headers=ah)
    await client.post(u, json={"text": "from b"}, headers=bh)
    await db.users.update_one({"_id": ObjectId(a)}, {"$push": {"blocked_users": ObjectId(b)}})
    assert [c["text"] for c in (await client.get(u, headers=ah)).json()["items"]] == ["from a"]
    await db.users.update_one({"_id": ObjectId(owner)}, {"$push": {"blocked_users": ObjectId(b)}})
    assert (await client.post(u, json={"text": "again"}, headers=bh)).status_code == 404


async def test_QR7_who_can_delete_and_counts_stay_right(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    a, ah = await make_user(db)
    b, bh = await make_user(db)
    card = await _card(client, oh, comments=True)
    u = f"{Q}/{card['id']}/comments"
    ca = (await client.post(u, json={"text": "a1"}, headers=ah)).json()
    cb = (await client.post(u, json={"text": "b1"}, headers=bh)).json()
    assert (await client.delete(f"{Q}/comments/{ca['id']}", headers=bh)).status_code == 404   # not yours
    assert (await client.delete(f"{Q}/comments/{ca['id']}", headers=ah)).status_code == 200   # yours
    assert (await client.delete(f"{Q}/comments/{ca['id']}", headers=ah)).status_code == 404   # twice: no double count
    assert (await client.delete(f"{Q}/comments/{cb['id']}", headers=oh)).status_code == 200   # the poster's
    listed = (await client.get(u, headers=oh)).json()
    assert listed["items"] == [] and listed["count"] == 0


async def test_QR8_three_trusted_reports_hide_a_comment(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    bad, bah = await make_user(db)
    card = await _card(client, oh, comments=True)
    c = (await client.post(f"{Q}/{card['id']}/comments", json={"text": "rude"}, headers=bah)).json()
    assert (await client.post(f"{Q}/comments/{c['id']}/report", json={"reason": "harassment"}, headers=bah)).status_code == 400
    for _ in range(3):
        _, h = await make_user(db, created_at=OLD)
        assert (await client.post(f"{Q}/comments/{c['id']}/report", json={"reason": "harassment"}, headers=h)).status_code == 200
    listed = (await client.get(f"{Q}/{card['id']}/comments", headers=oh)).json()
    assert listed["items"] == [] and listed["count"] == 0
    # Comment reports never show up as reports on the quick post itself.
    from app.services import quick_posts
    assert await quick_posts.admin_reports(db) == []


async def test_QR9_voice_note_reply_and_comments_flag(client, db, monkeypatch):
    from app.services import quick_posts
    monkeypatch.setattr(quick_posts, "_put", lambda k, d: None)
    from tests.pressure.test_quick_posts import m4a
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    await _mutual(db, owner, fan)
    r = await client.post(f"{Q}/voice", files={"file": ("v.m4a", m4a(), "audio/mp4")},
                          data={"duration_ms": "12000", "comments": "true"}, headers=oh)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["comments"] is True
    cid = await _chat(client, fh, owner)
    m = (await client.post(f"{M}/conversations/{cid}/messages", json={"text": "🔥", "quick_id": v["id"]}, headers=fh)).json()
    assert m["quick"]["kind"] == "voice" and m["quick"]["duration_ms"] == 12000 and "text" not in m["quick"]
