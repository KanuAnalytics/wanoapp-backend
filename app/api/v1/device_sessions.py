from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.api.deps import get_current_active_user, oauth2_scheme
from app.core.database import get_database
from app.services import device_sessions as sessions
from pydantic import BaseModel, Field

async def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(no_store)])


async def current(token: str = Depends(oauth2_scheme), uid: str = Depends(get_current_active_user)):
    row = await get_database().device_sessions.find_one({"_id": sessions.fingerprint(token), "user_id": ObjectId(uid)})
    if not row or row.get("revoked_at"):
        raise sessions.denied()
    return row


def public(row, key):
    return {"id": str(row["id"]), "device_name": row["device_name"],
            "created_at": row["created_at"].isoformat() + "Z",
            "last_seen_at": row["last_seen_at"].isoformat() + "Z",
            "is_current": row["_id"] == key, "legacy": row.get("legacy", False)}


@router.get("")
async def listing(before: str | None = Query(None), row=Depends(current)):
    db = get_database()
    user = await db.users.find_one({"_id": row["user_id"]})
    query = {"user_id": row["user_id"], "revoked_at": {"$exists": False}, "$or": [
        {"generation": user.get("sessions_generation", 0)}, {"_id": user.get("sessions_preserved", "")} ]}
    if before:
        if not ObjectId.is_valid(before):
            raise HTTPException(400, "Invalid page")
        query["id"] = {"$lt": ObjectId(before)}
    rows = await db.device_sessions.find(query).sort("id", -1).limit(51).to_list(51)
    alerts = await db.security_login_alerts.find({"user_id": row["user_id"]}).sort("at", -1).limit(10).to_list(10)
    return {"sessions": [public(r, row["_id"]) for r in rows[:50]],
            "next_before": str(rows[49]["id"]) if len(rows) > 50 else None,
            "alerts": [{"id": str(a["_id"]), "device_name": a["device_name"], "at": a["at"].isoformat() + "Z"} for a in alerts]}


@router.delete("/current")
async def logout(row=Depends(current)):
    if row.get("legacy"):
        # Older builds can share this exact token between phones. Ordinary
        # local logout must not unexpectedly eject those other phones. The
        # client removes its own push token separately. Explicit remote or
        # "all other devices" sign-out is available for these shared logins.
        return {"signed_out": True, "server_revoked": False, "legacy": True}
    await sessions.revoke(row)
    return {"signed_out": True, "server_revoked": True}


class PreservePush(BaseModel):
    current_push_token: str | None = Field(default=None, max_length=4096)


@router.post("/revoke-others")
async def revoke_others(body: PreservePush | None = None, row=Depends(current)):
    db = get_database()
    replacement = None
    preserved = row
    keep_push = row.get("push_tokens", [])
    if row.get("legacy"):
        # Shared legacy tokens may also share the registry row. Retaining its
        # whole push list would still notify phones whose access was revoked.
        current_push = body.current_push_token if body else None
        keep_push = [current_push] if current_push and current_push in keep_push else []
        # Old builds could share identical JWTs. Keeping that token alive
        # would keep its copies alive too: rotate this caller first.
        from app.core.security import create_access_token
        import secrets
        replacement = create_access_token({"sub": str(row["user_id"]),
            "sid": secrets.token_urlsafe(32), "sg": row.get("generation", 0)})
        preserved = {**row, "_id": sessions.fingerprint(replacement), "id": ObjectId(),
                     "legacy": False, "created_at": sessions.now(), "push_tokens": keep_push}
        await db.device_sessions.insert_one(preserved)
    # One atomic account change also rejects unknown legacy tokens. Preserve
    # the initiating token explicitly; it need not change in local storage.
    await db.users.update_one({"_id": row["user_id"]}, {"$inc": {"sessions_generation": 1},
                             "$set": {"sessions_preserved": preserved["_id"],
                                      "expo_push_tokens": keep_push},
                             "$pull": {"badge_capable_tokens": {"$nin": keep_push}}})
    # Generation is the revocation barrier. Do not then loop and revoke rows:
    # a fresh login after the barrier must survive, even on another worker.
    return {"signed_out": True, "access_token": replacement}


@router.delete("/{session_id}")
async def revoke_one(session_id: str, row=Depends(current)):
    if not ObjectId.is_valid(session_id):
        raise HTTPException(404, "Sign-in not found")
    other = await get_database().device_sessions.find_one({"id": ObjectId(session_id), "user_id": row["user_id"]})
    if not other:
        raise HTTPException(404, "Sign-in not found")
    if other["_id"] == row["_id"]:
        raise HTTPException(400, "Use Log out to sign out this device")
    await sessions.revoke(other)
    return {"signed_out": True}
