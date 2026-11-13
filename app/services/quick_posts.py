"""Quick posts ("quick_posts" switch, off by default).

The ring around a creator's photo in the feed. Two kinds, both tiny on data:

- voice: a Wano Voice note, up to 30 seconds of low-bitrate AAC (.m4a), about
  20-60 KB. The app sends the file here (not straight to storage) so it is
  checked before it is kept: size, length and that it really is an m4a.
- card:  a Status card, up to 140 characters on one of six Wano colours. No
  picture at all.

Each lasts 24 hours. At most MAX_ACTIVE at a time and PER_DAY a day.

Safety, the same rules as messages and the feed:
- Only signed-in people see quick posts.
- Blocks either way hide them; banned accounts' disappear.
- Under-18s: Status cards only (no voice), and only people who follow them see
  them. Links are refused in cards from everyone.
- Every one can be reported. REPORTS_TO_HIDE reports from established
  accounts hide it at once until an admin looks (remove or restore).
- No Wano points, no notifications: they cost nothing to run.

Old voice files are deleted from storage by purge() (growth loop, every 30
min); the records themselves go 3 days after expiry (TTL), so a report can
still be looked at.
"""

import asyncio
import logging
import re
import struct
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.config import settings

logger = logging.getLogger(__name__)

LIFETIME = timedelta(hours=24)
MAX_ACTIVE = 3
PER_DAY = 10
CARD_MAX = 140
COLORS = ("green", "night", "sunset", "ocean", "plum", "earth")
VOICE_MIN_MS = 1000
VOICE_MAX_MS = 30_500           # 30 s plus a little slack for the recorder
VOICE_MAX_BYTES = 160_000       # 30 s at 32 kbit/s is 120 KB; the app records at ~24 kbit/s
VOICE_FOLDER = "quick-voice"
RING_IDS_MAX = 60
REPORTS_TO_HIDE = 3
TRUSTED_ACCOUNT_DAYS = 3
REPORTS_PER_DAY = 20
SEEN_PER_DAY = 2000
REASONS = ("spam", "harassment", "hate", "scam", "nudity", "violence", "self_harm", "false_information", "other")


def _now() -> datetime:
    return datetime.utcnow()


def _oid(x, what="Not found") -> ObjectId:
    if not isinstance(x, str) or not ObjectId.is_valid(x):
        raise HTTPException(status_code=404, detail=what)
    return ObjectId(x)


def _minor(u: dict) -> bool:
    from app.services import home_ground
    return home_ground.is_minor(u)


def _ids(u: dict, key: str) -> set:
    """The ids in a user's list, as strings (old records mix strings and ObjectIds)."""
    v = (u or {}).get(key)
    return {str(x) for x in v} if isinstance(v, list) else set()


def _live(now: Optional[datetime] = None) -> dict:
    """Mongo filter: quick posts anyone may still see."""
    return {"expires_at": {"$gt": now or _now()}, "deleted": {"$ne": True}, "hidden": {"$ne": True}}


VIEWER_FIELDS = {"blocked_users": 1, "blocked_by": 1, "is_active": 1, "date_of_birth": 1}
OWNER_FIELDS = {"username": 1, "display_name": 1, "profile_picture": 1, "is_active": 1, "date_of_birth": 1,
                "blocked_users": 1, "blocked_by": 1}


def _may_see(me: dict, owner: Optional[dict], follows: Optional[set] = None) -> bool:
    """follows: the ids of under-18 owners I follow (only those are asked about)."""
    if not owner or owner.get("is_active") is False:
        return False
    if owner["_id"] == me["_id"]:
        return True
    if str(owner["_id"]) in _ids(me, "blocked_users") | _ids(me, "blocked_by"):
        return False
    if str(me["_id"]) in _ids(owner, "blocked_users") | _ids(owner, "blocked_by"):
        return False
    if _minor(owner) and str(owner["_id"]) not in (follows or set()):
        return False
    return True


async def _follows(db, me: dict, owners) -> set:
    """Which of these under-18 owners I follow. Only asked when there are any, and
    only about them, so someone following thousands costs nothing extra."""
    minors = [o["_id"] for o in owners if o and o["_id"] != me["_id"] and _minor(o)]
    if not minors:
        return set()
    doc = await db.users.find_one({"_id": me["_id"]}, {"following": 1}) or {}
    mine = _ids(doc, "following")
    return {str(m) for m in minors if str(m) in mine}


def _public_user(u: dict) -> dict:
    s = lambda k: u.get(k) if isinstance(u.get(k), str) else None  # noqa: E731
    return {"id": str(u["_id"]), "username": s("username"), "display_name": s("display_name"),
            "profile_picture": s("profile_picture")}


def _out(q: dict, seen: bool, mine: bool) -> dict:
    o = {"id": str(q["_id"]), "kind": q["kind"], "created_at": q["created_at"], "expires_at": q["expires_at"],
         "seen": seen}
    if q["kind"] == "card":
        o.update(text=q.get("text") or "", color=q.get("color") if q.get("color") in COLORS else "green")
    else:
        o.update(audio_url=q.get("audio_url"), duration_ms=int(q.get("duration_ms") or 0))
    if mine:
        o["views"] = int(q.get("views") or 0)
    return o


async def _me(db, me_id: str) -> dict:
    me = await db.users.find_one({"_id": ObjectId(me_id)}, VIEWER_FIELDS)
    if not me:
        raise HTTPException(status_code=401, detail="Not signed in")
    return me


# ------------------------------------------------------------------ reading

async def rings(db, me_id: str, ids: List[str]) -> Dict[str, dict]:
    """For the people on screen: who has quick posts I may see, and are any new to me."""
    oids = list({ObjectId(i) for i in ids[:RING_IDS_MAX] if isinstance(i, str) and ObjectId.is_valid(i)})
    if not oids:
        return {}
    me = await _me(db, me_id)
    posts = await db.quick_posts.find({"user_id": {"$in": oids}, **_live()},
                                      {"user_id": 1, "kind": 1}).to_list(len(oids) * MAX_ACTIVE + 10)
    if not posts:
        return {}
    owners = {u["_id"]: u async for u in db.users.find({"_id": {"$in": list({p["user_id"] for p in posts})}},
                                                        OWNER_FIELDS)}
    follows = await _follows(db, me, owners.values())
    posts = [p for p in posts if _may_see(me, owners.get(p["user_id"]), follows)]
    seen = {d["_id"] async for d in db.quick_post_seen.find(
        {"_id": {"$in": [f"{me['_id']}:{p['_id']}" for p in posts]}}, {"_id": 1})}
    out: Dict[str, dict] = {}
    for p in posts:
        r = out.setdefault(str(p["user_id"]), {"n": 0, "new": False, "voice": False})
        r["n"] += 1
        r["voice"] = r["voice"] or p.get("kind") == "voice"
        if p["user_id"] != me["_id"] and f"{me['_id']}:{p['_id']}" not in seen:
            r["new"] = True
    return out


async def for_user(db, me_id: str, uid: str) -> dict:
    owner_oid = _oid(uid, "Nothing here")
    me = await _me(db, me_id)
    owner = await db.users.find_one({"_id": owner_oid}, OWNER_FIELDS)
    if not _may_see(me, owner, await _follows(db, me, [owner])):
        return {"user": None, "items": [], "mine": False}
    mine = owner_oid == me["_id"]
    posts = await db.quick_posts.find({"user_id": owner_oid, **_live()}).sort("created_at", 1).to_list(MAX_ACTIVE + 5)
    seen = {d["_id"] async for d in db.quick_post_seen.find(
        {"_id": {"$in": [f"{me['_id']}:{p['_id']}" for p in posts]}}, {"_id": 1})}
    return {"user": _public_user(owner), "mine": mine,
            "items": [_out(p, mine or f"{me['_id']}:{p['_id']}" in seen, mine) for p in posts]}


async def _visible_post(db, me: dict, qid: str) -> dict:
    q = await db.quick_posts.find_one({"_id": _oid(qid), **_live()})
    if not q:
        raise HTTPException(status_code=404, detail="This quick post has gone")
    owner = await db.users.find_one({"_id": q["user_id"]}, OWNER_FIELDS)
    if not _may_see(me, owner, await _follows(db, me, [owner])):
        raise HTTPException(status_code=404, detail="This quick post has gone")
    return q


async def _take_daily(db, key: str, cap: int) -> bool:
    day = _now().strftime("%Y%m%d")
    doc = await db.quick_limits.find_one_and_update(
        {"_id": f"{key}:{day}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if (doc or {}).get("n", 0) > cap:
        await db.quick_limits.update_one({"_id": f"{key}:{day}"}, {"$inc": {"n": -1}})
        return False
    return True


async def _give_back(db, key: str) -> None:
    await db.quick_limits.update_one({"_id": f"{key}:{_now().strftime('%Y%m%d')}", "n": {"$gt": 0}}, {"$inc": {"n": -1}})


async def mark_seen(db, me_id: str, qid: str) -> dict:
    me = await _me(db, me_id)
    q = await _visible_post(db, me, qid)
    if q["user_id"] == me["_id"]:
        return {"ok": True}
    if not await _take_daily(db, f"seen:{me['_id']}", SEEN_PER_DAY):
        return {"ok": True}
    try:
        await db.quick_post_seen.insert_one({"_id": f"{me['_id']}:{q['_id']}", "at": _now()})
    except DuplicateKeyError:
        return {"ok": True}
    await db.quick_posts.update_one({"_id": q["_id"]}, {"$inc": {"views": 1}})
    return {"ok": True}


# ------------------------------------------------------------------ making

async def _poster(db, me_id: str) -> dict:
    me = await db.users.find_one({"_id": ObjectId(me_id)}, {"is_active": 1, "date_of_birth": 1})
    if not me or me.get("is_active") is False:
        raise HTTPException(status_code=403, detail="This account can't post")
    return me


def _slots(uid, now: Optional[datetime] = None) -> dict:
    """The quick posts that use one of your 3 places. One hidden after reports
    doesn't: you can't see it to delete it, so it mustn't block you."""
    return {"user_id": uid, **_live(now)}


async def _room(db, me: dict) -> None:
    n = await db.quick_posts.count_documents(_slots(me["_id"]))
    if n >= MAX_ACTIVE:
        raise HTTPException(status_code=409, detail=f"You can have {MAX_ACTIVE} quick posts at a time. "
                                                    "Delete one or wait for one to end.")
    if not await _take_daily(db, f"make:{me['_id']}", PER_DAY):
        raise HTTPException(status_code=429, detail="That's enough quick posts for today")


async def _insert(db, doc: dict) -> dict:
    now = _now()
    doc.update(created_at=now, expires_at=now + LIFETIME, views=0, reports=0)
    doc["_id"] = (await db.quick_posts.insert_one(doc)).inserted_id
    # Two posts sent at the same moment can both pass the count; the later one goes.
    live = await db.quick_posts.find(_slots(doc["user_id"], now), {"_id": 1}).sort("_id", 1).to_list(MAX_ACTIVE + 10)
    if doc["_id"] not in [q["_id"] for q in live[:MAX_ACTIVE]]:
        await db.quick_posts.delete_one({"_id": doc["_id"]})
        await _give_back(db, f"make:{doc['user_id']}")
        raise HTTPException(status_code=409, detail=f"You can have {MAX_ACTIVE} quick posts at a time.")
    return _out(doc, True, True)


# On top of the messages link check: the country endings people around Wano use.
_MORE_LINKS = re.compile(r"\b[a-z0-9-]+\.(gh|cm|rw|zm|zw|et|sn|ci|cd|bj|tg|ml|bf|ne|mw|mz|ao|na|bw|ls|sz|"
                         r"so|sd|ss|er|dj|mg|mu|sc|eg|ma|dz|tn|ly|lr|sl|gm|gn|gw|cv|st|gq|ga|cg|cf|td|bi|"
                         r"tv|cc|gg|top|shop|live|click|pro|vip|win|bet|club|store|ru|in|uk|us|tk|ml|gq)\b", re.I)
MAX_MARKS_IN_A_ROW = 3


def _tidy_card(text) -> str:
    """Clean text that can't be used to hide things: invisible characters go
    (messages.clean_text), piles of accent marks (the "glitch text" that spills
    off the card) are cut to a few, and something visible must be left."""
    from app.services.messages import clean_text
    out, run = [], 0
    for ch in clean_text(text):
        if unicodedata.category(ch) in ("Mn", "Me"):
            run += 1
            if run > MAX_MARKS_IN_A_ROW:
                continue
        else:
            run = 0
        out.append(ch)
    t = "".join(out).strip()
    visible = any(unicodedata.category(c)[0] in "LNPS" for c in t)
    return t if visible else ""


async def make_card(db, me_id: str, text, color: str) -> dict:
    from app.services.messages import has_link
    me = await _poster(db, me_id)
    t = _tidy_card(text)
    if not t:
        raise HTTPException(status_code=400, detail="Write something first")
    if len(t) > CARD_MAX:
        raise HTTPException(status_code=400, detail=f"Keep it to {CARD_MAX} characters")
    if t.count("\n") > 5:
        raise HTTPException(status_code=400, detail="Too many lines")
    if has_link(t) or _MORE_LINKS.search(t):
        raise HTTPException(status_code=400, detail="Links can't go in quick posts")
    await _room(db, me)
    return await _insert(db, {"user_id": me["_id"], "kind": "card", "text": t, "color": color})


def _boxes(data: bytes, start: int = 0, end: Optional[int] = None):
    """The MP4 boxes between start and end: (type, body start, body end)."""
    end = len(data) if end is None else end
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        head = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
            head = 16
        elif size == 0:
            size = end - i
        if size < head or i + size > end:
            return
        yield kind, i + head, i + size
        i += size


def _child(data: bytes, kind: bytes, start: int, end: int):
    for k, b, e in _boxes(data, start, end):
        if k == kind:
            return b, e
    return None


def is_m4a(data: bytes) -> bool:
    """An M4A voice file: an MP4 'ftyp' box first, a 'moov', and sound tracks only."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    if data[8:12] not in (b"M4A ", b"mp42", b"isom", b"mp41", b"3gp4", b"3gp5", b"iso2", b"dash"):
        return False
    moov = _child(data, b"moov", 0, len(data))
    if not moov:
        return False
    tracks = 0
    for k, b, e in _boxes(data, *moov):
        if k != b"trak":
            continue
        mdia = _child(data, b"mdia", b, e)
        hdlr = mdia and _child(data, b"hdlr", *mdia)
        if not hdlr or data[hdlr[0] + 8:hdlr[0] + 12] != b"soun":
            return False            # a video (or anything but sound) is sneaking in
        tracks += 1
    return tracks >= 1


def m4a_duration_ms(data: bytes) -> Optional[int]:
    """The real length, from the file's own 'mvhd' header (never the app's word for it)."""
    moov = _child(data, b"moov", 0, len(data))
    mvhd = moov and _child(data, b"mvhd", *moov)
    if not mvhd:
        return None
    b, e = mvhd
    try:
        if data[b] == 1:
            scale, dur = struct.unpack(">IQ", data[b + 20:b + 32])
        else:
            scale, dur = struct.unpack(">II", data[b + 12:b + 20])
    except (struct.error, IndexError):
        return None
    if scale <= 0:
        return None
    return int(dur * 1000 / scale)


def _put(key: str, data: bytes) -> None:
    from app.services.upload_DO import _spaces_client
    _spaces_client().put_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key, Body=data, ACL="public-read",
                                ContentType="audio/mp4", CacheControl="public, max-age=3600")


def _delete(key: str) -> None:
    from app.services.upload_DO import _spaces_client
    _spaces_client().delete_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key)


async def make_voice(db, me_id: str, data: bytes, duration_ms: int) -> dict:
    me = await _poster(db, me_id)
    if _minor(me):
        raise HTTPException(status_code=403, detail="Voice quick posts are for people 18 and over. "
                                                    "You can post a Status card.")
    if len(data) > VOICE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="That voice note is too big")
    if not is_m4a(data):
        raise HTTPException(status_code=400, detail="That isn't a voice note")
    real = m4a_duration_ms(data)
    # The file's own length decides; the app's number only has to roughly agree.
    if real is None or not (VOICE_MIN_MS <= real <= VOICE_MAX_MS) or not (VOICE_MIN_MS // 2 <= duration_ms <= VOICE_MAX_MS):
        raise HTTPException(status_code=400, detail="Voice notes are 1 to 30 seconds")
    duration_ms = real
    await _room(db, me)
    key = f"{VOICE_FOLDER}/{me['_id']}/{uuid.uuid4().hex}.m4a"
    try:
        await asyncio.to_thread(_put, key, data)
    except Exception:
        logger.exception("quick voice upload failed")
        await _give_back(db, f"make:{me['_id']}")
        raise HTTPException(status_code=502, detail="Couldn't save your voice note. Try again.")
    url = f"{settings.DO_SPACES_CDN_URL}/{settings.DO_SPACES_BUCKET_NAME}/{key}"
    try:
        return await _insert(db, {"user_id": me["_id"], "kind": "voice", "audio_key": key, "audio_url": url,
                                  "duration_ms": int(duration_ms), "bytes": len(data)})
    except HTTPException:
        try:
            await asyncio.to_thread(_delete, key)
        except Exception:
            logger.warning("orphan quick voice %s", key)
        raise


async def delete(db, me_id: str, qid: str) -> dict:
    q = await db.quick_posts.find_one({"_id": _oid(qid), "user_id": ObjectId(me_id), "deleted": {"$ne": True}})
    if not q:
        raise HTTPException(status_code=404, detail="Not found")
    await db.quick_posts.update_one({"_id": q["_id"]}, {"$set": {"deleted": True, "deleted_at": _now()}})
    await _drop_file(db, q)
    return {"deleted": True}


async def _drop_file(db, q: dict) -> None:
    if q.get("audio_key") and not q.get("purged"):
        try:
            await asyncio.to_thread(_delete, q["audio_key"])
        except Exception:
            logger.warning("couldn't delete %s; purge will retry", q.get("audio_key"))
            return
    await db.quick_posts.update_one({"_id": q["_id"]}, {"$set": {"purged": True}})


async def purge(db, now: Optional[datetime] = None, batch: int = 200) -> int:
    """Delete the voice files of quick posts that have ended or were deleted."""
    now = now or _now()
    rows = await db.quick_posts.find({"purged": {"$ne": True}, "$or": [{"expires_at": {"$lte": now}},
                                                                       {"deleted": True}]},
                                     {"audio_key": 1}).limit(batch).to_list(batch)
    for q in rows:
        await _drop_file(db, q)
    return len(rows)


# ------------------------------------------------------------------ reports

async def report(db, me_id: str, qid: str, reason: str) -> dict:
    me = await _me(db, me_id)
    q = await _visible_post(db, me, qid)
    if q["user_id"] == me["_id"]:
        raise HTTPException(status_code=400, detail="You can't report your own quick post")
    try:
        await db.quick_reports.insert_one({"_id": f"{q['_id']}:{me['_id']}", "quick_id": q["_id"],
                                           "reporter_id": me["_id"], "owner_id": q["user_id"], "reason": reason,
                                           "status": "open", "created_at": _now()})
    except DuplicateKeyError:
        return {"reported": True}
    reporter = await db.users.find_one({"_id": me["_id"]}, {"created_at": 1})
    made = (reporter or {}).get("created_at")
    trusted = isinstance(made, datetime) and made.replace(tzinfo=None) < _now() - timedelta(days=TRUSTED_ACCOUNT_DAYS)
    if trusted and await _take_daily(db, f"report:{me['_id']}", REPORTS_PER_DAY):
        upd = await db.quick_posts.find_one_and_update({"_id": q["_id"]}, {"$inc": {"reports": 1}},
                                                       return_document=ReturnDocument.AFTER)
        if (upd or {}).get("reports", 0) >= REPORTS_TO_HIDE:
            await db.quick_posts.update_one({"_id": q["_id"]}, {"$set": {"hidden": True, "hidden_at": _now()}})
    return {"reported": True}


async def admin_reports(db, limit: int = 50) -> List[dict]:
    rows = await db.quick_reports.aggregate([
        {"$match": {"status": "open"}},
        {"$group": {"_id": "$quick_id", "reasons": {"$push": "$reason"}, "n": {"$sum": 1},
                    "last": {"$max": "$created_at"}}},
        {"$sort": {"last": -1}}, {"$limit": limit}]).to_list(limit)
    posts = {q["_id"]: q async for q in db.quick_posts.find({"_id": {"$in": [r["_id"] for r in rows]}})}
    owners = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({q["user_id"] for q in posts.values()})}}, OWNER_FIELDS)}
    out = []
    for r in rows:
        q = posts.get(r["_id"])
        if not q:
            continue
        o = _out(q, True, True)
        if q.get("purged") and o.get("audio_url"):
            o["audio_url"] = None        # the file is gone; nothing to play
        o.update(reports=r["n"], reasons=sorted(set(r["reasons"])), hidden=q.get("hidden") is True,
                 deleted=q.get("deleted") is True, owner=_public_user(owners[q["user_id"]])
                 if q["user_id"] in owners else None)
        out.append(o)
    return out


async def admin_act(db, qid: str, action: str) -> dict:
    q = await db.quick_posts.find_one({"_id": _oid(qid)})
    if not q:
        raise HTTPException(status_code=404, detail="Not found")
    if action == "remove":
        await db.quick_posts.update_one({"_id": q["_id"]}, {"$set": {"deleted": True, "hidden": True,
                                                                     "removed_by_admin": True, "text": ""}})
        await _drop_file(db, q)
    else:
        await db.quick_posts.update_one({"_id": q["_id"]}, {"$set": {"hidden": False, "reports": 0}})
    await db.quick_reports.update_many({"quick_id": q["_id"], "status": "open"},
                                       {"$set": {"status": "removed" if action == "remove" else "dismissed",
                                                 "settled_at": _now()}})
    return {"ok": True, "action": action}
