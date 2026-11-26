"""Revocable sign-ins. Only token digests are stored; old tokens migrate on use.

Revoked rows must NOT have a TTL: legacy tokens have no expiry and deleting a
tombstone would allow replay. A user generation invalidates even unseen tokens.
Device headers are display hints, never authorization or revocation keys.
"""
import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.core.database import get_database
from app.core.security import create_access_token, decode_access_token

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()


def clean(value, limit=80):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()[:limit]


def metadata(request):
    headers = request.headers if request is not None else {}
    device = clean(headers.get("X-Device-Id"), 64)
    # No IP address, precise location, serial number, email or raw user agent.
    label = clean(headers.get("X-Wano-Device-Name"))
    return {"device_key": hashlib.sha256(device.encode()).hexdigest() if device else None,
            "device_name": label or "Wano app or browser"}


def denied():
    return HTTPException(401, "Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})


async def validate(token, payload, request=None):
    db = get_database()
    uid = payload.get("sub")
    if not isinstance(uid, str) or not ObjectId.is_valid(uid):
        raise denied()
    user = await db.users.find_one({"_id": ObjectId(uid)})
    if not user:
        raise denied()
    from app.api.deps import _ensure_token_not_revoked
    _ensure_token_not_revoked(user, token)
    key = fingerprint(token)
    generation = payload.get("sg", 0)
    if not isinstance(generation, int) or (generation != user.get("sessions_generation", 0)
            and key != user.get("sessions_preserved")):
        raise denied()
    row = await db.device_sessions.find_one({"_id": key, "user_id": ObjectId(uid)})
    if row is None:
        # A new-style token must have a registered row. Only old signed tokens
        # migrate lazily; they remain usable until the user explicitly revokes.
        if payload.get("sid"):
            raise denied()
        stamp = now()
        legacy = {"_id": key, "id": ObjectId(), "user_id": ObjectId(uid),
                  "generation": generation, "created_at": stamp, "last_seen_at": stamp,
                  "legacy": True, **metadata(request)}
        try:
            await db.device_sessions.update_one({"_id": key}, {"$setOnInsert": legacy}, upsert=True)
        except DuplicateKeyError:
            pass
        row = await db.device_sessions.find_one({"_id": key, "user_id": ObjectId(uid)})
    if not row or row.get("revoked_at"):
        raise denied()
    # No writes for each video/feed request. At most one last-seen update per
    # session per 15 minutes, using a conditional update for concurrent workers.
    stamp = now()
    if row["last_seen_at"] < stamp - timedelta(minutes=15):
        await db.device_sessions.update_one(
            {"_id": key, "last_seen_at": {"$lt": stamp - timedelta(minutes=15)},
             "revoked_at": {"$exists": False}}, {"$set": {"last_seen_at": stamp}})
    if request is not None:
        request.state.device_session_user = user
    return row


async def mint(user, request=None, background_tasks=None):
    db = get_database()
    uid = user["_id"]
    # Re-read the generation: the caller may have read this user before a
    # concurrent "sign out others". Validation fails closed if it races again.
    fresh = await db.users.find_one({"_id": uid}, {"sessions_generation": 1})
    generation = (fresh or {}).get("sessions_generation", 0)
    token = create_access_token({"sub": str(uid), "sid": secrets.token_urlsafe(32), "sg": generation})
    stamp, hints = now(), metadata(request)
    previous = await db.device_sessions.find_one({"user_id": uid, "revoked_at": {"$exists": False}})
    known = hints["device_key"] and await db.device_sessions.find_one({
        "user_id": uid, "device_key": hints["device_key"], "revoked_at": {"$exists": False}})
    row = {"_id": fingerprint(token), "id": ObjectId(), "user_id": uid, "generation": generation,
           "created_at": stamp, "last_seen_at": stamp, "legacy": False, **hints}
    await db.device_sessions.insert_one(row)
    if previous and not known:
        event = {"_id": ObjectId(), "user_id": uid, "device_name": hints["device_name"], "at": stamp}
        await db.security_login_alerts.insert_one(event)
        if background_tasks is not None:
            background_tasks.add_task(deliver_alert, str(uid), str(event["_id"]))
    return token


async def deliver_alert(uid, event_id):
    """Best effort, after the login response; failure never cancels sign-in.
    Security history remains available even with notifications disabled.
    """
    import asyncio
    from app.services.growth_notifications import _send_one
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(uid), "is_active": True})
    event = await db.security_login_alerts.find_one({"_id": ObjectId(event_id), "user_id": ObjectId(uid)})
    if not user or not event:
        return
    # At most one security push per account per hour, claimed in the database.
    claimed = await db.users.update_one({"_id": ObjectId(uid), "$or": [
        {"security_push_at": {"$exists": False}}, {"security_push_at": {"$lt": now() - timedelta(hours=1)}}]},
        {"$set": {"security_push_at": now()}})
    if not claimed.modified_count:
        return
    async def send(token):
        try:
            # No new notification-list type: old apps can display this safely.
            await asyncio.wait_for(_send_one(token, "A new device signed in. Review Signed-in devices in Wano Settings.", {}, "Wano account security"), timeout=5)
        except Exception:
            logger.warning("Could not deliver new-device security alert")
    await asyncio.gather(*(send(t) for t in list(dict.fromkeys(user.get("expo_push_tokens", [])))[:20]))


async def revoke(row):
    db = get_database()
    await db.device_sessions.update_one({"_id": row["_id"], "user_id": row["user_id"]},
                                        {"$set": {"revoked_at": now()}})
    tokens = row.get("push_tokens", [])
    if tokens:
        # A different active session may now own this push token after an
        # account switch / fresh sign-in. Preserve any current owners.
        for token in tokens:
            live = await db.device_sessions.find_one({"user_id": row["user_id"],
                "_id": {"$ne": row["_id"]}, "revoked_at": {"$exists": False}, "push_tokens": token})
            if not live:
                await db.users.update_one({"_id": row["user_id"]}, {"$pull": {"expo_push_tokens": token, "badge_capable_tokens": token}})


async def attach_push(token, push_token):
    payload = decode_access_token(token)
    row = await validate(token, payload or {})
    db = get_database()
    # Assign push ownership to the current sign-in, not merely the account.
    await db.device_sessions.update_many({"_id": {"$ne": row["_id"]}, "push_tokens": push_token},
                                         {"$pull": {"push_tokens": push_token}})
    result = await db.device_sessions.update_one({"_id": row["_id"], "revoked_at": {"$exists": False}},
                                                 {"$addToSet": {"push_tokens": push_token}})
    if not result.matched_count:
        raise denied()
