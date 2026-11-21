"""Warnings ladder ("warnings" switch, needs safe_ban): warn → limit → suspend → ban.

- Only staff create warnings (never reports on their own, so a group can't
  get someone warned by reporting together).
- Each warning names the rule, uses a fixed, kind template (a gentler one for
  teens; ready for translation) and keeps a copy of what it was about, so the
  person can see it even after the post is gone.
- The person learns about it after the undo window plus a small random delay
  (so the timing doesn't point at whoever reported), as an "I understand"
  notice in the app (2.3.60) and, with the wano_team switch, in the official
  Wano Team thread for older apps. The server records when it was seen.
- suggest(): the next step. Steps count within WINDOW_DAYS; a lifetime count
  of LIFETIME_REVIEW sends any new case to a suspension review. Serious harm
  (safe_mod.SERIOUS) goes straight to suspension. Withdrawn warnings never count.
- Grooming or exploitation (child_safety): no warning at all. A warning would
  tip them off; that's a silent suspension (safe_mod.freeze) with evidence kept.
- A teen who is the victim (for example a self-image) is a safeguarding case,
  not a strike: staff pick "safeguard", which hides the content and records
  it without any warning or count.
- Appeals: one per decision, from the person only, through a form; rude text
  is kept but marked. The reporter is never named anywhere.
- A warning affects nothing else; limits and suspensions pause points.
"""

import random
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.services import safe_mod

SWITCH = "warnings"
WINDOW_DAYS = 90
LIFETIME_REVIEW = 5
NOTICE_JITTER_MINUTES = 20
APPEAL_MAX = 500

TEMPLATES = {
    "spam": "Posting the same thing again and again, or adverts for other apps and services, isn't allowed on Wano.",
    "scam": "Asking people for money, codes or personal details, or promising prizes that aren't real, isn't allowed on Wano.",
    "harassment": "Insulting, threatening or ganging up on someone isn't allowed on Wano.",
    "hate": "Attacking people for who they are (where they're from, their tribe, religion, gender and the like) isn't allowed on Wano.",
    "nudity": "Nudity and sexual content aren't allowed on Wano.",
    "violence": "Threats and content that shows or encourages violence aren't allowed on Wano.",
    "self_harm": "Content that encourages suicide or self-harm isn't allowed on Wano. If you're struggling, please talk to someone you trust.",
    "impersonation": "Pretending to be someone else, including Wano, isn't allowed.",
    "false_information": "Sharing false information that can hurt people isn't allowed on Wano.",
    "copyright": "Posting other people's videos or music as your own isn't allowed on Wano.",
    "other": "This goes against Wano's community rules.",
}
TEEN_TEMPLATES = {
    k: v.replace(" isn't allowed on Wano.", " isn't okay on Wano.").replace(" aren't allowed on Wano.", " aren't okay on Wano.")
    for k, v in TEMPLATES.items()
}
TITLE = "A note about your account"
TEEN_TITLE = "A quick note from Wano"
SAY = "Wano will never ask for your password or a code."


def _now() -> datetime:
    return datetime.utcnow()


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH) and await safe_mod.on(db)


async def require_on(db):
    if not await on(db):
        raise HTTPException(404, "Not found")


async def _counts(db, uid: ObjectId) -> dict:
    since = _now() - timedelta(days=WINDOW_DAYS)
    live = {"user": uid, "withdrawn": {"$ne": True}}
    recent = await db.warnings.count_documents({**live, "at": {"$gte": since}})
    lifetime = await db.warnings.count_documents(live)
    limits = await db.mod_actions.count_documents({"target_user": uid, "kind": "limit", "state": "done",
                                                   "at": {"$gte": since}})
    return {"recent": recent, "lifetime": lifetime, "recent_limits": limits}


async def suggest(db, user_id, reason: str) -> dict:
    uid = ObjectId(str(user_id))
    c = await _counts(db, uid)
    if reason in safe_mod.SERIOUS:
        step = "suspend"
    elif c["lifetime"] >= LIFETIME_REVIEW - 1:
        step = "suspend"
    elif c["recent"] + c["recent_limits"] >= 2:
        step = "suspend"
    elif c["recent"] + c["recent_limits"] == 1:
        step = "limit"
    else:
        step = "warn"
    if reason == "child_safety":
        step = "suspend_silent"
    return {"step": step, **c}


def _is_teen(user: dict) -> bool:
    from app.services import home_ground
    return home_ground.is_minor(user)


async def _copy(db, post_id=None, comment_id=None) -> Optional[dict]:
    if comment_id:
        c = await db.comments.find_one({"_id": comment_id}, {"content": 1, "video_id": 1})
        if c:
            return {"kind": "comment", "text": (c.get("content") or "")[:500]}
    if post_id:
        v = await db.videos.find_one({"_id": post_id}, {"description": 1, "title": 1, "thumbnail_url": 1,
                                                         "thumbnail": 1})
        if v:
            return {"kind": "post", "text": (v.get("description") or v.get("title") or "")[:300],
                    "picture": v.get("thumbnail_url") or v.get("thumbnail")}
    return None


async def warn(db, staff, user_id, reason: str, post_id=None, comment_id=None, note: str = "",
               key: Optional[str] = None) -> dict:
    if (prev := await safe_mod.replay(db, staff, key)):
        return prev
    if reason == "child_safety":
        raise HTTPException(400, "Child safety cases are never warned: suspend (silently) instead")
    if reason not in TEMPLATES:
        raise HTTPException(400, "Pick a rule")
    user = await safe_mod._target_user(db, staff, user_id)
    pid = safe_mod._oid(post_id, "post id") if post_id else None
    cid = safe_mod._oid(comment_id, "comment id") if comment_id else None
    try:
        a = await safe_mod._record(db, staff=staff, kind="warn", reason=reason, key=key, target_user=user["_id"],
                                   target_post=pid, target_comment=cid, note=note, extra={"rule": reason})
    except safe_mod._Already as e:
        return safe_mod.public_action(e.prev)
    teen = _is_teen(user)
    doc = {"user": user["_id"], "action": a["_id"], "rule": reason, "teen": teen,
           "title": TEEN_TITLE if teen else TITLE,
           "text": (TEEN_TEMPLATES if teen else TEMPLATES)[reason],
           "copy": await _copy(db, pid, cid), "at": _now(),
           "visible_after": a["undo_until"] + timedelta(minutes=random.randint(0, NOTICE_JITTER_MINUTES))}
    await db.warnings.insert_one(doc)
    return await safe_mod._done(db, a)


async def safeguard(db, staff, user_id, post_id=None, comment_id=None, note: str = "",
                    key: Optional[str] = None) -> dict:
    """Protect a teen who is the victim: hide the content, record it, no strike."""
    user = await safe_mod._target_user(db, staff, user_id)
    if post_id:
        out = await safe_mod.hide_post(db, staff, post_id, "child_safety", note=f"safeguard {note}".strip(), key=key)
    elif comment_id:
        out = await safe_mod.hide_comment(db, staff, comment_id, "child_safety", note=f"safeguard {note}".strip(), key=key)
    else:
        raise HTTPException(400, "Pick the post or comment")
    await db.mod_actions.update_one({"_id": ObjectId(out["id"])}, {"$set": {"safeguard": True}})
    await db.safeguarding.insert_one({"user": user["_id"], "action": ObjectId(out["id"]), "at": _now(),
                                      "by": staff.oid})
    return out


# ------------------------------------------------------------------ the person's side
async def notices(db, user_id) -> dict:
    uid = ObjectId(str(user_id))
    now = _now()
    items = []
    async for w in db.warnings.find({"user": uid, "withdrawn": {"$ne": True}, "seen_at": {"$exists": False},
                                     "visible_after": {"$lte": now}}).sort("at", 1).limit(5):
        a = await db.mod_actions.find_one({"_id": w["action"]}, {"state": 1})
        if (a or {}).get("state") == "undone":
            continue
        items.append({"id": str(w["_id"]), "kind": "warning", "title": w.get("title"), "text": w.get("text"),
                      "rule": w.get("rule"), "rule_label": safe_mod.REASONS.get(w.get("rule"), ""),
                      "copy": w.get("copy"), "at": w.get("at"), "action": str(w["action"]),
                      "can_appeal": await db.appeals.find_one({"action": w["action"]}, {"_id": 1}) is None,
                      "say": SAY})
    return {"state": await safe_mod.my_state(db, uid), "items": items}


async def seen(db, user_id, warning_id) -> dict:
    uid = ObjectId(str(user_id))
    oid = safe_mod._oid(warning_id, "notice id")
    r = await db.warnings.update_one({"_id": oid, "user": uid, "seen_at": {"$exists": False}},
                                     {"$set": {"seen_at": _now()}})
    return {"seen": True, "changed": bool(r.modified_count)}


async def appeal(db, user_id, action_id, text: str) -> dict:
    uid = ObjectId(str(user_id))
    aid = safe_mod._oid(action_id, "decision id")
    a = await db.mod_actions.find_one({"_id": aid, "target_user": uid, "state": "done"})
    if not a or a.get("kind") not in ("warn", "limit", "freeze", "hide_post", "hide_comment"):
        raise HTTPException(404, "Decision not found")
    text = " ".join((text or "").split())[:APPEAL_MAX]
    if len(text) < 3:
        raise HTTPException(400, "Tell us in a few words why you think it's wrong")
    from app.services import comment_guard
    rude = comment_guard.has_blocked_word(text, comment_guard.BLOCKED_WORDS)
    try:
        await db.appeals.insert_one({"_id": aid, "action": aid, "user": uid, "text": text, "rude": rude,
                                     "status": "open", "at": _now()})
    except DuplicateKeyError:
        raise HTTPException(409, "You've already appealed this decision")
    return {"appealed": True}


async def open_appeals(db, limit: int = 100) -> list:
    out = []
    async for ap in db.appeals.find({"status": "open"}).sort("at", 1).limit(limit):
        a = await db.mod_actions.find_one({"_id": ap["action"]})
        out.append({"id": str(ap["_id"]), "text": ap.get("text"), "rude": ap.get("rude", False), "at": ap.get("at"),
                    "decision": safe_mod.public_action(a)})
    return out


async def decide_appeal(db, staff, appeal_id, outcome: str, note: str = "") -> dict:
    oid = safe_mod._oid(appeal_id, "appeal id")
    ap = await db.appeals.find_one({"_id": oid, "status": "open"})
    if not ap:
        raise HTTPException(404, "Appeal not found")
    a = await db.mod_actions.find_one({"_id": ap["action"]})
    if a and a.get("by") == staff.oid and staff.role == "moderator":
        raise HTTPException(403, "Someone else must review an appeal against your own decision")
    if outcome == "reversed" and a:
        kind = a.get("kind")
        if kind == "warn":
            await db.warnings.update_one({"action": a["_id"]}, {"$set": {"withdrawn": True, "withdrawn_at": _now()}})
        elif kind == "freeze":
            await safe_mod.apply_unfreeze(db, a["target_user"])
        elif kind == "limit":
            await db.users.update_one({"_id": a["target_user"], "mod_state.action": a["_id"]}, {"$unset": {"mod_state": ""}})
        elif kind == "hide_post":
            await safe_mod.unhide_post(db, a["target_post"])
        elif kind == "hide_comment":
            c = await db.comments.find_one({"_id": a["target_comment"]})
            if c:
                await safe_mod._unhide_comment_doc(db, c)
        await db.mod_actions.update_one({"_id": a["_id"]}, {"$set": {"state": "reversed", "reversed_at": _now()}})
    elif outcome != "upheld":
        raise HTTPException(400, "upheld or reversed")
    await db.appeals.update_one({"_id": oid}, {"$set": {"status": outcome, "decided_by": staff.oid,
                                                         "decided_at": _now(), "note": (note or "")[:500]}})
    from app.services import admin_log
    await admin_log.record(db, staff.id, "mod_appeal", {"appeal": str(oid), "outcome": outcome})
    return {"status": outcome}


async def history(db, user_id) -> dict:
    """For the account card: counts and the last decisions (newest first)."""
    uid = ObjectId(str(user_id))
    c = await _counts(db, uid)
    acts = [safe_mod.public_action(a) async for a in
            db.mod_actions.find({"target_user": uid}).sort("at", -1).limit(20)]
    return {**c, "actions": acts}


async def export(db, user_id) -> dict:
    """The person's own moderation record (data rights)."""
    uid = ObjectId(str(user_id))
    acts = [safe_mod.public_action(a) async for a in db.mod_actions.find({"target_user": uid}).sort("at", -1)]
    for a in acts:
        a.pop("by", None)                # staff names are never shown to users
    warns = [{"rule": w.get("rule"), "at": w.get("at"), "withdrawn": bool(w.get("withdrawn")),
              "seen_at": w.get("seen_at")} async for w in db.warnings.find({"user": uid}).sort("at", -1)]
    return {"decisions": acts, "warnings": warns}
