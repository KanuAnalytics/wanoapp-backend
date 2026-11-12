"""In-house messaging v1 (behind the "messages" switch).

Who can message whom
  People who follow each other chat freely. Anyone else's first message lands
  in the other person's Requests: the sender can send at most 3 messages, with
  no links, until it is accepted (or the other person replies). A declined
  request looks unchanged to the sender; anything more they send is kept but
  never shown or pushed. People can turn requests off completely. Opening new
  requests is limited per day (3 for accounts under a week old, 10 otherwise).

Never
  Under-18s on either side (by date of birth), blocks either way, banned or
  deleted accounts. These are checked on every send, not just when a chat is
  opened, so a block or ban takes effect straight away.

Storage
  conversations: one per pair (unique `key`), with per-member state under
  m.<user id>: unread count, read pointer, mute, and the time they cleared the
  chat (delete is for that person only). messages: one document each; shared
  posts are stored by id and shown as "unavailable" if the post later goes.
  message_reports feed the admin page, which can remove a message.
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

MAX_TEXT = 1000
REQUEST_MESSAGES = 3
REQUESTS_PER_DAY = 10
REQUESTS_PER_DAY_NEW = 3
NEW_ACCOUNT_DAYS = 7
SENDS_PER_MINUTE = 30
TYPING_SECONDS = 6
PUSH_GAP_SECONDS = 20
PAGE = 20
REPORT_REASONS = ("spam", "harassment", "hate", "scam", "nudity", "violence", "self_harm", "other")

_LINK = re.compile(
    r"(https?://|www\.|\b[a-z0-9-]+\.(com|net|org|co|io|ly|me|app|info|biz|xyz|link|site|online|ug|ke|ng|tz|za)\b"
    r"|\bbit\.ly\b|\bwa\.me\b|\bt\.me\b)", re.I)
_tasks: set = set()


def _now() -> datetime:
    return datetime.utcnow()


def _oid(x, code=404, what="Not found") -> ObjectId:
    if not isinstance(x, str) or not ObjectId.is_valid(x):
        raise HTTPException(status_code=code, detail=what)
    return ObjectId(x)


def clean_text(text) -> str:
    """Drops invisible and direction-changing characters (a common way to hide
    or disguise text); keeps emoji (including joined ones), accents and newlines."""
    if not isinstance(text, str):
        return ""
    out = []
    for ch in text:
        if ch in "\n\t" or ch == "\u200d":       # zero-width joiner builds family/flag emoji
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(ch)
    return re.sub(r"\n{3,}", "\n\n", "".join(out).replace("\t", " ")).strip()


def has_link(text: str) -> bool:
    return bool(_LINK.search(text or ""))


def _minor(user: dict) -> bool:
    from app.services import home_ground
    return home_ground.is_minor(user)


def _blocked(a: dict, b_id: ObjectId) -> bool:
    for k in ("blocked_users", "blocked_by"):
        lst = a.get(k)
        if isinstance(lst, list) and b_id in lst:
            return True
    return False


USER_FIELDS = {"username": 1, "display_name": 1, "profile_picture": 1, "is_active": 1, "date_of_birth": 1,
               "blocked_users": 1, "blocked_by": 1, "following": 1, "followers": 1, "message_prefs": 1,
               "created_at": 1, "expo_push_tokens": 1, "notification_prefs": 1}


async def _user(db, uid: ObjectId) -> Optional[dict]:
    return await db.users.find_one({"_id": uid}, USER_FIELDS)


def _mutual(a: dict, b: dict) -> bool:
    af = a.get("following") if isinstance(a.get("following"), list) else []
    bf = b.get("following") if isinstance(b.get("following"), list) else []
    return b["_id"] in af and a["_id"] in bf


def _public_user(u: Optional[dict]) -> Optional[dict]:
    if not u:
        return None
    s = lambda k: u.get(k) if isinstance(u.get(k), str) else None  # noqa: E731
    return {"id": str(u["_id"]), "username": s("username"), "display_name": s("display_name"),
            "profile_picture": s("profile_picture")}


def _check_pair(me: dict, other: Optional[dict]):
    """The rules that apply to every open and every send."""
    if not other or other.get("is_active") is False:
        raise HTTPException(status_code=403, detail="This account isn't available")
    if _blocked(me, other["_id"]) or _blocked(other, me["_id"]):
        raise HTTPException(status_code=403, detail="You can't message this account")
    if _minor(me):
        raise HTTPException(status_code=403, detail="Messages are for people 18 and over")
    if _minor(other):
        raise HTTPException(status_code=403, detail="This account can't receive messages (under 18)")


def _status(conv: dict, me: ObjectId) -> str:
    st = conv.get("state")
    if st == "active":
        return "active"
    return "request_sent" if conv.get("requester") == me else "request_received"


def _other_id(conv: dict, me: ObjectId) -> ObjectId:
    a, b = conv["members"]
    return b if a == me else a


def _mstate(conv: dict, uid: ObjectId) -> dict:
    m = (conv.get("m") or {}).get(str(uid))
    return m if isinstance(m, dict) else {}


async def _conv_for(db, cid: str, me: ObjectId) -> dict:
    oid = _oid(cid)
    conv = await db.conversations.find_one({"_id": oid, "members": me})
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _summary(conv: dict, me: ObjectId, other: Optional[dict]) -> dict:
    ms = _mstate(conv, me)
    last = conv.get("last_message") if isinstance(conv.get("last_message"), dict) else None
    lm = None
    if last:
        lm = {"id": str(last.get("id")), "kind": last.get("kind", "text"), "text": last.get("text") or "",
              "mine": last.get("sender") == me, "at": last.get("at")}
    last_at = conv.get("last_at") or conv.get("created_at")
    return {"id": str(conv["_id"]), "status": _status(conv, me), "other": _public_user(other),
            "last_message": lm, "unread": max(0, int(ms.get("unread") or 0)), "muted": ms.get("muted") is True,
            "last_at": last_at, "cursor": f"{last_at.isoformat() if isinstance(last_at, datetime) else ''}|{conv['_id']}"}


# ------------------------------------------------------------------ open

async def _take_daily(db, key: str, cap: int) -> bool:
    day = _now().strftime("%Y%m%d")
    doc = await db.message_limits.find_one_and_update(
        {"_id": f"{key}:{day}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if (doc or {}).get("n", 0) > cap:
        await db.message_limits.update_one({"_id": f"{key}:{day}"}, {"$inc": {"n": -1}})
        return False
    return True


def _is_new_account(u: dict) -> bool:
    c = u.get("created_at")
    if not isinstance(c, datetime):
        return False
    return c.replace(tzinfo=None) > _now() - timedelta(days=NEW_ACCOUNT_DAYS)


async def open_conversation(db, me_id: str, other_id: str) -> dict:
    me_oid = ObjectId(me_id)
    if not isinstance(other_id, str) or not ObjectId.is_valid(other_id):
        raise HTTPException(status_code=400, detail="Invalid user")
    other_oid = ObjectId(other_id)
    if other_oid == me_oid:
        raise HTTPException(status_code=400, detail="You can't message yourself")
    me = await _user(db, me_oid)
    other = await _user(db, other_oid)
    if not other or other.get("is_active") is False:
        raise HTTPException(status_code=404, detail="User not found")
    _check_pair(me, other)
    a, b = sorted([me_oid, other_oid], key=str)
    key = f"{a}:{b}"
    existing = await db.conversations.find_one({"key": key})
    if existing:
        return _summary(existing, me_oid, other)
    mutual = _mutual(me, other)
    if not mutual:
        prefs = other.get("message_prefs") if isinstance(other.get("message_prefs"), dict) else {}
        if prefs.get("allow_requests") is False:
            raise HTTPException(status_code=403, detail="This person only gets messages from people they follow")
        cap = REQUESTS_PER_DAY_NEW if _is_new_account(me) else REQUESTS_PER_DAY
        if not await _take_daily(db, f"req:{me_oid}", cap):
            raise HTTPException(status_code=429, detail="You've sent a lot of message requests today. Try again tomorrow.")
    now = _now()
    doc = {"key": key, "members": [a, b], "state": "active" if mutual else "request",
           "requester": None if mutual else me_oid, "requester_sent": 0, "created_at": now, "last_at": now,
           "m": {str(a): {"unread": 0}, str(b): {"unread": 0}}}
    try:
        await db.conversations.insert_one(doc)
    except DuplicateKeyError:
        if not mutual:   # someone else's parallel open won; give back the request we took
            await db.message_limits.update_one({"_id": f"req:{me_oid}:{now.strftime('%Y%m%d')}"}, {"$inc": {"n": -1}})
        doc = await db.conversations.find_one({"key": key})
    return _summary(doc, me_oid, other)


# ------------------------------------------------------------------ inbox

async def inbox(db, me_id: str, folder: str, cursor: Optional[str]) -> dict:
    me_oid = ObjectId(me_id)
    me = await db.users.find_one({"_id": me_oid}, {"blocked_users": 1, "blocked_by": 1}) or {}
    blocked = set()
    for k in ("blocked_users", "blocked_by"):
        if isinstance(me.get(k), list):
            blocked.update(me[k])
    q: dict = {"members": me_oid, "last_message": {"$exists": True}}
    if folder == "requests":
        q.update({"state": "request", "requester": {"$ne": me_oid}})
    else:
        q["$or"] = [{"state": "active"}, {"requester": me_oid}]
    # my own hidden-from-me messages and cleared chats
    q[f"m.{me_oid}.hidden"] = {"$ne": True}
    if cursor:
        try:
            at_s, cid = cursor.split("|", 1)
            at = datetime.fromisoformat(at_s)
            q["$and"] = [{"$or": [{"last_at": {"$lt": at}}, {"last_at": at, "_id": {"$lt": ObjectId(cid)}}]}]
        except Exception:
            raise HTTPException(status_code=400, detail="Bad cursor")
    out: List[dict] = []
    scanned = 0
    batch: List[dict] = []

    async def flush(rows: List[dict]):
        others = {_other_id(c, me_oid) for c in rows}
        users = {u["_id"]: u async for u in db.users.find(
            {"_id": {"$in": list(others)}}, {"username": 1, "display_name": 1, "profile_picture": 1, "is_active": 1})}
        for c in rows:
            o = _other_id(c, me_oid)
            u = users.get(o)
            if not u or u.get("is_active") is False or o in blocked:
                continue
            if len(out) < PAGE:
                out.append(_summary(c, me_oid, u))

    # Chats with blocked or banned accounts are skipped, so keep reading until
    # the page is full (bounded, so a huge inbox can't make one request slow).
    async for c in db.conversations.find(q).sort([("last_at", -1), ("_id", -1)]).limit(PAGE * 25):
        batch.append(c)
        scanned += 1
        if len(batch) >= PAGE:
            await flush(batch)
            batch = []
            if len(out) >= PAGE:
                break
    if batch and len(out) < PAGE:
        await flush(batch)
    return {"items": out, "next_cursor": out[-1]["cursor"] if len(out) == PAGE else None}


async def unread(db, me_id: str) -> dict:
    me_oid = ObjectId(me_id)
    total = 0
    requests = 0
    async for c in db.conversations.find(
            {"members": me_oid, "last_message": {"$exists": True}, f"m.{me_oid}.hidden": {"$ne": True}},
            {"state": 1, "requester": 1, f"m.{me_oid}": 1}).limit(500):
        if c.get("state") == "request" and c.get("requester") != me_oid:
            requests += 1
        elif c.get("state") == "active":
            total += max(0, int(_mstate(c, me_oid).get("unread") or 0))
    return {"unread": total, "requests": requests}


# ------------------------------------------------------------------ messages

def _post_card(v: Optional[dict], creator: Optional[dict]) -> Optional[dict]:
    if not v or not creator:
        return None
    urls = v.get("urls") if isinstance(v.get("urls"), dict) else {}
    thumb = v.get("thumbnail") if isinstance(v.get("thumbnail"), str) else urls.get("thumbnail")
    title = v.get("title") if isinstance(v.get("title"), str) and v.get("title") else v.get("description")
    return {"id": str(v["_id"]), "thumbnail": thumb if isinstance(thumb, str) else None,
            "title": (title or "")[:120] if isinstance(title, str) else "",
            "username": creator.get("username") if isinstance(creator.get("username"), str) else None,
            "media_type": v.get("media_type") if isinstance(v.get("media_type"), str) else "video"}


def _post_visible_query(oid: ObjectId) -> dict:
    return {"_id": oid, "is_active": True, "privacy": {"$in": ["public", "unlisted"]},
            "moderation.state": {"$nin": ["held", "removed"]}}


async def _posts_for(db, ids: List[ObjectId]) -> dict:
    if not ids:
        return {}
    vids = {v["_id"]: v async for v in db.videos.find(
        {"_id": {"$in": ids}, "is_active": True, "privacy": {"$in": ["public", "unlisted"]},
         "moderation.state": {"$nin": ["held", "removed"]}},
        {"title": 1, "description": 1, "thumbnail": 1, "urls": 1, "creator_id": 1, "media_type": 1})}
    creators = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({v.get("creator_id") for v in vids.values()})}, "is_active": {"$ne": False}},
        {"username": 1})}
    return {k: _post_card(v, creators.get(v.get("creator_id"))) for k, v in vids.items()}


def _msg_out(m: dict, me: ObjectId, posts: dict) -> dict:
    removed = m.get("removed") is True
    kind = m.get("kind", "text")
    out = {"id": str(m["_id"]), "kind": kind, "text": "" if removed else (m.get("text") or ""),
           "mine": m.get("sender_id") == me, "at": m.get("created_at"), "removed": removed}
    if kind == "post":
        out["post"] = None if removed else posts.get(m.get("post_id"))
    return out


async def _rate_ok(db, sender: ObjectId) -> bool:
    minute = _now().strftime("%Y%m%d%H%M")
    doc = await db.message_limits.find_one_and_update(
        {"_id": f"send:{sender}:{minute}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return (doc or {}).get("n", 0) <= SENDS_PER_MINUTE


async def send(db, me_id: str, cid: str, text, post_id, client_id) -> Tuple[dict, bool]:
    """Returns (message, created). created False = a retry of one already sent."""
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    if client_id is not None:
        if not isinstance(client_id, str) or not (1 <= len(client_id) <= 64):
            raise HTTPException(status_code=400, detail="Bad client id")
        prev = await db.messages.find_one({"sender_id": me_oid, "client_id": client_id})
        if prev:
            posts = await _posts_for(db, [prev["post_id"]] if prev.get("post_id") else [])
            return _msg_out(prev, me_oid, posts), False
    body = clean_text(text) if text is not None else ""
    if len(body) > MAX_TEXT:
        raise HTTPException(status_code=400, detail=f"Messages can be up to {MAX_TEXT} characters")
    post_oid = None
    if post_id is not None:
        post_oid = _oid(post_id, 404, "Post not found")
        if not await db.videos.find_one(_post_visible_query(post_oid), {"_id": 1}):
            raise HTTPException(status_code=404, detail="Post not found")
    if not body and post_oid is None:
        raise HTTPException(status_code=400, detail="Write a message")
    other_oid = _other_id(conv, me_oid)
    me = await _user(db, me_oid)
    other = await _user(db, other_oid)
    _check_pair(me, other)
    if post_oid is not None:
        v = await db.videos.find_one({"_id": post_oid}, {"creator_id": 1})
        creator = await db.users.find_one({"_id": (v or {}).get("creator_id")}, {"is_active": 1})
        if not creator or creator.get("is_active") is False:
            raise HTTPException(status_code=404, detail="Post not found")
    if not await _rate_ok(db, me_oid):
        raise HTTPException(status_code=429, detail="You're sending messages very fast. Wait a moment.")

    state = conv.get("state")
    swallowed = False
    accept_now = False
    if state in ("request", "declined"):
        if conv.get("requester") == me_oid:
            if has_link(body):
                raise HTTPException(status_code=400, detail="Links can't be sent until they accept your request")
            claimed = await db.conversations.find_one_and_update(
                {"_id": conv["_id"], "requester_sent": {"$lt": REQUEST_MESSAGES}},
                {"$inc": {"requester_sent": 1}}, return_document=ReturnDocument.AFTER)
            if not claimed:
                raise HTTPException(status_code=429,
                                    detail="You can send more once they accept your request")
            swallowed = state == "declined"
        else:
            # Replying to a request accepts it, even one declined earlier.
            accept_now = True

    now = _now()
    doc = {"conversation_id": conv["_id"], "sender_id": me_oid, "kind": "post" if post_oid else "text",
           "text": body, "created_at": now}
    if post_oid:
        doc["post_id"] = post_oid
    if client_id is not None:
        doc["client_id"] = client_id
    if swallowed:
        doc["hidden_from"] = [other_oid]
    try:
        res = await db.messages.insert_one(doc)
    except DuplicateKeyError:
        prev = await db.messages.find_one({"sender_id": me_oid, "client_id": client_id})
        posts = await _posts_for(db, [prev["post_id"]] if prev and prev.get("post_id") else [])
        return _msg_out(prev, me_oid, posts), False
    doc["_id"] = res.inserted_id
    last = {"id": res.inserted_id, "sender": me_oid, "kind": doc["kind"], "text": body[:200], "at": now}
    if swallowed:
        # Only the sender's own view moves on; the other person never sees it.
        await db.conversations.update_one({"_id": conv["_id"]}, {"$set": {f"m.{me_oid}.last_sent_at": now}})
    else:
        upd = {"$set": {"last_message": last, "last_at": now, f"m.{me_oid}.hidden": False,
                        f"m.{other_oid}.hidden": False, f"typing.{me_oid}": datetime(1970, 1, 1)},
               "$inc": {f"m.{other_oid}.unread": 1}}
        if accept_now:
            upd["$set"]["state"] = "active"
        await db.conversations.update_one({"_id": conv["_id"]}, upd)
        _push_later(db, conv["_id"], me, other, body, post_oid is not None,
                    request=(state == "request" and conv.get("requester") == me_oid))
    posts = await _posts_for(db, [post_oid] if post_oid else [])
    return _msg_out(doc, me_oid, posts), True


def _push_later(db, conv_id, sender: dict, to: dict, text: str, is_post: bool, request: bool):
    try:
        t = asyncio.get_running_loop().create_task(_push(db, conv_id, sender, to, text, is_post, request))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    except RuntimeError:
        pass


async def _push(db, conv_id, sender: dict, to: dict, text: str, is_post: bool, request: bool):
    from app.services import growth_notifications as g
    try:
        prefs = to.get("notification_prefs") if isinstance(to.get("notification_prefs"), dict) else {}
        if prefs.get("messages") is False:
            return
        tokens = g._tokens(to)
        if not tokens:
            return
        uid = str(to["_id"])
        now = _now()
        # One push per chat per PUSH_GAP_SECONDS, and none if muted.
        claimed = await db.conversations.find_one_and_update(
            {"_id": conv_id, f"m.{uid}.muted": {"$ne": True},
             "$or": [{f"m.{uid}.last_push_at": {"$exists": False}},
                     {f"m.{uid}.last_push_at": {"$lte": now - timedelta(seconds=PUSH_GAP_SECONDS)}}]},
            {"$set": {f"m.{uid}.last_push_at": now}})
        if not claimed:
            return
        name = sender.get("display_name") or sender.get("username") or "Someone"
        if request:
            title, body = "New message request", f"{name} wants to send you a message"
        else:
            title = name
            body = "Shared a post with you" if is_post and not text else (text[:140] or "New message")
        data = {"type": "message", "conversation_id": str(conv_id)}
        for t in tokens:
            await g._send_one(t, body, data, title)
    except Exception:
        logger.exception("message push failed")


async def page(db, me_id: str, cid: str, limit: int, before: Optional[str],
               after: Optional[str] = None) -> dict:
    """A page of a chat, newest first. With `after` (msg_lite): only the
    messages newer than that one, and none of the parts that don't change
    (the other person), so an open chat's check for news is a few hundred
    bytes instead of the whole last page. `gap` says more arrived than fit,
    and the app then reloads the chat in full."""
    me_oid = ObjectId(me_id)
    if before and after:
        raise HTTPException(status_code=400, detail="Bad cursor")
    conv = await _conv_for(db, cid, me_oid)
    other_oid = _other_id(conv, me_oid)
    ms = _mstate(conv, me_oid)
    q: dict = {"conversation_id": conv["_id"], "hidden_from": {"$ne": me_oid}}
    cleared = ms.get("cleared_at")
    if isinstance(cleared, datetime):
        q["created_at"] = {"$gt": cleared}
    if before:
        if not ObjectId.is_valid(before):
            raise HTTPException(status_code=400, detail="Bad cursor")
        ref = await db.messages.find_one({"_id": ObjectId(before), "conversation_id": conv["_id"]}, {"created_at": 1})
        if not ref:
            raise HTTPException(status_code=400, detail="Bad cursor")
        q["$or"] = [{"created_at": {"$lt": ref["created_at"]}},
                    {"created_at": ref["created_at"], "_id": {"$lt": ref["_id"]}}]
    if after:
        if not ObjectId.is_valid(after):
            raise HTTPException(status_code=400, detail="Bad cursor")
        ref = await db.messages.find_one({"_id": ObjectId(after), "conversation_id": conv["_id"]}, {"created_at": 1})
        if not ref:
            raise HTTPException(status_code=400, detail="Bad cursor")
        q["$or"] = [{"created_at": {"$gt": ref["created_at"]}},
                    {"created_at": ref["created_at"], "_id": {"$gt": ref["_id"]}}]
    rows = await db.messages.find(q).sort([("created_at", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    posts = await _posts_for(db, [r["post_id"] for r in rows if r.get("post_id")])
    om = _mstate(conv, other_oid)
    typing_at = (conv.get("typing") or {}).get(str(other_oid))
    typing = isinstance(typing_at, datetime) and typing_at > _now() - timedelta(seconds=TYPING_SECONDS)
    seen = om.get("seen_id")
    if after:
        return {"items": [_msg_out(r, me_oid, posts) for r in rows], "after": after, "gap": more,
                "seen_up_to": str(seen) if seen else None, "typing": typing, "status": _status(conv, me_oid)}
    other = await db.users.find_one({"_id": other_oid}, {"username": 1, "display_name": 1, "profile_picture": 1})
    seen = om.get("seen_id")
    return {"items": [_msg_out(r, me_oid, posts) for r in rows],
            "next_before": str(rows[-1]["_id"]) if more and rows else None,
            "seen_up_to": str(seen) if seen else None, "typing": typing,
            "status": _status(conv, me_oid), "other": _public_user(other)}


async def mark_read(db, me_id: str, cid: str) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    last = conv.get("last_message") if isinstance(conv.get("last_message"), dict) else {}
    s = {f"m.{me_oid}.unread": 0, f"m.{me_oid}.read_at": _now()}
    if last.get("id"):
        s[f"m.{me_oid}.seen_id"] = last["id"]
    await db.conversations.update_one({"_id": conv["_id"]}, {"$set": s})
    return {"ok": True}


async def typing(db, me_id: str, cid: str) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    await db.conversations.update_one({"_id": conv["_id"]}, {"$set": {f"typing.{me_oid}": _now()}})
    return {"ok": True}


async def accept(db, me_id: str, cid: str) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    if conv.get("requester") == me_oid:
        raise HTTPException(status_code=400, detail="Only the person who got the request can accept it")
    if conv.get("state") in ("request", "declined"):
        await db.conversations.update_one({"_id": conv["_id"]}, {"$set": {"state": "active", f"m.{me_oid}.hidden": False}})
    conv = await db.conversations.find_one({"_id": conv["_id"]})
    other = await db.users.find_one({"_id": _other_id(conv, me_oid)}, {"username": 1, "display_name": 1, "profile_picture": 1})
    return _summary(conv, me_oid, other)


async def decline(db, me_id: str, cid: str) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    if conv.get("requester") == me_oid or conv.get("state") != "request":
        raise HTTPException(status_code=400, detail="There's no request to decline")
    await db.conversations.update_one({"_id": conv["_id"]},
                                      {"$set": {"state": "declined", f"m.{me_oid}.hidden": True,
                                                f"m.{me_oid}.unread": 0}})
    return {"ok": True}


async def mute(db, me_id: str, cid: str, muted: bool) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    await db.conversations.update_one({"_id": conv["_id"]}, {"$set": {f"m.{me_oid}.muted": muted}})
    return {"muted": muted}


async def clear(db, me_id: str, cid: str) -> dict:
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    await db.conversations.update_one({"_id": conv["_id"]},
                                      {"$set": {f"m.{me_oid}.cleared_at": _now(), f"m.{me_oid}.hidden": True,
                                                f"m.{me_oid}.unread": 0}})
    return {"ok": True}


async def get_settings(db, me_id: str) -> dict:
    u = await db.users.find_one({"_id": ObjectId(me_id)}, {"message_prefs": 1}) or {}
    p = u.get("message_prefs") if isinstance(u.get("message_prefs"), dict) else {}
    return {"allow_requests": p.get("allow_requests") is not False}


async def put_settings(db, me_id: str, allow_requests: bool) -> dict:
    await db.users.update_one({"_id": ObjectId(me_id)}, {"$set": {"message_prefs.allow_requests": allow_requests}})
    return {"allow_requests": allow_requests}


# ------------------------------------------------------------------ reports

async def report(db, me_id: str, mid: str, reason: str) -> Tuple[dict, bool]:
    me_oid = ObjectId(me_id)
    m_oid = _oid(mid, 404, "Message not found")
    msg = await db.messages.find_one({"_id": m_oid})
    if not msg or not await db.conversations.find_one({"_id": msg["conversation_id"], "members": me_oid}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.get("sender_id") == me_oid:
        raise HTTPException(status_code=400, detail="You can't report your own message")
    if me_oid in (msg.get("hidden_from") or []):
        raise HTTPException(status_code=404, detail="Message not found")
    try:
        await db.message_reports.insert_one({"_id": f"{m_oid}:{me_oid}", "message_id": m_oid,
                                             "conversation_id": msg["conversation_id"], "reporter_id": me_oid,
                                             "sender_id": msg.get("sender_id"), "reason": reason,
                                             "status": "open", "created_at": _now()})
        return {"reported": True}, True
    except DuplicateKeyError:
        return {"reported": True}, False


async def admin_reports(db, limit: int = 50) -> List[dict]:
    rows = await db.message_reports.find({"status": "open"}).sort("created_at", -1).limit(limit).to_list(limit)
    out = []
    for r in rows:
        msg = await db.messages.find_one({"_id": r["message_id"]})
        if not msg:
            continue
        before = await db.messages.find({"conversation_id": msg["conversation_id"],
                                         "created_at": {"$lt": msg["created_at"]}}
                                        ).sort("created_at", -1).limit(2).to_list(2)
        after = await db.messages.find({"conversation_id": msg["conversation_id"],
                                        "created_at": {"$gt": msg["created_at"]}}
                                       ).sort("created_at", 1).limit(2).to_list(2)
        ids = {msg.get("sender_id"), r.get("reporter_id")} | {m.get("sender_id") for m in before + after}
        users = {u["_id"]: u async for u in db.users.find({"_id": {"$in": list(ids)}},
                                                          {"username": 1, "display_name": 1, "profile_picture": 1})}
        ctx = lambda m: {"id": str(m["_id"]), "text": "" if m.get("removed") else (m.get("text") or ""),  # noqa: E731
                         "kind": m.get("kind", "text"),
                         "from": (users.get(m.get("sender_id")) or {}).get("username"),
                         "at": m.get("created_at"), "reported": m["_id"] == msg["_id"]}
        out.append({"id": str(r["_id"]), "reason": r.get("reason"), "created_at": r.get("created_at"),
                    "message": ctx(msg), "sender": _public_user(users.get(msg.get("sender_id"))),
                    "reporter": _public_user(users.get(r.get("reporter_id"))),
                    "context": [ctx(m) for m in reversed(before)] + [ctx(msg)] + [ctx(m) for m in after]})
    return out


async def admin_act(db, report_id: str, action: str) -> dict:
    r = await db.message_reports.find_one({"_id": report_id})
    if not r:
        raise HTTPException(status_code=404, detail="Report not found")
    if action == "remove":
        await db.messages.update_one({"_id": r["message_id"]},
                                     {"$set": {"removed": True, "text": "", "removed_at": _now()}})
        conv = await db.conversations.find_one({"_id": r["conversation_id"]}, {"last_message": 1})
        if conv and (conv.get("last_message") or {}).get("id") == r["message_id"]:
            await db.conversations.update_one({"_id": conv["_id"]}, {"$set": {"last_message.text": ""}})
    # Every report of this message is settled together.
    await db.message_reports.update_many({"message_id": r["message_id"], "status": "open"},
                                         {"$set": {"status": "removed" if action == "remove" else "dismissed",
                                                   "settled_at": _now()}})
    return {"ok": True, "action": action}
