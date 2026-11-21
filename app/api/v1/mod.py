"""Moderation for staff, and the person's own side of it (safe_mod.py,
mod_warnings.py, staff.py). Everything here is behind the "safe_ban" switch
(404 while it's off); warnings also need the "warnings" switch.

Staff endpoints take an Admin page sign-in or an app staff session (a 6-digit
code from an authenticator app), never just an app token: see staff.need().
"""

import re
from datetime import datetime, timedelta
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.api.deps import _ensure_token_not_revoked, get_current_active_user, get_current_user, oauth2_scheme
from app.core.database import get_database
from app.services import mod_warnings, safe_mod, staff as staff_svc

router = APIRouter()
Reason = Literal[tuple(safe_mod.REASONS)]  # type: ignore[valid-type]


async def _db():
    db = get_database()
    await safe_mod.require_on(db)
    return db


async def _wdb():
    db = await _db()
    await mod_warnings.require_on(db)
    return db


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Optional[str] = Field(None, max_length=80)      # one-time id: a resend does nothing
    note: str = Field("", max_length=500)


class FreezeIn(_In):
    user_id: str = Field(..., max_length=40)
    reason: Reason
    evidence: StrictBool = False


class UserIn(_In):
    user_id: str = Field(..., max_length=40)
    reason: Reason = "other"


class LimitIn(_In):
    user_id: str = Field(..., max_length=40)
    reason: Reason
    hours: Literal[24, 72, 168]


class PostIn(_In):
    post_id: str = Field(..., max_length=40)
    reason: Reason


class CommentIn(_In):
    comment_id: str = Field(..., max_length=40)
    reason: Reason


class WarnIn(_In):
    user_id: str = Field(..., max_length=40)
    reason: Reason
    post_id: Optional[str] = Field(None, max_length=40)
    comment_id: Optional[str] = Field(None, max_length=40)


class SafeguardIn(_In):
    user_id: str = Field(..., max_length=40)
    post_id: Optional[str] = Field(None, max_length=40)
    comment_id: Optional[str] = Field(None, max_length=40)


# ------------------------------------------------------------------ staff session
class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=6, max_length=6)
    device: str = Field(..., min_length=8, max_length=100)


@router.post("/session")
async def start_session(body: SessionIn, me: str = Depends(get_current_active_user)):
    db = await _db()
    user = await db.users.find_one({"_id": ObjectId(me)})
    if await staff_svc.effective_role(db, user) is None:
        raise HTTPException(403, "Staff only")
    return await staff_svc.start_session(db, user, body.code, body.device)


@router.get("/me")
async def staff_me(me: str = Depends(get_current_active_user)):
    """The app asks this to know whether to show staff tools (no session needed)."""
    db = get_database()
    if not await safe_mod.on(db):
        return {"staff": False}
    user = await db.users.find_one({"_id": ObjectId(me)}, {"user_type": 1, "staff_role": 1, "is_active": 1,
                                                          "staff_totp.secret": 1})
    role = await staff_svc.effective_role(db, user) if user else None
    from app.services import creator_push
    return {"staff": role is not None, "role": role,
            "has_code": bool(((user or {}).get("staff_totp") or {}).get("secret")),
            "tools": role is not None and await creator_push.on(db, "mod_tools"),
            "warnings": await mod_warnings.on(db)}


# ------------------------------------------------------------------ actions
@router.post("/actions/freeze")
async def freeze(body: FreezeIn, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.freeze(await _db(), s, body.user_id, body.reason, body.note, body.key, body.evidence)


@router.post("/actions/unfreeze")
async def unfreeze(body: UserIn, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.unfreeze(await _db(), s, body.user_id, body.reason, body.note, body.key)


@router.post("/actions/limit")
async def limit(body: LimitIn, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.limit(await _db(), s, body.user_id, body.reason, body.hours, body.note, body.key)


@router.post("/actions/hide-post")
async def hide_post(body: PostIn, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.hide_post(await _db(), s, body.post_id, body.reason, body.note, body.key)


@router.post("/actions/hide-comment")
async def hide_comment(body: CommentIn, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.hide_comment(await _db(), s, body.comment_id, body.reason, body.note, body.key)


@router.post("/actions/warn")
async def warn(body: WarnIn, s=Depends(staff_svc.need("moderator"))):
    return await mod_warnings.warn(await _wdb(), s, body.user_id, body.reason, body.post_id, body.comment_id,
                                   body.note, body.key)


@router.post("/actions/safeguard")
async def safeguard(body: SafeguardIn, s=Depends(staff_svc.need("moderator"))):
    return await mod_warnings.safeguard(await _db(), s, body.user_id, body.post_id, body.comment_id,
                                        body.note, body.key)


@router.post("/actions/{action_id}/undo")
async def undo(action_id: str, s=Depends(staff_svc.need("moderator"))):
    return await safe_mod.undo(await _db(), s, action_id[:40])


@router.get("/suggest")
async def suggest(user_id: str = Query(..., max_length=40), reason: Reason = Query(...),
                  s=Depends(staff_svc.need("moderator"))):
    db = await _db()
    return await mod_warnings.suggest(db, safe_mod._oid(user_id, "user id"), reason)


@router.get("/reasons")
async def reasons():
    return {"reasons": [{"key": k, "label": v} for k, v in safe_mod.REASONS.items()],
            "templates": mod_warnings.TEMPLATES, "limit_hours": list(safe_mod.LIMIT_HOURS)}


# ------------------------------------------------------------------ account card
def _mask_email(e: Optional[str]) -> Optional[str]:
    if not isinstance(e, str) or "@" not in e:
        return None
    name, dom = e.split("@", 1)
    return f"{name[:1]}•••@{dom}"


def _mask_phone(p: Optional[str]) -> Optional[str]:
    if not isinstance(p, str) or len(p) < 4:
        return None
    return "•••" + p[-3:]


@router.get("/users/{user_id}/card")
async def account_card(user_id: str, s=Depends(staff_svc.need("moderator"))):
    db = await _db()
    oid = safe_mod._oid(user_id[:40], "user id")
    u = await db.users.find_one({"_id": oid})
    if not u:
        raise HTTPException(404, "User not found")
    await db.staff_views.insert_one({"by": s.oid, "user": oid, "what": "card", "at": datetime.utcnow()})
    made = u.get("created_at")
    if isinstance(made, datetime) and made.tzinfo is not None:
        made = made.replace(tzinfo=None)
    from app.services import home_ground
    age = home_ground.age_of(u.get("date_of_birth"))
    hist = await mod_warnings.history(db, oid)
    reports = await db.reports.count_documents({"video_id": {"$in": [v["_id"] async for v in db.videos.find(
        {"creator_id": oid}, {"_id": 1}).limit(500)]}, "is_active": True})
    return {
        "id": str(oid), "username": u.get("username"), "display_name": u.get("display_name"),
        "picture": u.get("profile_picture"),
        "account_days": (datetime.utcnow() - made).days if isinstance(made, datetime) else None,
        "teen": age is not None and age < 18, "followers": u.get("followers_count", 0),
        "posts": u.get("videos_count", 0), "app_version": u.get("app_version") or u.get("last_app_version"),
        "email": _mask_email(u.get("email")), "phone": _mask_phone(u.get("phone_number")),
        "state": (u.get("mod_state") or {}).get("state", "ok"),
        "evidence": bool((u.get("mod_state") or {}).get("evidence")),
        "reports_on_posts": reports,
        "held_comments": await db.comments.count_documents({"user_id": oid, "held": {"$exists": True}}),
        "failed_uploads": await db.upload_attempts.count_documents({"user": oid, "posted_at": {"$exists": False}}),
        "staff": await staff_svc.effective_role(db, u),
        **hist,
    }


# ------------------------------------------------------------------ reports inbox
@router.get("/inbox")
async def inbox(s=Depends(staff_svc.need("moderator"))):
    """Every report queue in one list, urgent first, then newest. Each item says
    how old the reporting accounts are (a pile-on by new accounts shows)."""
    db = await _db()
    from app.services import comment_guard, messages
    items = []
    for r in await comment_guard.open_reports(db, 50):
        items.append({"type": "comment", "id": r["comment_id"], "post_id": r["video_id"], "user_id": r["owner_id"],
                      "text": r["text"], "reasons": r["reason"], "reports": r["reports"], "urgent": r.get("urgent", False),
                      "hidden": r["hidden"], "at": r["created_at"],
                      "reporter_days": await _reporter_days(db, "comment_reports", {"comment_id": ObjectId(r["comment_id"])})})
    async for v in db.videos.find({"is_active": True, "$or": [{"moderation.state": {"$in": ["held", "flagged"]}},
                                                              {"report_count": {"$gt": 0}}],
                                   "moderation.mod_hidden": {"$ne": True}},
                                  {"creator_id": 1, "description": 1, "thumbnail": 1, "moderation": 1, "report_count": 1,
                                   "updated_at": 1}).sort("updated_at", -1).limit(50):
        pend = await db.reports.find({"video_id": v["_id"], "status": "pending", "is_active": True},
                                     {"category": 1, "reason": 1, "created_at": 1}).to_list(50)
        if not pend:
            continue
        cats = sorted({str(p.get("category") or p.get("reason") or "other") for p in pend})
        items.append({"type": "post", "id": str(v["_id"]), "post_id": str(v["_id"]), "user_id": str(v.get("creator_id")),
                      "text": (v.get("description") or "")[:300], "picture": v.get("thumbnail"),
                      "reasons": ", ".join(cats), "reports": len(pend),
                      "urgent": "child_exploitation" in cats, "held": (v.get("moderation") or {}).get("state") == "held",
                      "at": max((p.get("created_at") for p in pend if p.get("created_at")), default=None),
                      "reporter_days": await _reporter_days(db, "reports", {"video_id": v["_id"], "status": "pending"})})
    for m in await messages.admin_reports(db, 30):
        items.append({"type": "message", "id": m["id"], "user_id": (m.get("sender") or {}).get("id"),
                      "text": (m.get("message") or {}).get("text", ""), "reasons": m.get("reason"), "reports": 1,
                      "urgent": m.get("reason") in ("child_safety", "child_exploitation"), "at": m.get("created_at"),
                      "context": m.get("context")})
    items.sort(key=lambda x: (not x.get("urgent"), -(x["at"].timestamp() if isinstance(x.get("at"), datetime) else 0)))
    return {"items": items[:100]}


async def _reporter_days(db, coll: str, q: dict) -> list:
    ids = [r.get("reporter_id") async for r in db[coll].find(q, {"reporter_id": 1}).limit(20)]
    out = []
    async for u in db.users.find({"_id": {"$in": [i for i in ids if isinstance(i, ObjectId)]}}, {"created_at": 1}):
        made = u.get("created_at")
        if isinstance(made, datetime):
            out.append((datetime.utcnow() - made.replace(tzinfo=None)).days)
    return sorted(out)


# ------------------------------------------------------------------ appeals
@router.get("/appeals")
async def appeals(s=Depends(staff_svc.need("moderator"))):
    return {"items": await mod_warnings.open_appeals(await _db())}


class AppealDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["upheld", "reversed"]
    note: str = Field("", max_length=500)


@router.post("/appeals/{appeal_id}")
async def decide_appeal(appeal_id: str, body: AppealDecisionIn, s=Depends(staff_svc.need("moderator"))):
    return await mod_warnings.decide_appeal(await _db(), s, appeal_id[:40], body.outcome, body.note)


# ------------------------------------------------------------------ owners and admins
class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


@router.post("/users/{user_id}/evidence")
async def evidence(user_id: str, body: EvidenceIn, s=Depends(staff_svc.need("admin"))):
    return await safe_mod.set_evidence(await _db(), s, user_id[:40], body.on)


class WipeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(..., max_length=80)


@router.post("/users/{user_id}/wipe")
async def wipe(user_id: str, body: WipeIn, s=Depends(staff_svc.need("owner"))):
    if s.via != "web":
        raise HTTPException(403, "Wiping is only on the Admin page")
    return await safe_mod.wipe(await _db(), s, user_id[:40], body.confirm)


class StaffRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(..., max_length=40)
    role: Optional[Literal["moderator", "admin", "owner"]] = None      # None: no longer staff


@router.post("/staff")
async def set_staff(body: StaffRoleIn, s=Depends(staff_svc.need("owner"))):
    """Add, change or remove a staff member. Owners only. Staff must be adults;
    admin and owner need an admin account (made on the server, never from here)."""
    db = await _db()
    oid = safe_mod._oid(body.user_id, "user id")
    u = await db.users.find_one({"_id": oid})
    if not u:
        raise HTTPException(404, "User not found")
    if oid == s.oid and body.role != "owner":
        raise HTTPException(400, "You can't remove yourself as owner")
    if body.role and not staff_svc.is_adult(u):
        raise HTTPException(400, "Staff must be 18 or over (with a birthday on the account)")
    if body.role in ("admin", "owner") and str(u.get("user_type", "")).lower() != "admin":
        raise HTTPException(400, "Admin and owner need an admin account")
    if body.role is None:
        await db.users.update_one({"_id": oid}, {"$unset": {"staff_role": "", "staff_totp": ""}})
        await staff_svc.end_all(db, oid)
    else:
        await db.users.update_one({"_id": oid}, {"$set": {"staff_role": body.role}})
    staff_svc.forget()
    from app.services import admin_log
    await admin_log.record(db, s.id, "staff_role", {"user": str(oid), "role": body.role})
    return {"user_id": str(oid), "role": body.role}


@router.get("/staff")
async def list_staff(s=Depends(staff_svc.need("admin"))):
    db = await _db()
    out = []
    async for u in db.users.find({"$or": [{"user_type": "admin"}, {"staff_role": {"$in": list(staff_svc.ROLES)}}],
                                  "is_active": True},
                                 {"username": 1, "display_name": 1, "staff_role": 1, "user_type": 1, "staff_totp.secret": 1}):
        out.append({"id": str(u["_id"]), "username": u.get("username"), "display_name": u.get("display_name"),
                    "role": await staff_svc.effective_role(db, u),
                    "has_code": bool((u.get("staff_totp") or {}).get("secret"))})
    return {"items": out}


@router.post("/staff/{user_id}/code")
async def new_code(user_id: str, s=Depends(staff_svc.need("owner"))):
    """A new authenticator secret for a staff member. Shown once, on the Admin
    page only; the person types it into an authenticator app."""
    if s.via != "web":
        raise HTTPException(403, "Only on the Admin page")
    db = await _db()
    oid = safe_mod._oid(user_id[:40], "user id")
    u = await db.users.find_one({"_id": oid})
    if not u or await staff_svc.effective_role(db, u) is None:
        raise HTTPException(404, "Not a staff member")
    secret = staff_svc.new_secret()
    await db.users.update_one({"_id": oid}, {"$set": {"staff_totp": {"secret": secret, "made_at": datetime.utcnow()}}})
    await staff_svc.end_all(db, oid)
    from app.services import admin_log
    await admin_log.record(db, s.id, "staff_code", {"user": str(oid)})
    return {"secret": secret, "uri": staff_svc.otpauth_uri(secret, u.get("username") or str(oid))}


class OfficialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(..., max_length=40)
    on: StrictBool


@router.post("/official")
async def set_official(body: OfficialIn, s=Depends(staff_svc.need("owner"))):
    """The Wano Team badge. Owners only, on the Admin page; only the server sets it."""
    if s.via != "web":
        raise HTTPException(403, "Only on the Admin page")
    db = await _db()
    oid = safe_mod._oid(body.user_id, "user id")
    r = await db.users.update_one({"_id": oid}, {"$set": {"official": True}} if body.on else {"$unset": {"official": ""}})
    if not r.matched_count:
        raise HTTPException(404, "User not found")
    from app.services import admin_log
    await admin_log.record(db, s.id, "official_badge", {"user": str(oid), "on": body.on})
    return {"user_id": str(oid), "official": body.on}


@router.post("/sessions/end-all")
async def end_all(s=Depends(staff_svc.need("owner"))):
    db = await _db()
    n = await staff_svc.end_all(db)
    from app.services import admin_log
    await admin_log.record(db, s.id, "staff_sessions_end_all", {"ended": n})
    return {"ended": n}


@router.get("/actions")
async def recent_actions(days: int = Query(7, ge=1, le=90), s=Depends(staff_svc.need("admin"))):
    """For the weekly review: every staff action, undo and reversal."""
    db = await _db()
    since = datetime.utcnow() - timedelta(days=days)
    acts = [safe_mod.public_action(a) async for a in db.mod_actions.find({"at": {"$gte": since}}).sort("at", -1).limit(500)]
    views = await db.staff_views.count_documents({"at": {"$gte": since}})
    return {"items": acts, "card_views": views}


# ------------------------------------------------------------------ the person's side
async def me_any(current_user: str = Depends(get_current_user), token: str = Depends(oauth2_scheme)) -> str:
    """Signed in, including a suspended account (so it can read why and appeal)."""
    db = get_database()
    u = await db.users.find_one({"_id": ObjectId(current_user)})
    if not u or (u.get("is_active") is not True and (u.get("mod_state") or {}).get("state") != "frozen"):
        raise HTTPException(403, "Inactive user")
    _ensure_token_not_revoked(u, token)
    return current_user


@router.get("/me/notices")
async def my_notices(me: str = Depends(me_any)):
    db = get_database()
    if not await safe_mod.on(db):
        return {"state": {"state": "ok"}, "items": []}
    return await mod_warnings.notices(db, me)


@router.post("/me/notices/{notice_id}/seen")
async def notice_seen(notice_id: str, me: str = Depends(me_any)):
    return await mod_warnings.seen(await _db(), me, notice_id[:40])


class AppealIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(..., max_length=40)
    text: str = Field(..., max_length=1000)


@router.post("/me/appeals")
async def my_appeal(body: AppealIn, me: str = Depends(me_any)):
    return await mod_warnings.appeal(await _db(), me, body.decision_id, body.text)


@router.get("/me/record")
async def my_record(me: str = Depends(me_any)):
    return await mod_warnings.export(await _db(), me)


@router.get("/me/decisions")
async def my_decisions(me: str = Depends(me_any)):
    """A suspended person sees the decision so they can appeal it."""
    db = await _db()
    a = await db.mod_actions.find_one({"target_user": ObjectId(me), "kind": {"$in": ["freeze", "limit"]},
                                       "state": "done"}, sort=[("at", -1)])
    if not a:
        return {"decision": None}
    out = safe_mod.public_action(a)
    out.pop("by", None)
    out["can_appeal"] = await db.appeals.find_one({"_id": a["_id"]}, {"_id": 1}) is None
    return {"decision": out}
