"""Profile extras: "Followed by" faces (people you follow who follow this
account). Behind the "profile_plus" switch."""

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creator_push

router = APIRouter()

# A big account's followers list is read from its newest end only; mutuals are
# shown as a hint, so missing someone who followed years ago is fine.
MAX_SCAN = 100_000
FACES = 3


def _oid(v):
    if isinstance(v, ObjectId):
        return v
    try:
        return ObjectId(str(v))
    except Exception:
        return None


@router.get("/{user_id}/followed-by")
async def followed_by(user_id: str, current_user: str = Depends(get_current_active_user)):
    """{count, users[<=3]}: people you follow who also follow `user_id`."""
    db = get_database()
    await creator_push.require(db, "profile_plus")
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user ID")
    target = ObjectId(user_id)
    me = _oid(current_user)
    empty = {"count": 0, "users": []}
    if me is None or me == target:
        return empty
    mine = await db.users.find_one({"_id": me}, {"following": {"$slice": -MAX_SCAN}, "blocked_users": 1,
                                                 "blocked_by": 1})
    theirs = await db.users.find_one({"_id": target, "is_active": True},
                                     {"followers": {"$slice": -MAX_SCAN}})
    if not mine or not theirs:
        return empty
    blocked = {_oid(x) for x in (mine.get("blocked_users") or []) + (mine.get("blocked_by") or [])}
    if target in blocked:
        return empty
    following = mine.get("following") or []
    followers = {_oid(x) for x in (theirs.get("followers") or [])}
    # Newest follows first: those are the faces you're likeliest to know.
    common, seen = [], set()
    for raw in reversed(following if isinstance(following, list) else []):
        o = _oid(raw)
        if o is None or o in seen or o in blocked or o == target or o == me:
            continue
        seen.add(o)
        if o in followers:
            common.append(o)
    if not common:
        return empty
    # Faces: active accounts, ones with a picture first.
    docs = [d async for d in db.users.find(
        {"_id": {"$in": common[:30]}, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1})]
    order = {o: i for i, o in enumerate(common)}
    docs.sort(key=lambda d: (not d.get("profile_picture"), order.get(d["_id"], 0)))
    users = [{"id": str(d["_id"]), "username": d.get("username") or "",
              "display_name": d.get("display_name") or d.get("username") or "",
              "profile_picture": d.get("profile_picture") or None} for d in docs[:FACES]]
    return {"count": len(common), "users": users}
