"""
Creator stats: GET /api/v1/creators/me/stats?days=7

How the signed-in creator's posts are doing: views, watch %, finishes, likes,
comments, shares for the period and the one before it, the posting streak,
and each recent post's numbers and first-viewer stage. Only ever the caller's
own data, and nothing about who watched.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import creators
from app.services.creators import _num

router = APIRouter()

MAX_POSTS = 20


class Totals(BaseModel):
    views: int
    previous_views: int
    views_change_pct: Optional[float] = None
    finished: int
    avg_watch_pct: Optional[float] = None
    likes: int
    comments: int
    shares: int
    posts: int


class Streak(BaseModel):
    current: int
    posted_today: bool
    boost_active: bool
    next_milestone: Optional[int] = None


class PostStats(BaseModel):
    id: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    created_at: Optional[datetime] = None
    views: int
    avg_watch_pct: Optional[float] = None
    finish_rate_pct: Optional[float] = None
    likes: int
    comments: int
    shares: int
    reach_stage: str      # "testing" | "boosted" | "done"


class CreatorStats(BaseModel):
    period_days: int
    totals: Totals
    streak: Streak
    posts: List[PostStats]


def _pct(part: float, whole: float) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole > 0 else None


async def _sum_days(db, creator: ObjectId, start: datetime, end: datetime) -> dict:
    tot = {k: 0.0 for k in ("views", "finished", "portion_sum", "likes", "comments", "shares")}
    async for row in db.creator_daily.find({"creator_id": creator, "day": {"$gte": start, "$lt": end}}):
        if not isinstance(row.get("day"), datetime):
            continue
        for k in tot:
            tot[k] += _num(row.get(k))
    return tot


@router.get("/me/stats", response_model=CreatorStats)
async def my_stats(
    days: int = Query(7, ge=1, le=30),
    current_user: str = Depends(get_current_active_user),
):
    db = get_database()
    me = ObjectId(current_user)
    today = creators._day(datetime.now(timezone.utc))
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    cur = await _sum_days(db, me, start, end)
    prev = await _sum_days(db, me, start - timedelta(days=days), start)

    posts_q = {"creator_id": me, "is_active": True, "created_at": {"$gte": start, "$lt": end},
               "status": {"$nin": ["scheduled", "publishing", "cancelled"]}}
    post_count = await db.videos.count_documents(posts_q)

    s = (await creators.streaks(db, [me]))[me]
    recent = await db.videos.find(posts_q, {"title": 1, "urls.thumbnail": 1, "created_at": 1,
                                            "likes_count": 1, "comments_count": 1, "shares_count": 1}
                                  ).sort([("created_at", -1), ("_id", -1)]).limit(MAX_POSTS).to_list(MAX_POSTS)
    reach = {r["_id"]: r async for r in db.video_reach.find({"_id": {"$in": [v["_id"] for v in recent]}})}
    posts = []
    for v in recent:
        r = reach.get(v["_id"], {})
        views = _num(r.get("views"))
        urls = v.get("urls") if isinstance(v.get("urls"), dict) else {}
        posts.append(PostStats(
            id=str(v["_id"]),
            title=v.get("title") if isinstance(v.get("title"), str) else None,
            thumbnail=urls.get("thumbnail") if isinstance(urls.get("thumbnail"), str) else None,
            created_at=v.get("created_at") if isinstance(v.get("created_at"), datetime) else None,
            views=int(views),
            avg_watch_pct=_pct(_num(r.get("portion_sum")), views),
            finish_rate_pct=_pct(_num(r.get("finished")), views),
            likes=int(_num(v.get("likes_count"))),
            comments=int(_num(v.get("comments_count"))),
            shares=int(_num(v.get("shares_count"))),
            reach_stage=creators.stage_label(r, s["current"], v.get("created_at")),
        ))

    return CreatorStats(
        period_days=days,
        totals=Totals(
            views=int(cur["views"]), previous_views=int(prev["views"]),
            views_change_pct=_pct(cur["views"] - prev["views"], prev["views"]),
            finished=int(cur["finished"]), avg_watch_pct=_pct(cur["portion_sum"], cur["views"]),
            likes=int(cur["likes"]), comments=int(cur["comments"]), shares=int(cur["shares"]),
            posts=post_count,
        ),
        streak=Streak(current=s["current"], posted_today=s["posted_today"],
                      boost_active=s["current"] >= creators.STREAK_MIN,
                      next_milestone=creators.next_milestone(s["current"])),
        posts=posts,
    )
