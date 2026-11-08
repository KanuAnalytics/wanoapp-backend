"""Studio: posts made for Wano's own house accounts (Diasporastories, Cultura
Latina, Africa News Now, ...), reviewed before they go out.

An admin marks accounts as house accounts. A finished video is uploaded
straight to Cloudflare Stream from the admin page (direct upload URL), then
saved as a draft for one house account. Nothing is posted until an admin
approves it; approving creates a normal post on that account, now or at the
chosen time (the scheduled-post machinery holds it until then). Rejected
drafts stay in the list with the reason. Drafts made with AI (voice or
visuals) carry ai_generated, which the post keeps."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

STATES = ("pending", "approved", "rejected")


async def house_accounts(db) -> list:
    rows = await db.users.find({"house_account": True}, {"username": 1, "display_name": 1, "profile_picture": 1,
                                                         "is_active": 1}).sort("username", 1).to_list(100)
    return [{"id": str(u["_id"]), "username": u.get("username"), "display_name": u.get("display_name"),
             "profile_picture": u.get("profile_picture"), "active": u.get("is_active") is True} for u in rows]


async def set_house(db, creator_text: str, on: bool) -> dict:
    from app.api.v1.admin import _find_creator
    u = await _find_creator(db, creator_text)
    if not u:
        raise HTTPException(status_code=404, detail="No account found")
    if on and str(u.get("user_type", "")).lower() == "admin":
        raise HTTPException(status_code=400, detail="Admin accounts can't be house accounts")
    await db.users.update_one({"_id": u["_id"]}, {"$set": {"house_account": True}} if on
                              else {"$unset": {"house_account": ""}})
    return {"id": str(u["_id"]), "username": u.get("username"), "house_account": on}


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n] if not isinstance(s, (dict, list)) else ""


async def create_draft(db, admin: str, account_id: str, stream_uid: str, title: str, caption: str,
                       ai: bool, scheduled_for: Optional[datetime]) -> dict:
    import re
    if not ObjectId.is_valid(account_id):
        raise HTTPException(status_code=404, detail="Pick a house account")
    acct = await db.users.find_one({"_id": ObjectId(account_id), "house_account": True, "is_active": True}, {"_id": 1})
    if not acct:
        raise HTTPException(status_code=404, detail="That isn't an active house account")
    if not isinstance(stream_uid, str) or not re.fullmatch(r"[a-f0-9]{32}", stream_uid):
        raise HTTPException(status_code=422, detail="Upload the video first")
    if not _clean(title, 100) and not _clean(caption, 2200):
        raise HTTPException(status_code=422, detail="Add a title or caption")
    doc = {"account_id": acct["_id"], "stream_uid": stream_uid,
           "file_url": f"https://videodelivery.net/{stream_uid}/manifest/video.m3u8",
           "title": _clean(title, 100), "caption": str(caption or "")[:2200] if isinstance(caption, str) else "",
           "ai": bool(ai), "scheduled_for": _naive(scheduled_for), "state": "pending",
           "created_at": datetime.utcnow(), "created_by": ObjectId(admin)}
    r = await db.studio_drafts.insert_one(doc)
    return {"id": str(r.inserted_id)}


def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt if isinstance(dt, datetime) else None


async def drafts(db, state: Optional[str]) -> list:
    q = {"state": state} if state in STATES else {}
    rows = await db.studio_drafts.find(q).sort("created_at", -1).limit(200).to_list(200)
    names = {u["_id"]: u.get("username") async for u in db.users.find(
        {"_id": {"$in": list({r["account_id"] for r in rows})}}, {"username": 1})}
    return [{"id": str(r["_id"]), "account": {"id": str(r["account_id"]), "username": names.get(r["account_id"])},
             "stream_uid": r["stream_uid"], "title": r.get("title"), "caption": r.get("caption"), "ai": r.get("ai"),
             "scheduled_for": r.get("scheduled_for"), "state": r["state"], "created_at": r.get("created_at"),
             "post_id": str(r["post_id"]) if r.get("post_id") else None, "note": r.get("note")} for r in rows]


async def approve(db, admin: str, draft_id: str, when: Optional[datetime]) -> dict:
    from app.services.video_posts import create_video_post
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(status_code=404, detail="Not found")
    # Claim it first so two clicks (or two admins) can't post it twice.
    d = await db.studio_drafts.find_one_and_update({"_id": ObjectId(draft_id), "state": "pending"},
                                                   {"$set": {"state": "approving", "approved_by": ObjectId(admin)}})
    if not d:
        raise HTTPException(status_code=409, detail="Already handled")
    try:
        acct = await db.users.find_one({"_id": d["account_id"], "house_account": True, "is_active": True}, {"_id": 1})
        if not acct:
            raise HTTPException(status_code=400, detail="That account is no longer an active house account")
        now = datetime.utcnow()
        at = _naive(when) or d.get("scheduled_for")
        scheduled = at if isinstance(at, datetime) and at > now + timedelta(minutes=2) else None
        vid = await create_video_post(str(d["account_id"]), remote_url=d["file_url"], remote_url_cf=d["file_url"],
                                      title=d.get("title") or None, description=d.get("caption") or "",
                                      is_ready_to_stream=False, scheduled_at=scheduled,
                                      scheduled_timezone="UTC" if scheduled else None)
        sets = {"studio_draft": d["_id"]}
        if d.get("ai"):
            sets["ai_generated"] = True
        await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": sets})
        await db.studio_drafts.update_one({"_id": d["_id"]}, {"$set": {"state": "approved", "post_id": ObjectId(vid),
                                                                       "approved_at": now, "posted_for": scheduled}})
    except Exception:
        await db.studio_drafts.update_one({"_id": d["_id"], "state": "approving"}, {"$set": {"state": "pending"}})
        raise
    await _mark_ready_if_processed(d["stream_uid"])
    return {"post_id": str(vid), "scheduled_for": scheduled}


async def _mark_ready_if_processed(uid: str) -> None:
    """The upload usually finished processing long before approval, so its
    "ready" webhook found no post. Ask Cloudflare and mark it ready now; if
    that fails the stuck-upload rescue does it within minutes."""
    import asyncio
    from app.services.upload_DO import get_stream_video_status
    try:
        st = await asyncio.to_thread(get_stream_video_status, uid)
        if isinstance(st, dict) and st.get("readyToStream"):
            from app.routes.upload_video import mark_stream_ready
            await mark_stream_ready(uid)
    except Exception:
        pass


async def reject(db, admin: str, draft_id: str, note: str) -> None:
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(status_code=404, detail="Not found")
    r = await db.studio_drafts.update_one({"_id": ObjectId(draft_id), "state": "pending"},
                                          {"$set": {"state": "rejected", "note": _clean(note, 300),
                                                    "rejected_by": ObjectId(admin), "rejected_at": datetime.utcnow()}})
    if not r.matched_count:
        raise HTTPException(status_code=409, detail="Already handled")
