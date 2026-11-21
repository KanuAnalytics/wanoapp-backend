"""Weekly challenges (see app/services/challenges.py)."""

from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.deps import get_optional_active_user, require_admin
from app.core.database import get_database
from app.services import admin_log
from app.services import challenges as svc

router = APIRouter()


class ChallengeIn(BaseModel):
    hashtag: str
    title: str = Field(..., min_length=2, max_length=80)
    description: str = Field("", max_length=500)
    starts_at: datetime
    ends_at: datetime
    prize_points: int = Field(0, ge=0, le=100_000)

    @field_validator("hashtag")
    @classmethod
    def _tag(cls, v):
        m = svc.HASHTAG.match(v.strip())
        if not m:
            raise ValueError("hashtag: 2-40 letters, numbers or _")
        return m.group(1).lower()

    @model_validator(mode="after")
    def _window(self):
        start, end = svc._naive(self.starts_at), svc._naive(self.ends_at)
        if end <= start or end <= svc._naive(svc._utcnow()):
            raise ValueError("ends_at must be after starts_at and in the future")
        if end - start > timedelta(days=svc.MAX_DAYS):
            raise ValueError(f"at most {svc.MAX_DAYS} days")
        return self


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: ChallengeIn, admin: str = Depends(require_admin)):
    db = get_database()
    doc = {"hashtag": body.hashtag, "title": body.title.strip(), "description": body.description.strip(),
           "starts_at": svc._naive(body.starts_at), "ends_at": svc._naive(body.ends_at),
           "prize_points": body.prize_points, "created_by": ObjectId(admin), "created_at": datetime.utcnow()}
    from app.services import challenge_fair
    if await challenge_fair.on(db):
        doc["rules"] = challenge_fair.RULES     # fair scoring for its whole life
    doc["_id"] = (await db.challenges.insert_one(doc)).inserted_id
    await admin_log.record(db, admin, "challenge_create", {"id": str(doc["_id"]), "hashtag": doc["hashtag"]})
    return svc.public(doc, svc._utcnow())


@router.get("")
async def list_challenges(current_user: Optional[str] = Depends(get_optional_active_user)):
    """Live, upcoming, and ended in the last 14 days. Logged out: authenticated=false."""
    db = get_database()
    now = svc._utcnow()
    docs = await db.challenges.find({"ends_at": {"$gte": svc._naive(now) - timedelta(days=14)}}).to_list(200)
    # One ended early within its first hour was a test or a mistake: not shown.
    docs = [d for d in docs if not (d.get("ended_early_by") and d["ends_at"] - d["starts_at"] < timedelta(hours=1))]
    order = {"live": 0, "upcoming": 1, "ended": 2}
    out = [svc.public(d, now) for d in docs]
    out.sort(key=lambda c: (order[c["state"]], c["ends_at"]))
    return out


@router.get("/{challenge_id}/leaderboard")
async def leaderboard(challenge_id: str, limit: int = Query(50, ge=1, le=100),
                      current_user: Optional[str] = Depends(get_optional_active_user)):
    db = get_database()
    if not ObjectId.is_valid(challenge_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid challenge ID")
    ch = await db.challenges.find_one({"_id": ObjectId(challenge_id)})
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
    from app.services import challenge_fair
    if challenge_fair.fair(ch):
        rows = svc.without_example(ch, await challenge_fair.board(db, ch))
        return {"challenge": svc.public(ch, svc._utcnow()), "entries": rows[:limit],
                "mine": await challenge_fair.mine(db, ch, rows, current_user)}
    rows = svc.without_example(ch, await svc.ranking(db, ch))
    return {"challenge": svc.public(ch, svc._utcnow()), "entries": rows[:limit]}


class LookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cover: Optional[str] = Field(None, max_length=1024)
    cover_small: Optional[str] = Field(None, max_length=1024)
    alt: Optional[str] = Field(None, max_length=200)
    example_video: Optional[str] = Field(None, max_length=40)
    sound_id: Optional[str] = Field(None, max_length=40)
    discover: bool = False


@router.put("/{challenge_id}/look")
async def set_look(challenge_id: str, body: LookIn, admin: str = Depends(require_admin)):
    """challenge_plus: cover picture (uploaded on the Admin page), an example
    video Wano picks (its creator can't win), a sound and a Discover banner."""
    db = get_database()
    if not ObjectId.is_valid(challenge_id):
        raise HTTPException(status_code=400, detail="Invalid challenge ID")
    ch = await db.challenges.find_one({"_id": ObjectId(challenge_id)})
    if not ch:
        raise HTTPException(status_code=404, detail="Challenge not found")
    from app.services.whats_new import _cdn
    for u in (body.cover, body.cover_small):
        if u is not None and not _cdn(u):
            raise HTTPException(400, "Pictures must be on Wano's own storage")
    if body.cover and not (body.alt or "").strip():
        raise HTTPException(400, "Describe the cover (for people using a screen reader)")
    look = {"cover": body.cover, "cover_small": body.cover_small, "alt": (body.alt or "").strip(),
            "discover": body.discover}
    if body.example_video:
        if not ObjectId.is_valid(body.example_video):
            raise HTTPException(400, "Invalid video")
        v = await db.videos.find_one({"_id": ObjectId(body.example_video), "is_active": True, "privacy": "public"},
                                     {"creator_id": 1})
        if not v:
            raise HTTPException(404, "That video isn't public")
        look.update({"example_video": body.example_video, "example_creator": v.get("creator_id")})
    if body.sound_id:
        # Only a sound from Wano's own music catalogue (one already used on a post).
        if not await db.videos.find_one({"sound.id": body.sound_id, "is_active": True}, {"_id": 1}):
            raise HTTPException(404, "Sound not found in Wano's music")
        look["sound_id"] = body.sound_id
    await db.challenges.update_one({"_id": ch["_id"]}, {"$set": {"look": look}})
    await admin_log.record(db, admin, "challenge_look", {"id": challenge_id})
    return svc.public({**ch, "look": look}, svc._utcnow())


class ChallengeEdit(BaseModel):
    title: Optional[str] = Field(None, min_length=2, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    hashtag: Optional[str] = None


@router.put("/{challenge_id}")
async def edit_challenge(challenge_id: str, body: ChallengeEdit, admin: str = Depends(require_admin)):
    """Fix the title or description any time; the hashtag only before it starts."""
    db = get_database()
    if not ObjectId.is_valid(challenge_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid challenge ID")
    ch = await db.challenges.find_one({"_id": ObjectId(challenge_id)})
    if not ch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
    upd = {}
    if body.title is not None:
        upd["title"] = body.title.strip()
    if body.description is not None:
        upd["description"] = body.description.strip()
    if body.hashtag is not None:
        if svc.state(ch, svc._utcnow()) != "upcoming":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="The hashtag can't change once the challenge has started")
        m = svc.HASHTAG.match(body.hashtag.strip())
        if not m:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="hashtag: 2-40 letters, numbers or _")
        upd["hashtag"] = m.group(1).lower()
    if upd:
        await db.challenges.update_one({"_id": ch["_id"]}, {"$set": upd})
        await admin_log.record(db, admin, "challenge_edit", {"id": challenge_id, **upd})
    return svc.public({**ch, **upd}, svc._utcnow())
