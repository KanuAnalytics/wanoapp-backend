"""The official Wano Team thread ("wano_team" switch).

One trustworthy place for updates, tips and account notices, from the account
an owner marked official (users.official, the badge). Users can mute the
updates, never the account notices (warnings, limits, appeal answers).

- Broadcasts (team_posts): written by owners, with an audience like What's
  new (creators or viewers, countries, platform, teens or not). Never pushed:
  announcements must not wear out notifications.
- Personal notices (team_inbox): one person only. Delivered:
    app 2.3.60+: the thread screen (GET /team/thread);
    older apps, adults: also as a message in Messages from the Wano Team
      account (a real conversation, opened as an accepted chat);
    older apps, teens: Messages never reaches under-18s, so a push that names
      nothing ("You have a note from Wano") points them to update.
- Wano Team never starts a conversation except to carry these notices, and
  the text always says Wano never asks for a password or code.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

SWITCH = "wano_team"
NEVER_ASK = "Wano will never ask for your password or a code."
KINDS = ("update", "tip", "safety", "event")
NOTICE_KINDS = ("warning", "limit", "hidden", "appeal", "account")
PAGE = 30


def _now() -> datetime:
    return datetime.utcnow()


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH)


async def require_on(db):
    if not await on(db):
        raise HTTPException(404, "Not found")


async def team_account(db) -> Optional[dict]:
    return await db.users.find_one({"official": True, "is_active": True},
                                   {"username": 1, "display_name": 1, "profile_picture": 1},
                                   sort=[("_id", 1)])


def _bucket(uid, key: str) -> int:
    return int(hashlib.sha1(f"{uid}:{key}".encode()).hexdigest()[:8], 16) % 100


# ------------------------------------------------------------------ audience
AUDIENCE_KEYS = ("who", "countries", "platform", "teens", "languages")


def clean_audience(a) -> dict:
    a = a if isinstance(a, dict) else {}
    who = a.get("who") if a.get("who") in ("all", "creators", "viewers") else "all"
    countries = [c.lower()[:3] for c in (a.get("countries") or []) if isinstance(c, str) and c.strip()][:60]
    langs = [c.lower()[:5] for c in (a.get("languages") or []) if isinstance(c, str) and c.strip()][:20]
    platform = a.get("platform") if a.get("platform") in ("any", "android", "ios") else "any"
    teens = a.get("teens") is not False          # False: adults only (never sent to under-18s)
    return {"who": who, "countries": countries, "languages": langs, "platform": platform, "teens": teens}


def matches(aud: dict, user: Optional[dict], platform: Optional[str] = None, lang: Optional[str] = None) -> bool:
    from app.services import home_ground
    aud = clean_audience(aud)
    if user is None:
        return aud["who"] in ("all", "viewers") and not aud["countries"] and aud["teens"] is True
    if not aud["teens"] and home_ground.is_minor(user):
        return False
    if aud["who"] == "creators" and not (user.get("videos_count") or 0) > 0:
        return False
    if aud["who"] == "viewers" and (user.get("videos_count") or 0) > 0:
        return False
    if aud["countries"]:
        c = ((user.get("localization") or {}).get("country") or "").lower()
        if c not in aud["countries"]:
            return False
    if aud["platform"] != "any" and platform and platform != aud["platform"]:
        return False
    if aud["languages"] and lang and lang.lower()[:2] not in [x[:2] for x in aud["languages"]]:
        return False
    return True


_MONEY = ("payout", "cash out", "cashout", "withdraw", "earn money", "get paid", "mobile money", "free airtime",
          "free data")


def money_promise(text: str) -> bool:
    t = " ".join((text or "").lower().split())
    return any(w in t for w in _MONEY)


# ------------------------------------------------------------------ broadcasts
async def post(db, staff, body: dict) -> dict:
    from app.services.messages import clean_text
    title = clean_text(body.get("title"))[:80]
    text = clean_text(body.get("body"))[:600]
    if not title or not text:
        raise HTTPException(400, "Write a title and a message")
    kind = body.get("kind") if body.get("kind") in KINDS else "update"
    image = body.get("image") or None
    from app.services.whats_new import _cdn
    if image is not None and not _cdn(image):
        raise HTTPException(400, "Pictures must be on Wano's own storage")
    alt = clean_text(body.get("alt") or "")[:200]
    if image and not alt:
        raise HTTPException(400, "Describe the picture (for people using a screen reader)")
    if money_promise(f"{title} {text}"):
        raise HTTPException(400, "Cash payouts are locked: no announcements about earning money")
    doc = {"title": title, "body": text, "kind": kind, "image": image, "alt": alt,
           "audience": clean_audience(body.get("audience")), "at": _now(), "by": staff.oid, "active": True}
    res = await db.team_posts.insert_one(doc)
    from app.services import admin_log
    await admin_log.record(db, staff.id, "team_post", {"id": str(res.inserted_id), "title": title})
    return {"id": str(res.inserted_id)}


async def remove_post(db, staff, pid: str) -> dict:
    if not ObjectId.is_valid(pid):
        raise HTTPException(404, "Not found")
    await db.team_posts.update_one({"_id": ObjectId(pid)}, {"$set": {"active": False}})
    return {"ok": True}


# ------------------------------------------------------------------ personal notices
async def notify(db, user_id, kind: str, title: str, text: str, ref: Optional[str] = None) -> bool:
    """One person's account notice. Never raises; True when stored now."""
    try:
        uid = ObjectId(str(user_id))
        doc = {"user": uid, "kind": kind if kind in NOTICE_KINDS else "account", "title": title[:80],
               "body": f"{text}\n\n{NEVER_ASK}"[:1200], "at": _now()}
        if ref:
            doc["_id"] = f"{ref}:{uid}"
        try:
            await db.team_inbox.insert_one(doc)
        except DuplicateKeyError:
            return False
        await _old_app_route(db, uid, doc)
        return True
    except Exception:
        logger.warning("team notice not stored", exc_info=True)
        return False


async def _old_app_route(db, uid: ObjectId, doc: dict) -> None:
    """Reach apps without the thread screen (before 2.3.60)."""
    from app.services import home_ground
    u = await db.users.find_one({"_id": uid}, {"date_of_birth": 1, "expo_push_tokens": 1, "is_active": 1})
    if not u or u.get("is_active") is False:
        return
    if home_ground.is_minor(u):
        tokens = [t for t in (u.get("expo_push_tokens") or []) if isinstance(t, str)][:5]
        if tokens:
            try:
                from app.services.expo import send_push_batch_data
                await asyncio.to_thread(send_push_batch_data, [
                    {"to": t, "title": "Wano", "body": "You have a note from Wano. Update the app to read it.",
                     "sound": "default", "data": {"type": "team_notice"}} for t in tokens])
            except Exception:
                logger.warning("teen notice push failed", exc_info=True)
        return
    team = await team_account(db)
    from app.services import creator_push
    if not team or not await creator_push.on(db, "messages"):
        return
    a, b = sorted([team["_id"], uid], key=str)
    key = f"{a}:{b}"
    now = _now()
    conv = await db.conversations.find_one({"key": key})
    if not conv:
        try:
            await db.conversations.insert_one({"key": key, "members": [a, b], "state": "active", "official": True,
                                               "requester": None, "requester_sent": 0, "created_at": now,
                                               "last_at": now, "m": {str(a): {"unread": 0}, str(b): {"unread": 0}}})
        except DuplicateKeyError:
            pass
        conv = await db.conversations.find_one({"key": key})
    text = f"{doc['title']}\n\n{doc['body']}"[:1000]
    res = await db.messages.insert_one({"conversation_id": conv["_id"], "sender_id": team["_id"], "kind": "text",
                                        "text": text, "created_at": now, "official": True})
    await db.conversations.update_one({"_id": conv["_id"]}, {
        "$set": {"last_message": {"id": res.inserted_id, "sender": team["_id"], "kind": "text", "text": text[:200],
                                  "at": now}, "last_at": now, f"m.{uid}.hidden": False},
        "$inc": {f"m.{uid}.unread": 1}})


# ------------------------------------------------------------------ the thread
async def thread(db, me: str, platform: Optional[str] = None, lang: Optional[str] = None) -> dict:
    uid = ObjectId(me)
    u = await db.users.find_one({"_id": uid}, {"date_of_birth": 1, "videos_count": 1, "localization": 1,
                                               "created_at": 1, "team": 1}) or {}
    state = u.get("team") or {}
    since = _now() - timedelta(days=180)
    items = []
    async for p in db.team_posts.find({"active": True, "at": {"$gte": since}}).sort("at", -1).limit(PAGE):
        if matches(p.get("audience") or {}, u, platform, lang):
            items.append({"id": str(p["_id"]), "kind": p.get("kind"), "title": p.get("title"), "body": p.get("body"),
                          "image": p.get("image"), "alt": p.get("alt"), "at": p.get("at"), "personal": False})
    async for n in db.team_inbox.find({"user": uid}).sort("at", -1).limit(PAGE):
        items.append({"id": str(n["_id"]), "kind": n.get("kind"), "title": n.get("title"), "body": n.get("body"),
                      "at": n.get("at"), "personal": True})
    items.sort(key=lambda x: x["at"], reverse=True)
    read_at = state.get("read_at")
    unread_personal = sum(1 for i in items if i["personal"] and (not read_at or i["at"] > read_at))
    unread_updates = 0 if state.get("muted") else sum(
        1 for i in items if not i["personal"] and (not read_at or i["at"] > read_at))
    team = await team_account(db)
    return {"account": {"id": str(team["_id"]), "display_name": team.get("display_name"),
                        "profile_picture": team.get("profile_picture"), "official": True} if team else None,
            "items": items[:PAGE], "unread": unread_personal + unread_updates,
            "muted": bool(state.get("muted")), "say": NEVER_ASK}


async def mark_read(db, me: str) -> dict:
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"team.read_at": _now()}})
    return {"ok": True}


async def mute(db, me: str, muted: bool) -> dict:
    """Mutes the updates only; account notices always count and show."""
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"team.muted": bool(muted)}})
    return {"muted": bool(muted)}


# ------------------------------------------------------------------ delivery of due notices
async def deliver_due(db, now: Optional[datetime] = None, batch: int = 100) -> int:
    """Warnings whose undo window (and random delay) has passed go to the thread
    (and to older apps). Called from the scheduled-post tick. Never raises."""
    now = now or _now()
    n = 0
    try:
        if not await on(db):
            return 0
        async for w in db.warnings.find({"visible_after": {"$lte": now}, "delivered": {"$exists": False},
                                         "withdrawn": {"$ne": True}}).limit(batch):
            a = await db.mod_actions.find_one({"_id": w["action"]}, {"state": 1})
            if (a or {}).get("state") == "undone":
                await db.warnings.update_one({"_id": w["_id"]}, {"$set": {"delivered": "undone"}})
                continue
            r = await db.warnings.update_one({"_id": w["_id"], "delivered": {"$exists": False}},
                                             {"$set": {"delivered": now}})
            if not r.modified_count:
                continue
            await notify(db, w["user"], "warning", w.get("title") or "A note about your account",
                         f"{w.get('text', '')}\n\nYou can see what it was about, and appeal, in the app.",
                         ref=f"warn:{w['_id']}")
            n += 1
    except Exception:
        logger.warning("team delivery failed", exc_info=True)
    return n
