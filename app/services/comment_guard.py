"""Comment safety ("comment_guard" switch, Nov 2026).

Every app version from 2.1.1 to 2.3.58 ignores a refused comment: no error,
the text just stays in the box. So nothing here refuses. A comment that
breaks a rule is saved but HELD: only its author sees it, it adds nothing to
any count, and nobody is notified. Spammers can't tell, honest people aren't
confused, and old apps keep working.

Held when:
  - it has a link (scams and other-app adverts; quick posts already block them)
  - the author is going too fast: more than PER_MINUTE comments in a minute,
    PER_POST_HOUR on one post in an hour, or PER_DAY in a day
  - the author and the post's creator (or, for a reply, the person replied
    to) have blocked each other, either way

Reading: comments by people the viewer blocked, or who blocked the viewer,
are left out, and held comments are shown only to their author. Signed-out
people can read comments (most first-time viewers aren't signed in).

Reporting: a comment reported by REPORTS_TO_HIDE accounts at least
TRUSTED_ACCOUNT_DAYS old is hidden and goes to Admin; the post's creator can
also delete it.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

SWITCH = "comment_guard"
PER_MINUTE = 5
PER_POST_HOUR = 20
PER_DAY = 200
REPORTS_TO_HIDE = 3
TRUSTED_ACCOUNT_DAYS = 3
REPORTS_PER_DAY = 20
REASONS = ("spam", "harassment", "hate", "scam", "nudity", "violence", "self_harm", "false_information", "other")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH)


def has_link(text: str) -> bool:
    from app.services import messages, quick_posts
    t = text or ""
    return messages.has_link(t) or bool(quick_posts._MORE_LINKS.search(t.lower()))


def _ids(v) -> Set[ObjectId]:
    return {x for x in v if isinstance(x, ObjectId)} if isinstance(v, list) else set()


def blocked_between(user: Optional[dict], other_id) -> bool:
    """True if `user` blocked `other_id` or was blocked by them."""
    if not user or not isinstance(other_id, ObjectId):
        return False
    return other_id in _ids(user.get("blocked_users")) or other_id in _ids(user.get("blocked_by"))


async def _take(db, key: str, cap: int, window: timedelta, bucket: str) -> bool:
    """Count one against `cap` for this window; False (and not counted) when over."""
    doc = await db.comment_limits.find_one_and_update(
        {"_id": f"{key}:{bucket}"}, {"$inc": {"n": 1}, "$setOnInsert": {"at": _now()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if (doc or {}).get("n", 0) > cap:
        await db.comment_limits.update_one({"_id": f"{key}:{bucket}"}, {"$inc": {"n": -1}})
        return False
    return True


async def too_fast(db, user_id: ObjectId, video_id: ObjectId, now: Optional[datetime] = None,
                   own_post: bool = False) -> bool:
    """A creator answering fans on their own post only has the daily cap."""
    now = now or _now()
    u = str(user_id)
    if not own_post:
        if not await _take(db, f"m:{u}", PER_MINUTE, timedelta(minutes=1), now.strftime("%Y%m%d%H%M")):
            return True
        if not await _take(db, f"p:{u}:{video_id}", PER_POST_HOUR, timedelta(hours=1), now.strftime("%Y%m%d%H")):
            return True
    if not await _take(db, f"d:{u}", PER_DAY, timedelta(days=1), now.strftime("%Y%m%d")):
        return True
    return False


# ------------------------------------------------------------------ teens
# teen_comments switch (safety_record "teen_comments:v1"): who may comment on a
# teen's post. Under 16: friends only (follow each other), locked. 16-17:
# people the teen follows unless they chose everyone. Offensive words held.
TEEN_SWITCH = "teen_comments"
TEEN_RULE = "teen_comments:v1"
TEEN_CHOICES = ("following", "everyone")
# Offensive words held on teen posts. Admin can add more (config
# "teen_blocked_words"); matched as whole words, ignoring case and spacing tricks.
BLOCKED_WORDS = ("fuck", "fucking", "shit", "bitch", "bastard", "slut", "whore", "dick", "pussy", "cunt",
                 "nigga", "nigger", "faggot", "retard", "kill yourself", "kys", "nude", "nudes", "sexy",
                 "send pics", "hookup", "sugar daddy", "malaya", "kuma", "msenge", "shenzi", "fala")


async def teen_on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, TEEN_SWITCH)


def _age(user: dict) -> Optional[int]:
    from app.services import home_ground
    if not home_ground.is_minor(user):
        return None
    return home_ground.age_of(user.get("date_of_birth"))


async def _blocked_words(db) -> List[str]:
    cfg = await db.config.find_one({"teen_blocked_words": {"$exists": True}}, {"teen_blocked_words": 1}) or {}
    extra = [w.lower().strip() for w in (cfg.get("teen_blocked_words") or []) if isinstance(w, str) and w.strip()]
    return list(BLOCKED_WORDS) + extra


def _normal(text: str) -> str:
    t = (text or "").lower()
    t = t.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s", "!": "i"}))
    t = re.sub(r"[^a-z\s]", " ", t)
    return " " + re.sub(r"\s+", " ", t) + " "


def _join_spelled(t: str) -> str:
    """'f u c k' -> 'fuck' (runs of single letters), leaving real words alone."""
    return re.sub(r"(?:\b[a-z] ){2,}[a-z]\b", lambda m: m.group(0).replace(" ", ""), t)


def has_blocked_word(text: str, words: Iterable[str]) -> bool:
    t = _normal(text)
    joined = _join_spelled(t)
    for w in words:
        w = " ".join(w.lower().split())
        if w and (f" {w} " in t or f" {w} " in joined):
            return True
    return False


async def teen_reason(db, *, user: dict, owner: Optional[dict], content: str) -> Optional[str]:
    """Why a comment on a teen's post is held under teen_comments:v1, or None."""
    if not owner or owner.get("_id") == user.get("_id"):
        return None
    age = _age(owner)
    if age is None:
        return None
    me = user["_id"]
    they_follow_me = me in _ids(owner.get("following"))
    i_follow_them = owner["_id"] in _ids(user.get("following"))
    if age < 16:
        if not (they_follow_me and i_follow_them):
            return "teen_friends_only"
    elif owner.get("teen_comments") != "everyone" and not they_follow_me:
        return "teen_following_only"
    if has_blocked_word(content, await _blocked_words(db)):
        return "teen_word_filter"
    return None


async def hold_reason(db, *, user: dict, video: dict, parent: Optional[dict], content: str,
                      guard: bool = True, teen: bool = False) -> Optional[str]:
    """Why a new comment should be held, or None."""
    creator = video.get("creator_id")
    if teen:
        owner = await db.users.find_one({"_id": creator}, {"date_of_birth": 1, "following": 1, "teen_comments": 1})
        why = await teen_reason(db, user=user, owner=owner, content=content)
        if why:
            return why
    if not guard:
        return None
    if blocked_between(user, creator):
        return "blocked"
    if parent is not None and blocked_between(user, parent.get("user_id")):
        return "blocked"
    if has_link(content):
        return "link"
    if await too_fast(db, user["_id"], video["_id"], own_post=creator == user["_id"]):
        return "too_fast"
    return None


def visible_filter(viewer: Optional[dict]) -> dict:
    """Mongo filter for comments a viewer may see (with the switch on)."""
    hidden_people: List[ObjectId] = []
    if viewer:
        hidden_people = list(_ids(viewer.get("blocked_users")) | _ids(viewer.get("blocked_by")))
    me = viewer.get("_id") if viewer else None
    shown = {"held": {"$exists": False}, "hidden": {"$ne": True}}
    q: dict = {"$or": [shown, {"user_id": me}]} if me is not None else dict(shown)
    if hidden_people:
        q = {"$and": [q, {"user_id": {"$nin": hidden_people}}]}
    return q


async def viewer_doc(db, current_user: Optional[str]) -> Optional[dict]:
    if not current_user or not ObjectId.is_valid(str(current_user)):
        return None
    return await db.users.find_one({"_id": ObjectId(current_user)},
                                   {"blocked_users": 1, "blocked_by": 1, "created_at": 1})


async def report(db, *, me_id: str, comment: dict, reason: str) -> dict:
    """Report a comment. Hidden after REPORTS_TO_HIDE reports from established accounts."""
    from fastapi import HTTPException
    me = ObjectId(me_id)
    if comment.get("user_id") == me:
        raise HTTPException(status_code=400, detail="You can't report your own comment")
    try:
        await db.comment_reports.insert_one({
            "_id": f"{comment['_id']}:{me}", "comment_id": comment["_id"], "video_id": comment.get("video_id"),
            "reporter_id": me, "owner_id": comment.get("user_id"), "reason": reason,
            "text": (comment.get("content") or "")[:500], "status": "open", "created_at": _now()})
    except DuplicateKeyError:
        return {"reported": True}
    reporter = await db.users.find_one({"_id": me}, {"created_at": 1})
    made = (reporter or {}).get("created_at")
    if isinstance(made, datetime) and made.tzinfo is not None:
        made = made.astimezone(timezone.utc).replace(tzinfo=None)
    trusted = isinstance(made, datetime) and made < _now() - timedelta(days=TRUSTED_ACCOUNT_DAYS)
    if trusted and await _take(db, f"r:{me}", REPORTS_PER_DAY, timedelta(days=1), _now().strftime("%Y%m%d")):
        upd = await db.comments.find_one_and_update({"_id": comment["_id"]}, {"$inc": {"reports": 1}},
                                                    return_document=ReturnDocument.AFTER)
        if (upd or {}).get("reports", 0) >= REPORTS_TO_HIDE and upd.get("hidden") is not True:
            await hide(db, upd)
    return {"reported": True}


async def hide(db, comment: dict) -> None:
    """Hide a live comment and take it off the counts (once)."""
    res = await db.comments.update_one({"_id": comment["_id"], "hidden": {"$ne": True}},
                                       {"$set": {"hidden": True, "hidden_at": _now()}})
    if res.modified_count and comment.get("held") is None and comment.get("is_active", True):
        await db.videos.update_one({"_id": comment.get("video_id"), "comments_count": {"$gt": 0}},
                                   {"$inc": {"comments_count": -1}})
        if comment.get("parent_id"):
            await db.comments.update_one({"_id": comment["parent_id"], "replies_count": {"$gt": 0}},
                                         {"$inc": {"replies_count": -1}})


async def open_reports(db, limit: int = 100) -> List[dict]:
    """One row per reported comment, newest report first."""
    rows = await db.comment_reports.aggregate([
        {"$match": {"status": "open"}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$comment_id", "video_id": {"$first": "$video_id"}, "owner_id": {"$first": "$owner_id"},
                    "text": {"$first": "$text"}, "reasons": {"$addToSet": "$reason"}, "n": {"$sum": 1},
                    "last": {"$first": "$created_at"}}},
        {"$sort": {"last": -1}},
        {"$limit": limit},
    ]).to_list(limit)
    out = []
    for r in rows:
        c = await db.comments.find_one({"_id": r["_id"]}, {"hidden": 1, "is_active": 1})
        out.append({"comment_id": str(r["_id"]), "video_id": str(r.get("video_id")),
                    "owner_id": str(r.get("owner_id")), "reason": ", ".join(sorted(r.get("reasons") or [])),
                    "text": r.get("text", ""), "reports": r.get("n", 0), "hidden": bool((c or {}).get("hidden")),
                    "deleted": not (c or {}).get("is_active", False), "created_at": r.get("last")})
    return out


# ------------------------------------------------------------------ order (comment_plus)
# The order with comment_plus on: pinned, then what the creator hearted, then
# the most liked (newest first on ties), with one spot (the 3rd) for a small
# account so big accounts don't always own the top. The small account must be
# established (signed up 3+ days ago), under SMALL_FOLLOWERS followers, and
# its comment must not be held or hidden (those never reach this list).
PLUS_SWITCH = "comment_plus"
SMALL_FOLLOWERS = 100
SMALL_SLOT = 2          # 0-based: the 3rd comment
RANK_SCAN = 500


async def plus_on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, PLUS_SWITCH)


def _rank_key(c: dict):
    # docs arrive newest first; the sort is stable, so ties stay newest first.
    likes = c.get("likes_count") if isinstance(c.get("likes_count"), int) else 0
    return (not c.get("is_pinned"), not c.get("is_hearted"), -likes)


async def ranked(db, docs: List[dict], users: dict) -> List[dict]:
    """Order a post's visible top-level comments (all of them, up to RANK_SCAN)."""
    out = sorted(docs, key=_rank_key)
    if len(out) <= SMALL_SLOT + 1:
        return out
    cutoff = _now() - timedelta(days=TRUSTED_ACCOUNT_DAYS)
    ids = list({c.get("user_id") for c in out[SMALL_SLOT:]})
    small = {u["_id"] async for u in db.users.find(
        {"_id": {"$in": ids}, "created_at": {"$lte": cutoff},
         "$or": [{"followers_count": {"$lt": SMALL_FOLLOWERS}}, {"followers_count": {"$exists": False}}]},
        {"_id": 1})}
    head = out[:SMALL_SLOT]
    if any(c.get("user_id") in small for c in head):
        return out
    pick = next((c for c in out[SMALL_SLOT:] if c.get("user_id") in small), None)
    if pick is None:
        return out
    rest = [c for c in out[SMALL_SLOT:] if c is not pick]
    return head + [pick] + rest


async def my_total(db, video: dict, viewer: Optional[dict]) -> int:
    """The count this person should see: the post's count plus their own held
    comments (so a held comment never shows up as a missing number)."""
    n = video.get("comments_count") if isinstance(video.get("comments_count"), int) else 0
    if viewer:
        # Held ones never counted and hidden ones were taken off; the author still
        # sees both, so both go back into their own number.
        n += await db.comments.count_documents({"video_id": video["_id"], "user_id": viewer["_id"],
                                                "is_active": True,
                                                "$or": [{"held": {"$exists": True}}, {"hidden": True}]})
    return max(0, n)


async def my_reply_counts(db, parent_ids: List[ObjectId], viewer_id) -> dict:
    """The viewer's own held or hidden replies under each of these comments.
    They never counted in replies_count, so without this the author would see
    "0 replies" under a comment that has their reply, and couldn't open it."""
    if not viewer_id or not parent_ids:
        return {}
    rows = await db.comments.aggregate([
        {"$match": {"parent_id": {"$in": parent_ids}, "user_id": viewer_id, "is_active": True,
                    "$or": [{"held": {"$exists": True}}, {"hidden": True}]}},
        {"$group": {"_id": "$parent_id", "n": {"$sum": 1}}}]).to_list(None)
    return {r["_id"]: r["n"] for r in rows}
