"""Safe moderation ("safe_ban" switch, Nov 2026): freeze and hide, never wipe.

Before this, banning an account anonymised it and deleted its pictures and
every video from Cloudflare at once, with no reason and no undo: a wrong tap
on the team phone couldn't be fixed and evidence was destroyed. Now:

- Every action names a reason (REASONS), goes into mod_actions (who, when,
  why) and the admin log, and can be undone for UNDO minutes. The person is
  told only after the undo window (notices()).
- FREEZE (suspend) hides everything and deletes nothing:
    the account can't sign in (users.is_active False, which every feed,
    search, hashtag and reply query already treats as gone); its posts go
    private (moderation.frozen, the old privacy kept) and out of Recombee;
    scheduled posts pause; its comments elsewhere are hidden and taken off
    the counts once; its push tokens are put aside (no pushes at all).
    Messages it sent stay with the people who got them (they may be evidence).
  UNFREEZE puts back exactly what the freeze changed, nothing else.
- LIMIT: for HOURS, no posting, commenting or messaging. Reporting, blocking,
  deleting their own posts, contacting Wano Team and appealing always work.
- HIDE a post or a comment: hidden for everyone, media kept, undoable.
- WIPE (the old ban): owners only, from the Admin page, only for an account
  frozen at least WIPE_AFTER_DAYS and not marked "keep as evidence".
- KEEP AS EVIDENCE: no wipe and no deletion while it's set.
- Staff actions are rate-limited in the database (STAFF_ACTIONS_PER_HOUR); each
  action carries a one-time key, so a double tap or a resend does it once.
- With the switch on, the existing DELETE /users/ban/{id} (the app's Ban on
  the team phone today) freezes instead of wiping.

Warnings (the "warnings" switch) live in mod_warnings.py and use the same records.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

SWITCH = "safe_ban"
UNDO = timedelta(minutes=10)
WIPE_AFTER_DAYS = 30
STAFF_ACTIONS_PER_HOUR = 60
BIG_ACCOUNT_FOLLOWERS = 10_000          # freezing these needs an admin, never a moderator alone
LIMIT_HOURS = (24, 72, 168)

REASONS = {
    "spam": "Spam",
    "scam": "Scam or fraud",
    "harassment": "Bullying or harassment",
    "hate": "Hate",
    "nudity": "Nudity or sexual content",
    "violence": "Violence or threats",
    "self_harm": "Suicide or self-harm",
    "child_safety": "Child safety",
    "impersonation": "Pretending to be someone else",
    "false_information": "False information",
    "copyright": "Someone else's content",
    "other": "Other",
}
# Serious harm: straight to suspension, never just a warning.
SERIOUS = ("child_safety", "violence", "scam")


def _now() -> datetime:
    return datetime.utcnow()


async def on(db) -> bool:
    from app.services import creator_push
    try:
        return await creator_push.on(db, SWITCH)
    except Exception:
        return False                  # can't read the switch: the old behaviour


async def require_on(db):
    if not await on(db):
        raise HTTPException(404, "Not found")


def _oid(v, what="id") -> ObjectId:
    if isinstance(v, ObjectId):
        return v
    if not isinstance(v, str) or not ObjectId.is_valid(v):
        raise HTTPException(400, f"Invalid {what}")
    return ObjectId(v)


async def _ensure_indexes(db):
    try:
        await db.mod_actions.create_index("key", unique=True, sparse=True)
        await db.mod_actions.create_index([("target_user", 1), ("at", -1)])
        await db.mod_actions.create_index([("by", 1), ("at", -1)])
    except Exception:
        pass


async def _rate(db, staff_oid: ObjectId):
    n = await db.mod_actions.count_documents({"by": staff_oid, "at": {"$gte": _now() - timedelta(hours=1)}})
    if n >= STAFF_ACTIONS_PER_HOUR:
        # A burst like this is either a stolen phone or a mistake: stop and tell owners.
        from app.services import staff_alerts
        await staff_alerts.urgent(db, f"staffrate:{staff_oid}:{_now():%Y%m%d%H}")
        raise HTTPException(429, "Too many actions in an hour. Ask an owner if this is real.")


async def _record(db, *, staff, kind: str, reason: str, key: Optional[str], target_user=None,
                  target_post=None, target_comment=None, note: str = "", extra: Optional[dict] = None) -> dict:
    """Write the action first (the one-time key makes a resend a no-op)."""
    if reason not in REASONS:
        raise HTTPException(400, "Pick a reason")
    await _ensure_indexes(db)
    await _rate(db, staff.oid)
    doc = {"kind": kind, "reason": reason, "note": (note or "")[:500], "by": staff.oid, "by_role": staff.role,
           "by_name": staff.user.get("username"), "via": staff.via, "at": _now(),
           "undo_until": _now() + UNDO, "state": "pending",
           "target_user": target_user, "target_post": target_post, "target_comment": target_comment,
           **(extra or {})}
    if key:
        doc["key"] = f"{staff.id}:{key[:80]}"
    try:
        res = await db.mod_actions.insert_one(doc)
    except DuplicateKeyError:
        prev = await db.mod_actions.find_one({"key": doc["key"]})
        raise _Already(prev)
    doc["_id"] = res.inserted_id
    from app.services import admin_log
    await admin_log.record(db, staff.id, f"mod_{kind}", {
        "action": str(doc["_id"]), "reason": reason,
        **({"user": str(target_user)} if target_user else {}),
        **({"post": str(target_post)} if target_post else {}),
        **({"comment": str(target_comment)} if target_comment else {})})
    return doc


async def replay(db, staff, key: Optional[str]) -> Optional[dict]:
    """The action this one-time key already did (a double tap or a resend)."""
    if not key:
        return None
    prev = await db.mod_actions.find_one({"key": f"{staff.id}:{key[:80]}"})
    return public_action(prev) if prev else None


class _Already(Exception):
    def __init__(self, prev):
        self.prev = prev


def public_action(a: Optional[dict]) -> Optional[dict]:
    if not a:
        return None
    return {"id": str(a["_id"]), "kind": a.get("kind"), "reason": a.get("reason"),
            "reason_label": REASONS.get(a.get("reason"), ""), "state": a.get("state"),
            "at": a.get("at"), "undo_until": a.get("undo_until"), "by": a.get("by_name"),
            "user": str(a["target_user"]) if a.get("target_user") else None,
            "post": str(a["target_post"]) if a.get("target_post") else None,
            "comment": str(a["target_comment"]) if a.get("target_comment") else None,
            "hours": a.get("hours"), "rule": a.get("rule")}


async def _done(db, action: dict, **extra) -> dict:
    await db.mod_actions.update_one({"_id": action["_id"]}, {"$set": {"state": "done", **extra}})
    return public_action(await db.mod_actions.find_one({"_id": action["_id"]}))


async def _recombee_privacy(video_id, privacy: str):
    try:
        from app.services.moderation import _sync_privacy
        await _sync_privacy(video_id, privacy)
    except Exception:
        logger.warning("recombee privacy not synced", exc_info=True)


# ------------------------------------------------------------------ targets
async def _target_user(db, staff, user_id) -> dict:
    oid = _oid(user_id, "user id")
    user = await db.users.find_one({"_id": oid})
    if not user:
        raise HTTPException(404, "User not found")
    if oid == staff.oid:
        raise HTTPException(400, "You can't do this to your own account")
    from app.services import staff as staff_svc
    target_role = await staff_svc.effective_role(db, user)
    if target_role is not None and staff_svc.RANK[target_role] >= staff_svc.RANK[staff.role]:
        raise HTTPException(403, "Staff at your level or above can only be handled by an owner on the Admin page")
    if str(user.get("username", "")).startswith("deleted_user-"):
        raise HTTPException(409, "This account was banned the old way and is already wiped")
    return user


def _big(user: dict) -> bool:
    n = user.get("followers_count")
    return isinstance(n, int) and n >= BIG_ACCOUNT_FOLLOWERS


# ------------------------------------------------------------------ freeze
async def freeze(db, staff, user_id, reason: str, note: str = "", key: Optional[str] = None,
                 evidence: bool = False) -> dict:
    if (prev := await replay(db, staff, key)):
        return prev
    user = await _target_user(db, staff, user_id)
    if _big(user) and staff.role == "moderator":
        raise HTTPException(403, f"Accounts with {BIG_ACCOUNT_FOLLOWERS:,}+ followers need an admin")
    if (user.get("mod_state") or {}).get("state") == "frozen":
        raise HTTPException(409, "Already suspended")
    try:
        a = await _record(db, staff=staff, kind="freeze", reason=reason, key=key, target_user=user["_id"], note=note)
    except _Already as e:
        return public_action(e.prev)
    await apply_freeze(db, user["_id"], a["_id"], reason, evidence=evidence)
    return await _done(db, a)


async def apply_freeze(db, uid: ObjectId, action_id: ObjectId, reason: str, evidence: bool = False) -> None:
    now = _now()
    user = await db.users.find_one_and_update(
        {"_id": uid, "mod_state.state": {"$ne": "frozen"}},
        {"$set": {"mod_state": {"state": "frozen", "since": now, "reason": reason, "action": action_id,
                                "evidence": bool(evidence), "was_active": True},
                  "is_active": False, "updated_at": now}},
        return_document=ReturnDocument.BEFORE)
    if not user:
        return
    if user.get("is_active") is False:
        await db.users.update_one({"_id": uid}, {"$set": {"mod_state.was_active": False}})
    tokens = user.get("expo_push_tokens") or []
    if tokens:
        await db.users.update_one({"_id": uid}, {"$set": {"mod_state.push_tokens": tokens},
                                                 "$unset": {"expo_push_tokens": ""}})
    # Posts: private, old privacy kept; scheduled ones pause.
    async for v in db.videos.find({"creator_id": uid, "is_active": True, "moderation.frozen": {"$ne": True}},
                                  {"privacy": 1}):
        await db.videos.update_one({"_id": v["_id"]}, {"$set": {
            "moderation.frozen": True, "moderation.frozen_prev_privacy": v.get("privacy") or "public",
            "moderation.frozen_by": action_id, "privacy": "private"}})
        await _recombee_privacy(v["_id"], "private")
    await db.videos.update_many({"creator_id": uid, "status": "scheduled"},
                                {"$set": {"status": "paused", "moderation.frozen": True,
                                          "moderation.frozen_by": action_id, "moderation.frozen_prev_status": "scheduled"}})
    # Comments elsewhere: hidden and off the counts, once each.
    async for c in db.comments.find({"user_id": uid, "is_active": True, "mod_hidden": {"$ne": True}},
                                    {"video_id": 1, "parent_id": 1, "held": 1, "hidden": 1}):
        await _hide_comment_doc(db, c, f"freeze:{action_id}")


async def unfreeze(db, staff, user_id, reason: str = "other", note: str = "", key: Optional[str] = None) -> dict:
    if (prev := await replay(db, staff, key)):
        return prev
    oid = _oid(user_id, "user id")
    user = await db.users.find_one({"_id": oid})
    if not user or (user.get("mod_state") or {}).get("state") != "frozen":
        raise HTTPException(409, "Not suspended")
    try:
        a = await _record(db, staff=staff, kind="unfreeze", reason=reason, key=key, target_user=oid, note=note)
    except _Already as e:
        return public_action(e.prev)
    await apply_unfreeze(db, oid)
    return await _done(db, a)


async def apply_unfreeze(db, uid: ObjectId) -> None:
    user = await db.users.find_one_and_update(
        {"_id": uid, "mod_state.state": "frozen"},
        {"$set": {"updated_at": _now()}, "$unset": {"mod_state": ""}},
        return_document=ReturnDocument.BEFORE)
    if not user:
        return
    ms = user.get("mod_state") or {}
    sets = {"is_active": ms.get("was_active", True) is not False}
    if ms.get("push_tokens"):
        sets["expo_push_tokens"] = ms["push_tokens"]
    await db.users.update_one({"_id": uid}, {"$set": sets})
    async for v in db.videos.find({"creator_id": uid, "moderation.frozen": True},
                                  {"moderation": 1, "status": 1}):
        m = v.get("moderation") or {}
        upd = {"$unset": {"moderation.frozen": "", "moderation.frozen_prev_privacy": "",
                          "moderation.frozen_by": "", "moderation.frozen_prev_status": ""}}
        if m.get("frozen_prev_status") == "scheduled" and v.get("status") == "paused":
            upd["$set"] = {"status": "scheduled"}
        elif "frozen_prev_privacy" in m:
            # A post held for review while frozen keeps its review state.
            if m.get("state") != "held":
                upd["$set"] = {"privacy": m.get("frozen_prev_privacy") or "public"}
        await db.videos.update_one({"_id": v["_id"]}, upd)
        if (upd.get("$set") or {}).get("privacy"):
            await _recombee_privacy(v["_id"], upd["$set"]["privacy"])
    async for c in db.comments.find({"user_id": uid, "mod_hidden": True,
                                     "mod_hidden_by": {"$regex": "^freeze:"}}):
        await _unhide_comment_doc(db, c)


# ------------------------------------------------------------------ limit
async def limit(db, staff, user_id, reason: str, hours: int, note: str = "", key: Optional[str] = None) -> dict:
    if (prev := await replay(db, staff, key)):
        return prev
    if hours not in LIMIT_HOURS:
        raise HTTPException(400, "Limits are 24 hours, 3 days or 7 days")
    user = await _target_user(db, staff, user_id)
    if (user.get("mod_state") or {}).get("state") == "frozen":
        raise HTTPException(409, "Already suspended")
    try:
        a = await _record(db, staff=staff, kind="limit", reason=reason, key=key, target_user=user["_id"],
                          note=note, extra={"hours": hours})
    except _Already as e:
        return public_action(e.prev)
    until = _now() + timedelta(hours=hours)
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"mod_state": {
        "state": "limited", "since": _now(), "until": until, "reason": reason, "action": a["_id"]}}})
    return await _done(db, a, until=until)


async def ensure_can(db, user_id, what: str) -> None:
    """Raise 403 while this account is limited (posting, commenting, messaging)."""
    try:
        oid = ObjectId(str(user_id))
    except Exception:
        return
    u = await db.users.find_one({"_id": oid, "mod_state.state": "limited"}, {"mod_state": 1})
    if not u:
        return
    until = (u.get("mod_state") or {}).get("until")
    if isinstance(until, datetime) and until <= _now():
        await db.users.update_one({"_id": oid, "mod_state.state": "limited", "mod_state.until": until},
                                  {"$unset": {"mod_state": ""}})
        return
    when = until.strftime("%d %b, %H:%M UTC") if isinstance(until, datetime) else "later"
    raise HTTPException(403, f"Your account is limited until {when}, so you can't {what} for now. "
                             f"You can still report, block, delete your posts and appeal.")


# ------------------------------------------------------------------ posts and comments
async def hide_post(db, staff, post_id, reason: str, note: str = "", key: Optional[str] = None) -> dict:
    if (prev := await replay(db, staff, key)):
        return prev
    oid = _oid(post_id, "post id")
    v = await db.videos.find_one({"_id": oid})
    if not v or v.get("is_active") is False:
        raise HTTPException(404, "Post not found")
    if (v.get("moderation") or {}).get("mod_hidden"):
        raise HTTPException(409, "Already hidden")
    try:
        a = await _record(db, staff=staff, kind="hide_post", reason=reason, key=key, target_post=oid,
                          target_user=v.get("creator_id"), note=note)
    except _Already as e:
        return public_action(e.prev)
    await db.videos.update_one({"_id": oid}, {"$set": {
        "moderation.mod_hidden": True, "moderation.mod_hidden_prev_privacy": v.get("privacy") or "public",
        "moderation.mod_hidden_by": a["_id"], "privacy": "private"}})
    await _recombee_privacy(oid, "private")
    await db.reports.update_many({"video_id": oid, "status": "pending", "is_active": True},
                                 {"$set": {"status": "resolved", "reviewed_by": staff.oid, "reviewed_at": _now()}})
    return await _done(db, a)


async def unhide_post(db, oid: ObjectId) -> None:
    v = await db.videos.find_one({"_id": oid, "moderation.mod_hidden": True})
    if not v:
        return
    m = v.get("moderation") or {}
    upd = {"$unset": {"moderation.mod_hidden": "", "moderation.mod_hidden_prev_privacy": "", "moderation.mod_hidden_by": ""}}
    # Still frozen or held: stays private until that ends.
    if not m.get("frozen") and m.get("state") != "held":
        upd["$set"] = {"privacy": m.get("mod_hidden_prev_privacy") or "public"}
    elif m.get("frozen"):
        upd["$set"] = {"moderation.frozen_prev_privacy": m.get("mod_hidden_prev_privacy") or "public"}
    await db.videos.update_one({"_id": oid}, upd)
    if (upd.get("$set") or {}).get("privacy"):
        await _recombee_privacy(oid, upd["$set"]["privacy"])


async def _hide_comment_doc(db, c: dict, by: str) -> bool:
    res = await db.comments.update_one({"_id": c["_id"], "mod_hidden": {"$ne": True}},
                                       {"$set": {"mod_hidden": True, "mod_hidden_by": by, "mod_hidden_at": _now()}})
    if not res.modified_count:
        return False
    counted = c.get("held") is None and c.get("hidden") is not True
    if counted:
        await db.comments.update_one({"_id": c["_id"]}, {"$set": {"mod_hidden_counted": True}})
        await db.videos.update_one({"_id": c.get("video_id"), "comments_count": {"$gt": 0}},
                                   {"$inc": {"comments_count": -1}})
        if c.get("parent_id"):
            await db.comments.update_one({"_id": c["parent_id"], "replies_count": {"$gt": 0}},
                                         {"$inc": {"replies_count": -1}})
    return True


async def _unhide_comment_doc(db, c: dict) -> None:
    res = await db.comments.update_one({"_id": c["_id"], "mod_hidden": True},
                                       {"$unset": {"mod_hidden": "", "mod_hidden_by": "", "mod_hidden_at": "",
                                                   "mod_hidden_counted": ""}})
    if res.modified_count and c.get("mod_hidden_counted") and c.get("is_active", True):
        await db.videos.update_one({"_id": c.get("video_id")}, {"$inc": {"comments_count": 1}})
        if c.get("parent_id"):
            await db.comments.update_one({"_id": c["parent_id"]}, {"$inc": {"replies_count": 1}})


async def hide_comment(db, staff, comment_id, reason: str, note: str = "", key: Optional[str] = None) -> dict:
    if (prev := await replay(db, staff, key)):
        return prev
    oid = _oid(comment_id, "comment id")
    c = await db.comments.find_one({"_id": oid, "is_active": True})
    if not c:
        raise HTTPException(404, "Comment not found")
    if c.get("mod_hidden"):
        raise HTTPException(409, "Already hidden")
    try:
        a = await _record(db, staff=staff, kind="hide_comment", reason=reason, key=key, target_comment=oid,
                          target_user=c.get("user_id"), target_post=c.get("video_id"), note=note)
    except _Already as e:
        return public_action(e.prev)
    await _hide_comment_doc(db, c, f"staff:{a['_id']}")
    await db.comment_reports.update_many({"comment_id": oid, "status": "open"}, {"$set": {"status": "hidden"}})
    return await _done(db, a)


def comment_filter() -> dict:
    """Comments hidden by moderation are hidden from everyone (switch or not)."""
    return {"mod_hidden": {"$ne": True}}


# ------------------------------------------------------------------ undo
async def undo(db, staff, action_id) -> dict:
    oid = _oid(action_id, "action id")
    a = await db.mod_actions.find_one({"_id": oid})
    if not a:
        raise HTTPException(404, "Action not found")
    if a.get("state") == "undone":
        return public_action(a)
    if a.get("undo_until") and a["undo_until"] < _now():
        raise HTTPException(409, "Too late to undo; use the opposite action instead")
    if a["by"] != staff.oid and staff.role == "moderator":
        raise HTTPException(403, "Only the person who did it, or an admin, can undo")
    kind = a.get("kind")
    if kind == "freeze":
        await apply_unfreeze(db, a["target_user"])
    elif kind == "limit":
        await db.users.update_one({"_id": a["target_user"], "mod_state.action": a["_id"]}, {"$unset": {"mod_state": ""}})
    elif kind == "hide_post":
        await unhide_post(db, a["target_post"])
    elif kind == "hide_comment":
        c = await db.comments.find_one({"_id": a["target_comment"]})
        if c:
            await _unhide_comment_doc(db, c)
    elif kind == "warn":
        await db.warnings.update_one({"action": a["_id"]}, {"$set": {"withdrawn": True, "withdrawn_at": _now()}})
    elif kind == "unfreeze":
        u = await db.users.find_one({"_id": a["target_user"]})
        if u and (u.get("mod_state") or {}).get("state") != "frozen":
            await apply_freeze(db, a["target_user"], a["_id"], a.get("reason", "other"))
    await db.mod_actions.update_one({"_id": oid}, {"$set": {"state": "undone", "undone_at": _now(),
                                                             "undone_by": staff.oid}})
    from app.services import admin_log
    await admin_log.record(db, staff.id, "mod_undo", {"action": str(oid), "kind": kind})
    return public_action(await db.mod_actions.find_one({"_id": oid}))


# ------------------------------------------------------------------ evidence and wipe
async def set_evidence(db, staff, user_id, on_: bool) -> dict:
    oid = _oid(user_id, "user id")
    r = await db.users.update_one({"_id": oid, "mod_state.state": "frozen"}, {"$set": {"mod_state.evidence": bool(on_)}})
    if not r.matched_count:
        raise HTTPException(409, "Only a suspended account can be kept as evidence")
    from app.services import admin_log
    await admin_log.record(db, staff.id, "mod_evidence", {"user": str(oid), "on": bool(on_)})
    return {"evidence": bool(on_)}


async def evidence_hold(db, user_id) -> bool:
    try:
        oid = ObjectId(str(user_id))
    except Exception:
        return False
    return await db.users.find_one({"_id": oid, "mod_state.evidence": True}, {"_id": 1}) is not None


async def wipe(db, staff, user_id, confirm: str) -> dict:
    """The old ban, after the waiting period. Owners only; the caller checks the role."""
    oid = _oid(user_id, "user id")
    user = await db.users.find_one({"_id": oid})
    ms = (user or {}).get("mod_state") or {}
    if not user or ms.get("state") != "frozen":
        raise HTTPException(409, "Only a suspended account can be wiped")
    if ms.get("evidence"):
        raise HTTPException(409, "Kept as evidence: it can't be wiped until that's taken off")
    since = ms.get("since")
    if not isinstance(since, datetime) or since > _now() - timedelta(days=WIPE_AFTER_DAYS):
        raise HTTPException(409, f"Suspended for less than {WIPE_AFTER_DAYS} days")
    if confirm != user.get("username"):
        raise HTTPException(400, "Type the username to confirm")
    try:
        a = await _record(db, staff=staff, kind="wipe", reason=ms.get("reason") or "other", key=None, target_user=oid)
    except _Already as e:
        return public_action(e.prev)
    await db.mod_actions.update_one({"_id": a["_id"]}, {"$set": {"undo_until": _now()}})   # no undo
    # Put the posts back to their real state first, so the wipe deletes them all.
    await db.videos.update_many({"creator_id": oid, "status": "paused"}, {"$set": {"status": "scheduled"}})
    await old_ban(db, oid)
    await db.users.update_one({"_id": oid}, {"$set": {"mod_state.state": "wiped", "mod_state.wiped_at": _now()}})
    return await _done(db, a)


async def old_ban(db, oid: ObjectId) -> None:
    """Anonymise and delete everything (what DELETE /users/ban did before)."""
    from app.services.video_deletion import delete_spaces_file_if_unused
    from app.services.video_deletion import soft_delete_video
    from app.services.scheduled_publisher import SCHEDULED, PUBLISHING
    user = await db.users.find_one({"_id": oid})
    if not user:
        return
    now = _now()
    uid = str(oid)
    await db.users.update_one({"_id": oid}, {"$set": {
        "is_active": False, "username": f"deleted_user-{uid}", "email": f"deleted_user-{uid}@wanoafrica.com",
        "display_name": "Deleted User", "profile_picture": None, "cover_picture": None, "bio": None,
        "updated_at": now}})
    for url in (user.get("profile_picture"), user.get("cover_picture")):
        await delete_spaces_file_if_unused(db, url, exclude_user_id=oid)
    videos = [v async for v in db.videos.find({"creator_id": oid, "$or": [
        {"is_active": {"$ne": False}}, {"status": {"$in": [SCHEDULED, PUBLISHING]}},
        {"moderation.frozen": True}, {"moderation.mod_hidden": True}]})]
    for v in videos:
        if v.get("is_active") is False and (v.get("moderation") or {}).get("frozen") is not True \
                and v.get("status") not in (SCHEDULED, PUBLISHING):
            continue
        await soft_delete_video(db, v)


# ------------------------------------------------------------------ the person's side
async def my_state(db, user_id) -> dict:
    u = await db.users.find_one({"_id": ObjectId(str(user_id))}, {"mod_state": 1})
    ms = (u or {}).get("mod_state") or {}
    if ms.get("state") == "limited" and isinstance(ms.get("until"), datetime) and ms["until"] <= _now():
        return {"state": "ok"}
    if ms.get("state") in ("limited", "frozen"):
        return {"state": ms["state"], "until": ms.get("until"), "reason": REASONS.get(ms.get("reason"), "")}
    return {"state": "ok"}
