"""Birthday for accounts without one ("birthday_needed" switch, off by default).

Many accounts have no usable birthday (never asked, or an impossible date), and
Wano treats them as adults. Like Instagram did in 2021:

- The app (2.3.62+) asks for the birthday, at most once a day, and never in an
  account's first NEW_ACCOUNT_DAYS days (first impressions come first).
- "birthday_required" (separate switch): "Not now" then works 3 times (on 3
  different days); after that a birthday is needed to post, comment or send
  messages. Watching, liking, following and reporting still work. With only
  "birthday_needed" on, the ask is friendly and never blocks anything.
- Old apps never send "Not now", so their users are never blocked.
- A birthday is added once. After that it only changes through Wano support
  (Admin), or the one-time correction in age_check for under-18s.
- With the switch on, PATCH/PUT /users/me can no longer change a birthday that
  is already there (that was a way round the teen rules), and a new one must
  make the person 13 to 110.

"birthday_unknown_teen" (separate switch): posts by people with no usable
birthday get the 16-17 comment rule (comment_guard.teen_reason).
"""

from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException

from app.services import age_check, home_ground

SWITCH = "birthday_needed"
REQUIRED = "birthday_required"
NEW_ACCOUNT_DAYS = 3
UNKNOWN_TEEN = "birthday_unknown_teen"
FREE_SKIPS = 3
SKIP_GAP = timedelta(hours=20)       # one "Not now" a day counts
NEEDED_TEXT = "Add your birthday in the Wano app to post, comment and send messages."
_FIELDS = {"date_of_birth": 1, "birthday_skips": 1, "birthday_skip_at": 1, "created_at": 1}


def _now() -> datetime:
    return datetime.utcnow()


def usable(dob, now: Optional[datetime] = None) -> bool:
    years = home_ground.age_of(dob, now)
    return years is not None and home_ground.IMPLAUSIBLE_AGE <= years <= age_check.MAX_AGE


def _new_account(user: dict, now: datetime) -> bool:
    made = user.get("created_at")
    if not isinstance(made, datetime):
        return False
    return now - made.replace(tzinfo=None) < timedelta(days=NEW_ACCOUNT_DAYS)


def state(user: dict, now: Optional[datetime] = None, required_on: bool = False) -> dict:
    """needed: no usable birthday. ask: the app may ask at launch (not in a new
    account's first days). skips_left / required: only with birthday_required on."""
    now = now or _now()
    needed = not usable(user.get("date_of_birth"), now)
    skips = int(user.get("birthday_skips") or 0)
    counting = needed and required_on
    return {"needed": needed, "ask": needed and not _new_account(user, now),
            "skips_left": max(0, FREE_SKIPS - skips) if counting else 0,
            "required": counting and skips >= FREE_SKIPS}


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH)


async def required_on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, SWITCH) and await creator_push.on(db, REQUIRED)


async def _user(db, me: str) -> dict:
    try:
        oid = ObjectId(str(me))
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")
    u = await db.users.find_one({"_id": oid}, _FIELDS)
    if not u:
        raise HTTPException(status_code=404, detail="Not found")
    return u


async def check(db, me: str) -> dict:
    return state(await _user(db, me), required_on=await required_on(db))


async def later(db, me: str, now: Optional[datetime] = None) -> dict:
    """'Not now'. Counts at most once a day, so 3 means 3 days."""
    now = now or _now()
    u = await _user(db, me)
    req = await required_on(db)
    if req and state(u, now, req)["needed"]:          # only counted while it can lead somewhere
        await db.users.update_one(
            {"_id": u["_id"], "$or": [{"birthday_skip_at": {"$exists": False}},
                                      {"birthday_skip_at": {"$lte": now - SKIP_GAP}}]},
            {"$inc": {"birthday_skips": 1}, "$set": {"birthday_skip_at": now}})
    return state(await _user(db, me), now, req)


async def add(db, me: str, text: str) -> dict:
    u = await _user(db, me)
    if not state(u)["needed"]:
        raise HTTPException(status_code=409, detail="Your account already has a birthday. "
                                                    "To change it, contact Wano support.")
    d = age_check.parse(text)
    new = datetime(d.year, d.month, d.day)
    old = u.get("date_of_birth")
    res = await db.users.update_one(
        {"_id": u["_id"], "date_of_birth": old},
        {"$set": {"date_of_birth": new, "updated_at": _now()}, "$unset": {"birthday_skips": "", "birthday_skip_at": ""},
         "$push": {"dob_history": {"old": old, "new": new, "at": _now(), "via": "added"}}})
    if not res.modified_count:          # two at once: only the first counts
        raise HTTPException(status_code=409, detail="Your account already has a birthday.")
    return state(await _user(db, me), required_on=await required_on(db))


async def ensure(db, user_id) -> None:
    """403 when a birthday is required (birthday_required) and the free 'Not now's are used up."""
    if not await required_on(db):
        return
    try:
        oid = ObjectId(str(user_id))
    except Exception:
        return
    u = await db.users.find_one({"_id": oid}, _FIELDS)
    if u and state(u, required_on=True)["required"]:
        raise HTTPException(status_code=403, detail=NEEDED_TEXT)


async def profile_birthday(db, user: dict, new) -> Optional[datetime]:
    """What PATCH/PUT /users/me may save as the birthday: None means leave it.
    Switch off: as before (anything)."""
    if not await on(db):
        return new
    if new is None or usable(user.get("date_of_birth")):
        return None                    # already set: only support changes it
    if not isinstance(new, datetime):
        return None
    try:
        d = age_check.parse(new.date().isoformat())
    except HTTPException:
        return None                    # an impossible date (old apps' defaults): ignored, the rest still saves
    return datetime(d.year, d.month, d.day)


async def staff_set(db, admin: str, uid: str, text: str, reason: str) -> dict:
    """Admin > Users: Wano support changes someone's birthday (kept in dob_history)."""
    u = await _user(db, uid)
    d = age_check.parse(text)
    new = datetime(d.year, d.month, d.day)
    await db.users.update_one(
        {"_id": u["_id"]},
        {"$set": {"date_of_birth": new, "updated_at": _now()}, "$unset": {"birthday_skips": "", "birthday_skip_at": ""},
         "$push": {"dob_history": {"old": u.get("date_of_birth"), "new": new, "at": _now(), "via": "support",
                                   "by": admin, "reason": reason[:300]}}})
    return {"birthday": new.date().isoformat(), "age": home_ground.age_of(new)}
