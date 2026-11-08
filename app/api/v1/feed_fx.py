"""Local reactions on posts (app/services/feed_fx.py). Behind the "feed_fx" switch."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictBool

from app.api.deps import get_current_active_user
from app.api.v1.feed import _viewer_if_signed_in
from app.core.database import get_database
from app.services import creator_push, feed_fx, home_ground as hg

router = APIRouter()


class ReactionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["plus_one", "eish", "ayaa", "fire"]
    on: StrictBool = True


async def _ready(db):
    await creator_push.require(db, "feed_fx")
    await hg.load_custom(db)


@router.get("/{video_id}/reactions")
async def get_reactions(video_id: str, current_user=Depends(_viewer_if_signed_in)):
    db = get_database()
    await _ready(db)
    return await feed_fx.summary(db, video_id, current_user)


@router.post("/{video_id}/reactions")
async def post_reaction(video_id: str, body: ReactionIn, current_user: str = Depends(get_current_active_user)):
    db = get_database()
    await _ready(db)
    return await feed_fx.react(db, video_id, current_user, body.kind, body.on)


@router.get("/{video_id}/top-comment")
async def get_top_comment(video_id: str, current_user=Depends(_viewer_if_signed_in)):
    db = get_database()
    await creator_push.require(db, "feed_fx")
    return {"comment": await feed_fx.top_comment(db, video_id, current_user)}
