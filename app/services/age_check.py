"""Birthday check ("age_check" switch, off by default).

Some older accounts carry a wrong birthday from the old sign-up screen (it saved
its own default when the picker was skipped), so Wano treats adults as children:
no messages, no voice quick posts, country instead of city. The current app never
asks for a birthday, so nobody could fix it.

- Anyone whose saved birthday makes them under 18 (including impossible dates,
  which already count as unknown, see home_ground.IMPLAUSIBLE_AGE) gets a gentle
  nudge: "Is your birthday right?" with Fix / It's right / Later.
- Trying to message while under 18 asks again.
- A birthday can be corrected once. The new one must make the person 13 to 110.
  The old value is kept in dob_history. "It's right" just stops the nudges (and
  still leaves the one correction available).
"""

from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from bson import ObjectId

from app.services import home_ground

MIN_AGE = 13
MAX_AGE = 110


def _now() -> datetime:
    return datetime.utcnow()


def state(user: dict, now: Optional[datetime] = None) -> dict:
    dob = user.get("date_of_birth")
    age = home_ground.age_of(dob, now)
    done = user.get("dob_checked") if isinstance(user.get("dob_checked"), dict) else {}
    fixed = done.get("action") == "fixed"
    young = age is not None and age < 18
    return {
        "birthday": dob.date().isoformat() if isinstance(dob, datetime) else None,
        "age": age,
        "treated_as_minor": home_ground.is_minor(user, now),
        "needs_check": young and not done,
        "can_fix": young and not fixed,
    }


async def _user(db, me: str) -> dict:
    u = await db.users.find_one({"_id": ObjectId(me)}, {"date_of_birth": 1, "dob_checked": 1})
    if not u:
        raise HTTPException(status_code=404, detail="Not found")
    return u


async def check(db, me: str) -> dict:
    return state(await _user(db, me))


async def confirm(db, me: str) -> dict:
    u = await _user(db, me)
    if not state(u)["needs_check"]:
        return state(u)
    await db.users.update_one({"_id": u["_id"], "dob_checked": {"$exists": False}},
                              {"$set": {"dob_checked": {"action": "confirmed", "at": _now()}}})
    return state(await _user(db, me))


def parse(text: str, today: Optional[date] = None) -> date:
    try:
        d = date.fromisoformat(text)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="That isn't a date")
    today = today or _now().date()
    years = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    if years < MIN_AGE:
        raise HTTPException(status_code=400, detail=f"You need to be {MIN_AGE} or older to use Wano")
    if years > MAX_AGE:
        raise HTTPException(status_code=400, detail="Check the year")
    return d


async def fix(db, me: str, text: str) -> dict:
    u = await _user(db, me)
    if not state(u)["can_fix"]:
        raise HTTPException(status_code=409, detail="Your birthday has already been corrected. "
                                                    "To change it again, contact Wano support.")
    d = parse(text)
    new = datetime(d.year, d.month, d.day)
    res = await db.users.update_one(
        {"_id": u["_id"], "dob_checked.action": {"$ne": "fixed"}},
        {"$set": {"date_of_birth": new, "dob_checked": {"action": "fixed", "at": _now()}, "updated_at": _now()},
         "$push": {"dob_history": {"old": u.get("date_of_birth"), "new": new, "at": _now()}}})
    if not res.modified_count:          # two at once: only the first counts
        raise HTTPException(status_code=409, detail="Your birthday has already been corrected.")
    return state(await _user(db, me))
