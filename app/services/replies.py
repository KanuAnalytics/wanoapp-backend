"""
Replies and duets: a post made in answer to another post.

POST /videos/post with reply_to=<video id> and reply_type "reply" | "duet"
(how the app lays it out; the backend only links them). The post saves
reply_to {video_id, creator_id, type}; the original's remix_count goes up and
its creator gets a push (at most TELLS_PER_DAY a day, in their daytime).

You can reply to public live posts whose creator allows remixes
(remix_enabled) and hasn't blocked you, and to any of your own posts.
GET /videos/{id}/replies lists the visible replies, newest first.
"""

import base64
import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

from app.services.creators import _showable

logger = logging.getLogger(__name__)

TELLS_PER_DAY = 3
MAX_PAGE = 50


def _visible() -> dict:
    return {**_showable(), "moderation.state": {"$nin": ["held", "removed"]}}


def _blocked(a: dict, b_id: ObjectId) -> bool:
    return any(isinstance(a.get(k), list) and b_id in a[k] for k in ("blocked_users", "blocked_by"))


async def target(db, viewer: str, video_id: str, kind: str) -> dict:
    """The reply_to to save, or raises."""
    if not ObjectId.is_valid(video_id or ""):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid reply_to")
    me = ObjectId(viewer)
    oid = ObjectId(video_id)
    v = await db.videos.find_one({"_id": oid, "is_active": True}, {"creator_id": 1, "remix_enabled": 1})
    if v and v.get("creator_id") != me and not await db.videos.find_one({"_id": oid, **_visible()}, {"_id": 1}):
        v = None
    if not v:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    creator = v.get("creator_id")
    if creator != me:
        owner = await db.users.find_one({"_id": creator, "is_active": {"$ne": False}},
                                        {"blocked_users": 1, "blocked_by": 1})
        if not owner:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        if _blocked(owner, me):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can't reply to this post")
        if v.get("remix_enabled") is False:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This creator turned off replies")
    who = await db.users.find_one({"_id": creator}, {"username": 1}) or {}
    out = {"video_id": oid, "creator_id": creator, "type": kind}
    if isinstance(who.get("username"), str):
        out["username"] = who["username"]   # shown as "Reply to @username"
    return out


async def after_create(db, reply_to: dict, reply_id: str, replier: str, scheduled: bool) -> None:
    if scheduled:
        return
    try:
        await db.videos.update_one({"_id": reply_to["video_id"]}, {"$inc": {"remix_count": 1}})
        if str(reply_to["creator_id"]) == str(replier):
            return
        await _tell(db, reply_to, reply_id, replier)
    except Exception:
        logger.exception("After-reply steps failed for %s", reply_id)


async def _tell(db, reply_to: dict, reply_id: str, replier: str) -> None:
    from app.services import growth_notifications as g
    now = g._utcnow()
    user = await db.users.find_one({"_id": reply_to["creator_id"], "is_active": {"$ne": False}},
                                   {"expo_push_tokens": 1, "localization": 1, "notification_prefs": 1})
    if not user or not g._tokens(user) or not g._prefs(user)["creator_tips"] or g._quiet(user, now):
        return
    if not await g._take_budget(db, user, now, "replies", TELLS_PER_DAY):
        return
    who = await db.users.find_one({"_id": ObjectId(replier)}, {"display_name": 1, "username": 1}) or {}
    name = who.get("display_name") or who.get("username") or "Someone"
    verb = "made a duet with" if reply_to["type"] == "duet" else "replied to"
    for t in g._tokens(user):
        await g._send_one(t, g._clip(f"{name} {verb} your post. Go see it!", g.BODY_MAX),
                          {"type": "reply", "video_id": reply_id, "reply_to": str(reply_to["video_id"])},
                          "New reply to your post")


def _cursor(doc: dict) -> str:
    raw = f"{doc['created_at'].isoformat()}|{doc['_id']}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _uncursor(c: str):
    try:
        at, oid = base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)).decode().split("|")
        return datetime.fromisoformat(at), ObjectId(oid)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cursor")


async def list_replies(db, video_id: str, viewer: Optional[str], cursor: Optional[str], limit: int) -> dict:
    from app.services import thumbnails
    if not ObjectId.is_valid(video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video ID")
    oid = ObjectId(video_id)
    original = await db.videos.find_one({"_id": oid, "is_active": True}, {"creator_id": 1})
    mine = original and viewer and str(original.get("creator_id")) == str(viewer)
    if not original or (not mine and not await db.videos.find_one({"_id": oid, **_visible()}, {"_id": 1})):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    q = {"reply_to.video_id": oid, **_visible()}
    # Left out in the query itself (not after), so a page is never empty while
    # more replies exist: banned accounts, and people the viewer blocked or
    # who blocked the viewer.
    hidden = set(await db.users.distinct("_id", {"is_active": False}))
    if viewer and ObjectId.is_valid(str(viewer)):
        me = await db.users.find_one({"_id": ObjectId(str(viewer))}, {"blocked_users": 1, "blocked_by": 1}) or {}
        for k in ("blocked_users", "blocked_by"):
            for x in me.get(k) or []:
                if isinstance(x, ObjectId):
                    hidden.add(x)
                elif isinstance(x, str) and ObjectId.is_valid(x):
                    hidden.add(ObjectId(x))
    if hidden:
        q["creator_id"] = {"$nin": list(hidden)}
    if cursor:
        at, last = _uncursor(cursor)
        q["$or"] = [{"created_at": {"$lt": at}}, {"created_at": at, "_id": {"$lt": last}}]
    limit = max(1, min(limit, MAX_PAGE))
    docs = await db.videos.find(q).sort([("created_at", -1), ("_id", -1)]).limit(limit + 1).to_list(limit + 1)
    more = len(docs) > limit
    docs = docs[:limit]
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": list({d.get("creator_id") for d in docs})}, "is_active": {"$ne": False}},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    items = []
    for d in docs:
        u = users.get(d.get("creator_id"))
        if not u:
            continue
        items.append({"id": str(d["_id"]), "type": (d.get("reply_to") or {}).get("type", "reply"),
                      "thumbnail": thumbnails.best_thumbnail(d), "created_at": d.get("created_at"),
                      "views_count": d.get("views_count", 0), "likes_count": d.get("likes_count", 0),
                      "creator": {"id": str(u["_id"]), "username": u.get("username"),
                                  "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
    return {"items": items, "next_cursor": _cursor(docs[-1]) if more and docs else None}


async def feed_links(db, docs: list) -> dict:
    """For a page of posts: {reply video id: {"video_id", "type", "username"}}
    for replies whose original is still visible (one query for the page)."""
    refs = {}
    for d in docs:
        rt = d.get("reply_to")
        if isinstance(rt, dict) and isinstance(rt.get("video_id"), ObjectId):
            refs[d["_id"]] = rt
    if not refs:
        return {}
    ids = list({rt["video_id"] for rt in refs.values()})
    alive = {v["_id"]: v async for v in db.videos.find({"_id": {"$in": ids}, **_visible()}, {"creator_id": 1})}
    creators = list({v["creator_id"] for v in alive.values() if v.get("creator_id")})
    # Originals by banned or closed accounts aren't linked either.
    names = {u["_id"]: u.get("username") async for u in db.users.find(
        {"_id": {"$in": creators}, "is_active": {"$ne": False}}, {"username": 1})} if creators else {}
    out = {}
    for vid, rt in refs.items():
        orig = alive.get(rt["video_id"])
        if not orig or orig.get("creator_id") not in names:
            continue
        name = rt.get("username") if isinstance(rt.get("username"), str) else names.get(orig.get("creator_id"))
        out[vid] = {"video_id": str(rt["video_id"]),
                    "type": rt.get("type") if rt.get("type") in ("reply", "duet") else "reply",
                    "username": name}
    return out


def count_of(doc: dict) -> int:
    n = doc.get("remix_count")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
