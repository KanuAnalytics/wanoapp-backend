"""Counting a share (Nov 2026).

Until 2.3.45 the app never told the server about a share, so shares_count,
the share weight in Trending (x8), creator stats, the leaderboard and
Recombee's strongest signal (a "purchase") were all zero. Now a share is
counted when someone shares a post outside Wano (POST /videos/{id}/share) or
sends it to someone in a chat.

The count goes up once per person per post (the same engagement_once mark
Trending uses), never for the creator's own post, so sharing one post ten
times doesn't inflate it. Recombee always hears it (it keeps one per time).
"""

from typing import Optional

from bson import ObjectId


async def count_share(db, video_id: str, actor: Optional[str], creator=None,
                      recomm_id: Optional[str] = None) -> bool:
    """True when this share counted (first time this person shared this post)."""
    from app.services import recommendations, trending
    if not actor or not ObjectId.is_valid(str(video_id)):
        return False
    vid = str(video_id)
    if creator is None:
        v = await db.videos.find_one({"_id": ObjectId(vid)}, {"creator_id": 1})
        creator = (v or {}).get("creator_id")
    counted = False
    if creator is None or str(creator) != str(actor):
        mark = await db.engagement_once.find_one({"_id": f"share:{actor}:{vid}"}, {"_id": 1})
        if not mark:
            await trending.record_once(db, vid, actor=actor, creator=creator, kind="share", shares=1)
            await db.videos.update_one({"_id": ObjectId(vid)}, {"$inc": {"shares_count": 1}})
            counted = True
            v = await db.videos.find_one({"_id": ObjectId(vid)}, {"challenge_id": 1, "creator_id": 1})
            if (v or {}).get("challenge_id"):
                from app.services import challenge_fair
                await challenge_fair.note_share(db, v, str(actor))
    await recommendations.report_share(actor, vid, recomm_id)
    return counted
