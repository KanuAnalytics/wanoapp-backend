"""Staff: who may moderate, and how they prove it's them (Nov 2026).

Roles (users.staff_role): "moderator" < "admin" < "owner".
- Accounts with user_type "admin" are admins without setting anything, so the
  Admin page and every existing admin check work exactly as before.
- A moderator is an ordinary account with staff_role "moderator". Moderators
  never pass require_admin (it reads user_type), so the new role can't open
  anything that was admin-only; they reach only the /mod endpoints, and each
  of those names the least role it needs.
- Owners (staff_role "owner" on an admin account) can add and remove staff and
  wipe accounts. Until anyone is made owner, every admin counts as one, so
  nothing is locked out on the day this ships.

Proving it's them (the "staff session"). An app sign-in token never expires,
so a stolen phone or token would carry staff powers for ever. The /mod
endpoints therefore also want either:
- an Admin page sign-in (its token expires after SESSION_HOURS), or
- a staff session from the app: the person types a 6-digit code from an
  authenticator app (TOTP, RFC 6238; never SMS, which can be hijacked by a
  SIM swap). The server answers with a session id that is tied to the device
  id the app sent, ends after IDLE minutes without an action, and lasts at
  most MAX_HOURS. Only a hash of it is stored. Owners can end every staff
  session at once.
Every staff account must be an adult; staff accounts are kept out of search,
recommendations and every count ("staff_quiet" switch, see quiet()).
"""

import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

ROLES = ("moderator", "admin", "owner")
RANK = {r: i for i, r in enumerate(ROLES)}
IDLE = timedelta(minutes=15)
MAX_HOURS = 2
CODE_TRIES_PER_HOUR = 5
STAFF_CACHE_SECONDS = 60


def role_of(user: Optional[dict]) -> Optional[str]:
    if not user or user.get("is_active") is False:
        return None
    r = user.get("staff_role")
    if r in ROLES:
        if r in ("admin", "owner") and str(user.get("user_type", "")).lower() != "admin":
            return "moderator"          # admin powers only ever with an admin account
        return r
    if str(user.get("user_type", "")).lower() == "admin":
        return "admin"
    return None


def is_staff(user: Optional[dict]) -> bool:
    return role_of(user) is not None


async def any_owner(db) -> bool:
    return await db.users.find_one({"staff_role": "owner", "user_type": "admin", "is_active": True}, {"_id": 1}) is not None


async def effective_role(db, user: dict) -> Optional[str]:
    r = role_of(user)
    if r == "admin" and not await any_owner(db):
        return "owner"
    return r


def is_adult(user: dict) -> bool:
    """Staff need a known birthday of 18 or more (unknown isn't enough)."""
    from app.services import home_ground
    years = home_ground.age_of(user.get("date_of_birth"))
    return years is not None and years >= 18


# ------------------------------------------------------------------ TOTP
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def code_ok(secret: str, code: str, now: Optional[float] = None, used_step: Optional[int] = None) -> Optional[int]:
    """The time step the code belongs to (one step either side), or None."""
    if not secret or not isinstance(code, str) or not code.isdigit() or len(code) != 6:
        return None
    t = int((now if now is not None else time.time()) // 30)
    for step in (t, t - 1, t + 1):
        if used_step is not None and step <= used_step:
            continue                    # a code can't be used twice
        if hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, label: str) -> str:
    from urllib.parse import quote
    return f"otpauth://totp/Wano%20staff:{quote(label)}?secret={secret}&issuer=Wano%20staff"


# ------------------------------------------------------------------ sessions
def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


async def start_session(db, user: dict, code: str, device: str) -> dict:
    now = datetime.utcnow()
    if not device or len(device) > 100:
        raise HTTPException(400, "Missing device")
    since = now - timedelta(hours=1)
    tries = await db.staff_code_tries.count_documents({"user": user["_id"], "at": {"$gte": since}})
    if tries >= CODE_TRIES_PER_HOUR:
        raise HTTPException(429, "Too many tries. Wait an hour, or ask an owner.")
    secret = (user.get("staff_totp") or {}).get("secret")
    if not secret:
        raise HTTPException(409, "Set up your staff code first (an owner does this on the Admin page)")
    step = code_ok(secret, code, used_step=(user.get("staff_totp") or {}).get("last_step"))
    if step is None:
        await db.staff_code_tries.insert_one({"user": user["_id"], "at": now})
        raise HTTPException(401, "That code didn't work")
    # Claim the step so the same code can't be replayed.
    r = await db.users.update_one({"_id": user["_id"], "$or": [{"staff_totp.last_step": {"$lt": step}},
                                                               {"staff_totp.last_step": {"$exists": False}}]},
                                  {"$set": {"staff_totp.last_step": step}})
    if not r.modified_count:
        raise HTTPException(401, "That code was already used")
    sid = secrets.token_urlsafe(32)
    await db.staff_sessions.insert_one({"_id": _hash(sid), "user": user["_id"], "device": _hash(device),
                                        "created_at": now, "last_at": now,
                                        "ends_at": now + timedelta(hours=MAX_HOURS)})
    try:
        await db.staff_sessions.create_index("ends_at", expireAfterSeconds=0)
    except Exception:
        pass
    return {"session": sid, "idle_minutes": int(IDLE.total_seconds() // 60), "max_hours": MAX_HOURS}


async def check_session(db, user_id: ObjectId, sid: Optional[str], device: Optional[str]) -> bool:
    if not sid or not device:
        return False
    now = datetime.utcnow()
    s = await db.staff_sessions.find_one({"_id": _hash(sid)})
    if not s or s.get("user") != user_id or s.get("device") != _hash(device):
        return False
    if s.get("ends_at") <= now or s.get("last_at") < now - IDLE or s.get("revoked"):
        return False
    await db.staff_sessions.update_one({"_id": s["_id"]}, {"$set": {"last_at": now}})
    return True


async def end_all(db, user_id: Optional[ObjectId] = None) -> int:
    q = {"user": user_id} if user_id is not None else {}
    r = await db.staff_sessions.update_many({**q, "revoked": {"$ne": True}}, {"$set": {"revoked": True}})
    return r.modified_count


# ------------------------------------------------------------------ the dependency
_bearer = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login", auto_error=False)


def _web_admin_token(token: Optional[str]) -> bool:
    """An Admin page sign-in (admin /login): it expires and says so."""
    try:
        payload = jwt.decode(token or "", settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return False
    return payload.get("scope") == "admin_web" and isinstance(payload.get("exp"), (int, float))


class Staff:
    def __init__(self, user: dict, role: str, via: str):
        self.user, self.role, self.via = user, role, via
        self.id = str(user["_id"])
        self.oid = user["_id"]


def need(min_role: str = "moderator"):
    """FastAPI dependency: a staff member with at least `min_role`, proved by an
    Admin page sign-in or an app staff session."""
    from app.api.deps import get_current_active_user
    from app.core.database import get_database

    async def dep(current_user: str = Depends(get_current_active_user),
                  token: Optional[str] = Depends(_bearer),
                  x_staff_session: Optional[str] = Header(None, max_length=100),
                  x_staff_device: Optional[str] = Header(None, max_length=100)) -> Staff:
        db = get_database()
        user = await db.users.find_one({"_id": ObjectId(current_user)})
        role = await effective_role(db, user) if user else None
        if role is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff only")
        if RANK[role] < RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Needs {min_role}")
        if role in ("admin", "owner") and _web_admin_token(token):
            return Staff(user, role, "web")
        if await check_session(db, user["_id"], x_staff_session, x_staff_device):
            return Staff(user, role, "app")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Staff code needed",
                            headers={"X-Staff-Session": "required"})
    return dep


# ------------------------------------------------------------------ staff_quiet
_cache = {"at": 0.0, "ids": frozenset(), "on": False}


async def quiet_ids(db) -> frozenset:
    """Staff accounts whose activity counts nowhere (empty while staff_quiet is off).
    Refreshed every STAFF_CACHE_SECONDS; a failed read keeps the last answer."""
    now = time.monotonic()
    if now - _cache["at"] < STAFF_CACHE_SECONDS:
        return _cache["ids"] if _cache["on"] else frozenset()
    try:
        from app.services import creator_push
        on = await creator_push.on(db, "staff_quiet")
        ids = frozenset()
        if on:
            ids = frozenset([str(u["_id"]) async for u in db.users.find(
                {"$or": [{"user_type": "admin"}, {"staff_role": {"$in": list(ROLES)}}]}, {"_id": 1})])
        _cache.update(at=now, ids=ids, on=on)
    except Exception:
        logger.warning("staff list not refreshed", exc_info=True)
    return _cache["ids"] if _cache["on"] else frozenset()


def forget() -> None:
    _cache["at"] = 0.0


async def quiet(db, user_id) -> bool:
    return str(user_id) in await quiet_ids(db)
