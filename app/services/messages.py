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

Extras ("msg_plus" switch): reactions (one of six per person), edit (own
text, 15 minutes, 5 times), unsend ("delete for everyone", own message, 1 hour)
and replying to a message. The original text of an edited or unsent message is
kept privately for reports for 30 days, then wiped (purge_messages). Every
change sets updated_at, and a chat's check for news (?after=&since=) returns
what changed since its last check, so the other phone sees it.
Voice messages ("msg_voice" switch): AAC .m4a up to 2 minutes, checked like
Quick posts voice notes, not allowed in a request until it is accepted.
Older apps get readable text for the new kinds ("This message was deleted",
"Voice message") since they only know text and posts.
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
REACTIONS = ("\u2764\ufe0f", "\U0001F602", "\U0001F62E", "\U0001F622", "\U0001F64F", "\U0001F44D")  # ❤️ 😂 😮 😢 🙏 👍
EDIT_MINUTES = 15
MAX_EDITS = 5
UNSEND_MINUTES = 60
KEEP_ORIGINALS_DAYS = 30
CHANGES_MAX = 50
SYNC_OVERLAP_SECONDS = 2
UNSENT_TEXT = "This message was deleted"
VOICE_TEXT = "\U0001F3A4 Voice message"           # 🎤
VOICE_TEXT_OLD_APP = "\U0001F3A4 Voice message (update Wano to listen)"
VOICE_MIN_MS = 1000
VOICE_MAX_MS = 120_500
VOICE_MAX_BYTES = 480_000          # 2 min at ~24 kbit/s is ~360 KB
VOICE_FOLDER = "chat-voice"
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


def _shown_text(m: dict) -> str:
    """The text an app shows. Older apps only know text and posts, so an unsent
    message and a voice message come with words they can show as they are."""
    if m.get("removed") is True:
        return ""
    if m.get("unsent") is True:
        return UNSENT_TEXT
    if m.get("kind") == "voice":
        return VOICE_TEXT_OLD_APP
    return m.get("text") or ""


def _msg_out(m: dict, me: ObjectId, posts: dict, replies: Optional[dict] = None) -> dict:
    removed = m.get("removed") is True
    unsent = m.get("unsent") is True
    gone = removed or unsent
    kind = m.get("kind", "text")
    out = {"id": str(m["_id"]), "kind": kind, "text": _shown_text(m),
           "mine": m.get("sender_id") == me, "at": m.get("created_at"), "removed": removed}
    if kind == "post":
        out["post"] = None if gone else posts.get(m.get("post_id"))
    if unsent:
        out["unsent"] = True
    if m.get("edited_at") and not gone:
        out["edited"] = True
    r = m.get("reactions")
    if isinstance(r, dict) and r and not gone:
        out["reactions"] = [{"emoji": e, "mine": uid == str(me)} for uid, e in sorted(r.items())
                            if isinstance(e, str) and e in REACTIONS]
    if kind == "voice" and not gone and isinstance(m.get("audio_url"), str):
        out["voice"] = {"url": m["audio_url"], "duration_ms": int(m.get("duration_ms") or 0)}
    if m.get("reply_to") is not None:
        out["reply_to"] = (replies or {}).get(m["reply_to"]) or {"id": str(m["reply_to"]), "gone": True}
    return out


def _snippet(m: dict, me: ObjectId) -> dict:
    gone = m.get("removed") is True or m.get("unsent") is True
    kind = m.get("kind", "text")
    text = "" if gone else (VOICE_TEXT if kind == "voice" else (m.get("text") or ""))[:120]
    return {"id": str(m["_id"]), "mine": m.get("sender_id") == me, "kind": kind, "text": text,
            "gone": gone}


async def _replies_for(db, conv_id: ObjectId, rows: List[dict], me: ObjectId) -> dict:
    ids = list({r["reply_to"] for r in rows if isinstance(r.get("reply_to"), ObjectId)})
    if not ids:
        return {}
    found = await db.messages.find({"_id": {"$in": ids}, "conversation_id": conv_id, "hidden_from": {"$ne": me}},
                                   {"sender_id": 1, "kind": 1, "text": 1, "removed": 1, "unsent": 1}).to_list(len(ids))
    return {m["_id"]: _snippet(m, me) for m in found}


async def _out_many(db, conv_id: ObjectId, rows: List[dict], me: ObjectId) -> List[dict]:
    posts = await _posts_for(db, [r["post_id"] for r in rows if r.get("post_id")])
    replies = await _replies_for(db, conv_id, rows, me)
    return [_msg_out(r, me, posts, replies) for r in rows]


async def _rate_ok(db, sender: ObjectId) -> bool:
    minute = _now().strftime("%Y%m%d%H%M")
    doc = await db.message_limits.find_one_and_update(
        {"_id": f"send:{sender}:{minute}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return (doc or {}).get("n", 0) <= SENDS_PER_MINUTE


async def _feature_on(db, key: str) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, key)


async def _reply_target(db, conv: dict, me_oid: ObjectId, reply_to) -> Optional[ObjectId]:
    if reply_to is None:
        return None
    if not isinstance(reply_to, str) or not ObjectId.is_valid(reply_to):
        raise HTTPException(status_code=400, detail="The message you replied to isn't available")
    t = await db.messages.find_one({"_id": ObjectId(reply_to), "conversation_id": conv["_id"],
                                    "hidden_from": {"$ne": me_oid}}, {"removed": 1, "unsent": 1, "created_at": 1})
    cleared = _mstate(conv, me_oid).get("cleared_at")
    if not t or t.get("removed") is True or t.get("unsent") is True or (
            isinstance(cleared, datetime) and t.get("created_at") and t["created_at"] <= cleared):
        raise HTTPException(status_code=400, detail="The message you replied to isn't available")
    return t["_id"]


async def send(db, me_id: str, cid: str, text, post_id, client_id, reply_to=None,
               voice: Optional[dict] = None) -> Tuple[dict, bool]:
    """Returns (message, created). created False = a retry of one already sent.
    voice: an already-stored voice file {audio_key, audio_url, duration_ms, bytes}."""
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    if client_id is not None:
        if not isinstance(client_id, str) or not (1 <= len(client_id) <= 64):
            raise HTTPException(status_code=400, detail="Bad client id")
        prev = await db.messages.find_one({"sender_id": me_oid, "client_id": client_id})
        if prev:
            return (await _out_many(db, conv["_id"], [prev], me_oid))[0], False
    # Replies only with the extras switch on; an older or switched-off app just sends.
    reply_oid = await _reply_target(db, conv, me_oid, reply_to) if (
        reply_to is not None and await _feature_on(db, "msg_plus")) else None
    if voice is not None and text:
        raise HTTPException(status_code=400, detail="A voice message can't have text")
    body = clean_text(text) if text is not None else ""
    if len(body) > MAX_TEXT:
        raise HTTPException(status_code=400, detail=f"Messages can be up to {MAX_TEXT} characters")
    post_oid = None
    if post_id is not None:
        post_oid = _oid(post_id, 404, "Post not found")
        if not await db.videos.find_one(_post_visible_query(post_oid), {"_id": 1}):
            raise HTTPException(status_code=404, detail="Post not found")
    if not body and post_oid is None and voice is None:
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
            if voice is not None:
                raise HTTPException(status_code=400, detail="You can send voice messages once they accept your request")
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
    kind = "voice" if voice is not None else "post" if post_oid else "text"
    doc = {"conversation_id": conv["_id"], "sender_id": me_oid, "kind": kind,
           "text": body, "created_at": now}
    if post_oid:
        doc["post_id"] = post_oid
    if voice is not None:
        doc.update({k: voice[k] for k in ("audio_key", "audio_url", "duration_ms", "bytes")})
    if reply_oid is not None:
        doc["reply_to"] = reply_oid
    if client_id is not None:
        doc["client_id"] = client_id
    if swallowed:
        doc["hidden_from"] = [other_oid]
    try:
        res = await db.messages.insert_one(doc)
    except DuplicateKeyError:
        prev = await db.messages.find_one({"sender_id": me_oid, "client_id": client_id})
        return (await _out_many(db, conv["_id"], [prev], me_oid))[0], False
    doc["_id"] = res.inserted_id
    last = {"id": res.inserted_id, "sender": me_oid, "kind": doc["kind"],
            "text": VOICE_TEXT if voice is not None else body[:200], "at": now}
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
        _push_later(db, conv["_id"], me, other, VOICE_TEXT if voice is not None else body, post_oid is not None,
                    request=(state == "request" and conv.get("requester") == me_oid))
        if post_oid is not None:
            # A post sent to someone is a share (app/services/shares.py).
            try:
                from app.services.shares import count_share
                await count_share(db, str(post_oid), me_id)
            except Exception:
                logger.warning("couldn't count the share of %s", post_oid)
    return (await _out_many(db, conv["_id"], [doc], me_oid))[0], True


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


async def _changes(db, conv: dict, me_oid: ObjectId, q_base: dict, since: str) -> Tuple[List[dict], bool]:
    """Messages edited, unsent, reacted to or removed since `since` (an ISO time
    this server gave the app as `sync`). Too many: gap, the app reloads."""
    try:
        t = datetime.fromisoformat(since.replace("Z", "")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Bad cursor")
    rows = await db.messages.find({**q_base, "updated_at": {"$gt": t}}).sort("updated_at", 1).limit(
        CHANGES_MAX + 1).to_list(CHANGES_MAX + 1)
    if len(rows) > CHANGES_MAX:
        return [], True
    return await _out_many(db, conv["_id"], rows, me_oid), False


def _sync_mark() -> str:
    return (_now() - timedelta(seconds=SYNC_OVERLAP_SECONDS)).isoformat()


async def page(db, me_id: str, cid: str, limit: int, before: Optional[str],
               after: Optional[str] = None, since: Optional[str] = None) -> dict:
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
    q_base = dict(q)
    # Switch off: the replies stay exactly as they were.
    plus = await _feature_on(db, "msg_plus")
    sync = _sync_mark() if plus else None
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
    items = await _out_many(db, conv["_id"], rows, me_oid)
    om = _mstate(conv, other_oid)
    typing_at = (conv.get("typing") or {}).get(str(other_oid))
    typing = isinstance(typing_at, datetime) and typing_at > _now() - timedelta(seconds=TYPING_SECONDS)
    seen = om.get("seen_id")
    if after:
        out = {"items": items, "after": after, "gap": more,
               "seen_up_to": str(seen) if seen else None, "typing": typing, "status": _status(conv, me_oid)}
        if plus:
            out["sync"] = sync
        if since and plus:
            changed, too_many = await _changes(db, conv, me_oid, q_base, since)
            out["changed"] = changed
            out["gap"] = more or too_many
        return out
    other = await db.users.find_one({"_id": other_oid}, {"username": 1, "display_name": 1, "profile_picture": 1})
    seen = om.get("seen_id")
    out = {"items": items,
           "next_before": str(rows[-1]["_id"]) if more and rows else None,
           "seen_up_to": str(seen) if seen else None, "typing": typing,
           "status": _status(conv, me_oid), "other": _public_user(other)}
    if plus:
        out["sync"] = sync
    return out


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


# ------------------------------------------------------------------ extras

async def _msg_for(db, me_oid: ObjectId, mid: str) -> Tuple[dict, dict]:
    """A message in one of my chats that I can see."""
    oid = _oid(mid, 404, "Message not found")
    msg = await db.messages.find_one({"_id": oid, "hidden_from": {"$ne": me_oid}})
    conv = msg and await db.conversations.find_one({"_id": msg["conversation_id"], "members": me_oid})
    if not msg or not conv:
        raise HTTPException(status_code=404, detail="Message not found")
    cleared = _mstate(conv, me_oid).get("cleared_at")
    if isinstance(cleared, datetime) and msg.get("created_at") and msg["created_at"] <= cleared:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg, conv


async def _fresh_out(db, conv: dict, mid: ObjectId, me_oid: ObjectId) -> dict:
    m = await db.messages.find_one({"_id": mid})
    return (await _out_many(db, conv["_id"], [m], me_oid))[0]


async def react(db, me_id: str, mid: str, emoji: Optional[str]) -> dict:
    me_oid = ObjectId(me_id)
    msg, conv = await _msg_for(db, me_oid, mid)
    if msg.get("removed") is True or msg.get("unsent") is True:
        raise HTTPException(status_code=400, detail="You can't react to this message")
    if conv.get("state") != "active":
        raise HTTPException(status_code=400, detail="You can react once the request is accepted")
    if emoji is not None and emoji not in REACTIONS:
        raise HTTPException(status_code=400, detail="Pick one of the reactions")
    me = await _user(db, me_oid)
    other = await _user(db, _other_id(conv, me_oid))
    _check_pair(me, other)
    if not await _rate_ok(db, me_oid):
        raise HTTPException(status_code=429, detail="Slow down a little")
    field = f"reactions.{me_oid}"
    upd = {"$set": {field: emoji, "updated_at": _now()}} if emoji is not None else \
        {"$unset": {field: ""}, "$set": {"updated_at": _now()}}
    # Not on a message that was unsent or removed in the meantime.
    r = await db.messages.update_one({"_id": msg["_id"], "unsent": {"$ne": True}, "removed": {"$ne": True}}, upd)
    if r.matched_count != 1:
        raise HTTPException(status_code=400, detail="You can't react to this message")
    return await _fresh_out(db, conv, msg["_id"], me_oid)


async def _set_last_text(db, conv: dict, mid: ObjectId, text: str, kind: Optional[str] = None):
    s = {"last_message.text": text[:200]}
    if kind:
        s["last_message.kind"] = kind
    await db.conversations.update_one({"_id": conv["_id"], "last_message.id": mid}, {"$set": s})


async def edit(db, me_id: str, mid: str, text) -> dict:
    me_oid = ObjectId(me_id)
    msg, conv = await _msg_for(db, me_oid, mid)
    if msg.get("sender_id") != me_oid:
        raise HTTPException(status_code=403, detail="You can only edit your own messages")
    if msg.get("kind", "text") != "text" or msg.get("removed") is True or msg.get("unsent") is True:
        raise HTTPException(status_code=400, detail="This message can't be edited")
    now = _now()
    cutoff = now - timedelta(minutes=EDIT_MINUTES)
    if not msg.get("created_at") or msg["created_at"] < cutoff:
        raise HTTPException(status_code=400, detail=f"Messages can be edited for {EDIT_MINUTES} minutes after sending")
    if len(msg.get("edits") or []) >= MAX_EDITS:
        raise HTTPException(status_code=400, detail="This message has been edited too many times")
    body = clean_text(text)
    if not body:
        raise HTTPException(status_code=400, detail="Write a message, or delete it instead")
    if len(body) > MAX_TEXT:
        raise HTTPException(status_code=400, detail=f"Messages can be up to {MAX_TEXT} characters")
    if body == (msg.get("text") or ""):
        return await _fresh_out(db, conv, msg["_id"], me_oid)
    if conv.get("state") in ("request", "declined") and conv.get("requester") == me_oid and has_link(body):
        raise HTTPException(status_code=400, detail="Links can't be sent until they accept your request")
    me = await _user(db, me_oid)
    other = await _user(db, _other_id(conv, me_oid))
    _check_pair(me, other)
    if not await _rate_ok(db, me_oid):
        raise HTTPException(status_code=429, detail="Slow down a little")
    # Only if nothing changed it since we read it (two edits at once: one wins).
    r = await db.messages.update_one(
        {"_id": msg["_id"], "sender_id": me_oid, "text": msg.get("text"), "unsent": {"$ne": True},
         "removed": {"$ne": True}, "created_at": {"$gte": cutoff}, f"edits.{MAX_EDITS - 1}": {"$exists": False}},
        {"$set": {"text": body, "edited_at": now, "updated_at": now},
         "$push": {"edits": {"text": msg.get("text") or "", "at": now}}})
    if r.modified_count != 1:
        raise HTTPException(status_code=409, detail="This message changed. Try again.")
    if not msg.get("hidden_from"):
        await _set_last_text(db, conv, msg["_id"], body)
    return await _fresh_out(db, conv, msg["_id"], me_oid)


async def unsend(db, me_id: str, mid: str) -> dict:
    me_oid = ObjectId(me_id)
    msg, conv = await _msg_for(db, me_oid, mid)
    if msg.get("sender_id") != me_oid:
        raise HTTPException(status_code=403, detail="You can only delete your own messages for everyone")
    if msg.get("unsent") is True:
        return await _fresh_out(db, conv, msg["_id"], me_oid)
    if msg.get("removed") is True:
        raise HTTPException(status_code=400, detail="This message was already removed")
    now = _now()
    cutoff = now - timedelta(minutes=UNSEND_MINUTES)
    if not msg.get("created_at") or msg["created_at"] < cutoff:
        raise HTTPException(status_code=400, detail="Messages can be deleted for everyone for 1 hour after "
                                                    "sending. You can still delete the chat for yourself.")
    # The text stays on our side for KEEP_ORIGINALS_DAYS (for reports), never shown.
    await db.messages.update_one(
        {"_id": msg["_id"], "sender_id": me_oid, "removed": {"$ne": True}, "unsent": {"$ne": True}},
        {"$set": {"unsent": True, "unsent_at": now, "updated_at": now}, "$unset": {"reactions": ""}})
    if not msg.get("hidden_from"):
        await _set_last_text(db, conv, msg["_id"], UNSENT_TEXT, "text")
    return await _fresh_out(db, conv, msg["_id"], me_oid)


async def send_voice(db, me_id: str, cid: str, data: bytes, duration_ms: int, client_id, reply_to) -> Tuple[dict, bool]:
    from app.services import quick_posts as qp
    import uuid
    me_oid = ObjectId(me_id)
    conv = await _conv_for(db, cid, me_oid)
    if client_id is not None:
        prev = await db.messages.find_one({"sender_id": me_oid, "client_id": client_id}) if isinstance(client_id, str) else None
        if prev:
            return (await _out_many(db, conv["_id"], [prev], me_oid))[0], False
    if conv.get("state") in ("request", "declined") and conv.get("requester") == me_oid:
        raise HTTPException(status_code=400, detail="You can send voice messages once they accept your request")
    me = await _user(db, me_oid)
    _check_pair(me, await _user(db, _other_id(conv, me_oid)))
    if len(data) > VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="That voice message is too long")
    if not qp.is_m4a(data):
        raise HTTPException(status_code=400, detail="That isn't a voice message")
    real = qp.m4a_duration_ms(data)
    if real is None or not (VOICE_MIN_MS <= real <= VOICE_MAX_MS) or not (VOICE_MIN_MS // 2 <= duration_ms <= VOICE_MAX_MS):
        raise HTTPException(status_code=400, detail="Voice messages are 1 second to 2 minutes")
    key = f"{VOICE_FOLDER}/{conv['_id']}/{uuid.uuid4().hex}.m4a"
    try:
        await asyncio.to_thread(qp._put, key, data)
    except Exception:
        logger.exception("chat voice upload failed")
        raise HTTPException(status_code=502, detail="Couldn't send your voice message. Try again.")
    from app.core.config import settings
    voice = {"audio_key": key, "audio_url": f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{key}",
             "duration_ms": int(real), "bytes": len(data)}
    try:
        out, created = await send(db, me_id, cid, None, None, client_id, reply_to=reply_to, voice=voice)
    except BaseException:
        await _drop_audio(key)
        raise
    if not created:
        await _drop_audio(key)          # a retry of one already sent: this copy isn't needed
    return out, created


async def _drop_audio(key: str) -> bool:
    from app.services import quick_posts as qp
    try:
        await asyncio.to_thread(qp._delete, key)
        return True
    except Exception:
        logger.warning("couldn't delete chat voice %s; purge will retry", key)
        return False


async def purge_messages(db, now: Optional[datetime] = None, batch: int = 200) -> int:
    """After KEEP_ORIGINALS_DAYS: wipe the text of unsent messages and the old
    versions of edited ones, and delete the voice files of unsent or removed
    voice messages. Not while an open report needs them."""
    now = now or _now()
    old = now - timedelta(days=KEEP_ORIGINALS_DAYS)
    n = 0
    rows = await db.messages.find({"purged": {"$ne": True}, "$or": [
        {"unsent": True, "unsent_at": {"$lte": old}},
        {"removed": True, "kind": "voice", "audio_key": {"$exists": True}},
        {"edits.0": {"$exists": True}, "edited_at": {"$lte": old}}]},
        {"kind": 1, "audio_key": 1, "unsent": 1, "removed": 1}).limit(batch).to_list(batch)
    for m in rows:
        if await db.message_reports.find_one({"message_id": m["_id"], "status": "open"}, {"_id": 1}):
            continue
        gone = m.get("unsent") is True or m.get("removed") is True
        if gone and m.get("audio_key") and not await _drop_audio(m["audio_key"]):
            continue
        upd = {"$unset": {"edits": ""}}
        if gone:
            upd["$set"] = {"purged": True, "text": ""}
            upd["$unset"].update({"audio_key": "", "audio_url": ""})
        await db.messages.update_one({"_id": m["_id"]}, upd)
        n += 1
    return n


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
                         "kind": m.get("kind", "text"), "unsent": m.get("unsent") is True,
                         "edits": [e.get("text") for e in (m.get("edits") or []) if isinstance(e, dict)],
                         "audio_url": m.get("audio_url") if m.get("kind") == "voice" else None,
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
