"""Wano TV film submissions from wanoafrica.com/tv/submit (Nov 2026).

The website used to keep submissions only in two emails, so a failed email
lost the film. Now every submission is saved here first and listed in
Admin > Film submissions, where the team moves it from New to Published.
The poster arrives already shrunk by the filmmaker's browser.
"""

import random
import re
from datetime import datetime, timedelta
from typing import Optional

STATUSES = ("new", "reviewing", "shortlisted", "selected", "declined", "licensed", "published")
FIELDS = {   # name: (max length, required)
    "full_name": (120, True), "email": (200, True), "phone": (40, False), "country": (80, True),
    "city": (80, False), "production_company": (160, False), "film_title": (200, True),
    "country_represented": (120, True), "year_completed": (10, True), "runtime": (40, True),
    "category": (60, True), "languages": (200, True), "subtitles": (200, True),
    "synopsis": (4000, True), "viewing_link": (600, True), "viewing_password": (200, False),
    "trailer_link": (600, False), "poster_file_name": (200, False),
}
POSTER_MAX = 1_500_000            # characters of data URL (about 1.1 MB of picture)
_POSTER = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PER_SENDER_HOUR = 5               # one phone/computer: at most 5 an hour
PER_DAY = 300                     # everyone together: at most 300 a day
SAME_FILM_MINUTES = 15            # a double tap returns the first submission


class Invalid(ValueError):
    pass


class TooMany(Exception):
    pass


def clean(data: dict) -> dict:
    out = {}
    for name, (limit, required) in FIELDS.items():
        v = data.get(name)
        v = v.strip() if isinstance(v, str) else ""
        if required and not v:
            raise Invalid(f"Missing {name.replace('_', ' ')}")
        out[name] = v[:limit]
    if not _EMAIL.match(out["email"]):
        raise Invalid("Check the email address")
    for link in ("viewing_link", "trailer_link"):
        if out[link] and not re.match(r"^https?://", out[link], re.I):
            raise Invalid(f"The {link.replace('_', ' ')} must start with http:// or https://")
    if data.get("rights_confirmed") is not True:
        raise Invalid("Confirm that you own or control the rights to this film")
    poster = data.get("poster_data_url")
    out["poster"] = poster if isinstance(poster, str) and len(poster) <= POSTER_MAX and _POSTER.match(poster) else None
    return out


async def create(db, data: dict, who: str = "", now: Optional[datetime] = None) -> dict:
    """Save a submission. Returns {"ref", "created": bool}."""
    now = now or datetime.utcnow()
    doc = clean(data)
    same = await db.film_submissions.find_one({
        "email": doc["email"].lower(), "film_title": doc["film_title"],
        "created_at": {"$gte": now - timedelta(minutes=SAME_FILM_MINUTES)}}, {"_id": 1})
    if same:
        return {"ref": same["_id"], "created": False}
    who = (who or "")[:128]
    if who and await db.film_submissions.count_documents(
            {"who": who, "created_at": {"$gte": now - timedelta(hours=1)}}) >= PER_SENDER_HOUR:
        raise TooMany()
    if await db.film_submissions.count_documents({"created_at": {"$gte": now - timedelta(days=1)}}) >= PER_DAY:
        raise TooMany()
    doc.update(email=doc["email"].lower(), rights_confirmed=True, has_poster=bool(doc["poster"]), status="new", who=who,
               created_at=now, updated_at=now, history=[])
    for _ in range(20):
        ref = f"WTV-{random.randint(100000, 999999)}"
        try:
            await db.film_submissions.insert_one({"_id": ref, **doc})
            return {"ref": ref, "created": True}
        except Exception as e:          # the same ref already used: pick another
            if "duplicate" not in str(e).lower() and "E11000" not in str(e):
                raise
    raise RuntimeError("Could not pick a reference")


def _row(d: dict, poster: bool = False) -> dict:
    out = {k: d.get(k) for k in FIELDS}
    out.update(ref=d["_id"], status=d.get("status", "new"), note=d.get("note", ""),
               has_poster=bool(d.get("has_poster") or d.get("poster")),
               created_at=d["created_at"].isoformat() if d.get("created_at") else None,
               updated_at=d["updated_at"].isoformat() if d.get("updated_at") else None,
               history=d.get("history", []))
    if poster:
        out["poster"] = d.get("poster")
    return out


async def listing(db, status: Optional[str] = None, limit: int = 200) -> dict:
    q = {"status": status} if status in STATUSES else {}
    rows = [_row(d) async for d in db.film_submissions.find(q, {"poster": 0}).sort("created_at", -1).limit(limit)]
    counts = {s: 0 for s in STATUSES}
    async for c in db.film_submissions.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        if c["_id"] in counts:
            counts[c["_id"]] = c["n"]
    return {"rows": rows, "counts": counts, "statuses": list(STATUSES)}


async def get(db, ref: str) -> Optional[dict]:
    d = await db.film_submissions.find_one({"_id": ref})
    return _row(d, poster=True) if d else None


async def set_status(db, ref: str, status: str, note: Optional[str], admin: str,
                     now: Optional[datetime] = None) -> Optional[dict]:
    if status not in STATUSES:
        raise Invalid("Unknown status")
    now = now or datetime.utcnow()
    upd = {"status": status, "updated_at": now}
    if note is not None:
        upd["note"] = note.strip()[:2000]
    r = await db.film_submissions.find_one_and_update(
        {"_id": ref}, {"$set": upd, "$push": {"history": {"status": status, "by": admin, "at": now.isoformat()}}},
        projection={"poster": 0}, return_document=True)
    return _row(r) if r else None
