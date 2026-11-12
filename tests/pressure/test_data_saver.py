"""Data savers for people on mobile data. Attacks written before the code.

gzip_api: compressed API replies. Off = byte-for-byte what it was; on = only
/api and /graphql JSON, only big enough replies, only when asked for, and the
app gets exactly the same JSON back.

msg_lite (server part): an open chat asks only for messages newer than the
last one it has. Same privacy rules as a full page; nothing from another
chat; a bad or foreign cursor is refused."""

import gzip
import json

import pytest
from bson import ObjectId

from app.core import gzip_switch
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

M = "/api/v1/messages"
A = "/api/v1/admin"
GZ = {"Accept-Encoding": "gzip"}
PLAIN = {"Accept-Encoding": "identity"}


@pytest.fixture(autouse=True)
def _fresh_switch():
    gzip_switch._state.update({"on": False, "at": -1e9})
    yield
    gzip_switch._state.update({"on": False, "at": -1e9})


async def _switch(db, **on):
    doc = await db.config.find_one({"features": {"$type": "object"}})
    if doc:
        await db.config.update_one({"_id": doc["_id"]}, {"$set": {f"features.{k}": v for k, v in on.items()}})
    else:
        await db.config.insert_one({"features": dict(on)})
    gzip_switch.forget()


async def _feed(client, headers):
    return await client.get("/api/v1/feed/", params={"privacy": "public", "limit": 25, "scenario": "new",
                                                    "authenticated": "false"}, headers=headers)


async def _some_videos(db, n=25):
    c, _ = await make_user(db, username="maker")
    for i in range(n):
        await make_video(db, c, title=f"A fairly long title for post number {i} " * 3)


def _raw(r):
    """Bytes as they came over the wire (httpx would otherwise unpack them)."""
    return r.headers.get("content-encoding"), len(r.content)


# ------------------------------------------------------------------ gzip_api

async def test_GZ1_off_by_default_nothing_changes(client, db):
    await _some_videos(db)
    r = await _feed(client, GZ)
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert (await client.get("/api/v1/config/features")).json()["gzip_api"] is False


async def test_GZ2_on_compresses_big_api_replies_and_the_json_is_identical(client, db):
    await _some_videos(db)
    before = (await _feed(client, GZ)).json()
    await _switch(db, gzip_api=True)
    r = await _feed(client, GZ)
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    assert r.json() == before
    # Raw size on the wire is far smaller than the JSON.
    plain = await _feed(client, PLAIN)
    assert "content-encoding" not in plain.headers
    assert len(gzip.compress(plain.content)) < len(plain.content) / 2
    assert plain.json() == before


async def test_GZ3_only_when_the_client_asks(client, db):
    await _some_videos(db)
    await _switch(db, gzip_api=True)
    for h in (PLAIN, {"Accept-Encoding": "br"}, {"Accept-Encoding": ""}):
        r = await _feed(client, h)
        assert "content-encoding" not in r.headers, h


async def test_GZ4_small_replies_and_non_api_paths_left_alone(client, db):
    await _switch(db, gzip_api=True)
    r = await client.get("/api/v1/config/features", headers=GZ)
    assert r.status_code == 200 and "content-encoding" not in r.headers   # ~400 bytes
    for path in ("/", "/health", "/admin"):
        r = await client.get(path, headers=GZ)
        assert r.headers.get("content-encoding") != "gzip", path


async def test_GZ5_errors_and_headers_survive(client, db):
    await _some_videos(db)
    await _switch(db, gzip_api=True)
    r = await client.get("/api/v1/videos/" + str(ObjectId()), headers=GZ)
    assert r.status_code in (404, 400, 401, 403)
    # Paging headers the app reads are still there when compressed.
    r = await _feed(client, GZ)
    assert r.headers.get("content-encoding") == "gzip"
    assert "x-has-more" in {k.lower() for k in r.headers.keys()}


async def test_GZ6_admin_switch_takes_effect_at_once_and_all_off_covers_it(client, db):
    await _some_videos(db)
    _, ah = await make_user(db, user_type="admin", username="boss")
    assert "content-encoding" not in (await _feed(client, GZ)).headers
    r = await client.put(f"{A}/features", headers=ah, json={"key": "gzip_api", "on": True})
    assert r.status_code == 200
    assert (await _feed(client, GZ)).headers.get("content-encoding") == "gzip"
    assert (await client.post(f"{A}/features/all-off", headers=ah)).status_code == 200
    assert "content-encoding" not in (await _feed(client, GZ)).headers


async def test_GZ8_film_playlists_never_compressed(client, db, monkeypatch):
    await _switch(db, gzip_api=True)
    from app.services import tv_stream

    async def serve(_db, token, path):
        return "#EXTM3U\n" + "#EXTINF:6.0,\nseg.ts?sig=abc\n" * 200
    monkeypatch.setattr(tv_stream, "serve", serve)
    r = await client.get("/api/v1/tv/stream/tok/master.m3u8", headers=GZ)
    assert r.status_code == 200 and len(r.content) > 1000
    assert "content-encoding" not in r.headers
    assert r.text.startswith("#EXTM3U")


async def test_GZ9_many_requests_at_once_all_correct(client, db):
    import asyncio
    await _some_videos(db)
    await _switch(db, gzip_api=True)
    ref = (await _feed(client, PLAIN)).json()
    rs = await asyncio.gather(*[_feed(client, GZ if i % 2 else PLAIN) for i in range(40)])
    for i, r in enumerate(rs):
        assert r.status_code == 200 and r.json() == ref
        assert (r.headers.get("content-encoding") == "gzip") == bool(i % 2)


async def test_GZ7_a_database_hiccup_keeps_the_last_value(client, db, monkeypatch):
    await _some_videos(db)
    await _switch(db, gzip_api=True)
    assert (await _feed(client, GZ)).headers.get("content-encoding") == "gzip"
    from app.services import features

    async def boom(_db):
        raise RuntimeError("db down")
    monkeypatch.setattr(features, "current", boom)
    gzip_switch.forget()
    r = await client.get("/api/v1/config/version", headers=GZ)   # the switch read fails here
    assert r.status_code in (200, 404, 500)
    assert gzip_switch._state["on"] is True


# ------------------------------------------------------------- chat "after"

async def _chat(client, db):
    await db.config.insert_one({"features": {"messages": True}})
    a, ah = await make_user(db, username="amina")
    b, bh = await make_user(db, username="kofi")
    for x, y in ((a, b), (b, a)):
        await db.users.update_one({"_id": ObjectId(x)}, {"$addToSet": {"following": ObjectId(y)}})
        await db.users.update_one({"_id": ObjectId(y)}, {"$addToSet": {"followers": ObjectId(x)}})
    cid = (await client.post(f"{M}/conversations", json={"user_id": b}, headers=ah)).json()["id"]
    return a, ah, b, bh, cid


async def _say(client, h, cid, text):
    r = await client.post(f"{M}/conversations/{cid}/messages", json={"text": text}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _after(client, h, cid, after, **params):
    return await client.get(f"{M}/conversations/{cid}/messages", params={"after": after, **params}, headers=h)


async def test_AF1_only_newer_messages_and_a_lean_reply(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    first = await _say(client, ah, cid, "one")
    await _say(client, bh, cid, "two")
    await _say(client, ah, cid, "three")
    r = await _after(client, bh, cid, first)
    assert r.status_code == 200
    d = r.json()
    assert [m["text"] for m in d["items"]] == ["three", "two"]          # newest first, like a page
    assert d["gap"] is False and d["after"] == first
    assert "other" not in d and "next_before" not in d
    assert set(d) == {"items", "after", "gap", "seen_up_to", "typing", "status"}
    # Nothing new: a tiny reply.
    newest = d["items"][0]["id"]
    r = await _after(client, bh, cid, newest)
    assert r.json()["items"] == [] and len(r.content) < 200


async def test_AF2_gap_when_more_arrived_than_fit(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    first = await _say(client, ah, cid, "start")
    for i in range(6):
        await _say(client, ah, cid, f"m{i}")
    d = (await _after(client, bh, cid, first, limit=5)).json()
    assert len(d["items"]) == 5 and d["gap"] is True


async def test_AF3_cursor_from_another_chat_or_junk_is_refused(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    await _say(client, ah, cid, "hi")
    c, ch = await make_user(db, username="zawadi")
    for x, y in ((a, c), (c, a)):
        await db.users.update_one({"_id": ObjectId(x)}, {"$addToSet": {"following": ObjectId(y)}})
    cid2 = (await client.post(f"{M}/conversations", json={"user_id": c}, headers=ah)).json()["id"]
    other_msg = await _say(client, ah, cid2, "secret for zawadi")
    for bad in (other_msg, str(ObjectId()), "x", "$gt", "1" * 40):
        r = await _after(client, bh, cid, bad)
        assert r.status_code == 400, bad
        assert "secret" not in r.text
    r = await client.get(f"{M}/conversations/{cid}/messages", params={"after": other_msg, "before": other_msg},
                         headers=bh)
    assert r.status_code == 400


async def test_AF4_outsiders_and_cleared_or_hidden_messages_stay_out(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    first = await _say(client, ah, cid, "old")
    x, xh = await make_user(db, username="snoop")
    assert (await _after(client, xh, cid, first)).status_code in (403, 404)
    assert (await _after(client, {}, cid, first)).status_code == 401
    # Kofi clears the chat: messages before that never come back through `after`.
    assert (await client.delete(f"{M}/conversations/{cid}", headers=bh)).status_code == 200
    await _say(client, ah, cid, "new after clear")
    d = (await _after(client, bh, cid, first)).json()
    assert [m["text"] for m in d["items"]] == ["new after clear"]


async def test_AF5_switch_off_closes_it_like_everything_else(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    first = await _say(client, ah, cid, "hi")
    await db.config.update_many({}, {"$set": {"features.messages": False}})
    assert (await _after(client, bh, cid, first)).status_code == 404


async def test_AF6_full_pages_unchanged(client, db):
    a, ah, b, bh, cid = await _chat(client, db)
    for i in range(3):
        await _say(client, ah, cid, f"m{i}")
    d = (await client.get(f"{M}/conversations/{cid}/messages", headers=bh)).json()
    assert set(d) == {"items", "next_before", "seen_up_to", "typing", "status", "other"}
    assert d["other"]["username"] == "amina"
