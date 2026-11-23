"""Clip it ("tv_clip_it" switch, app 2.3.63): a viewer posts 5-60 seconds of a
Wano TV film to their own feed, linked back to the film ("Watch the full film").

- Only titles whose licence allows it (Admin: "Viewers may post clips"), on
  Cloudflare Stream (Stream cuts the clip; Wano stores no copy), films only.
- At most 30 seconds, CLIPS_PER_TITLE per person per film, and never from the
  last 15% (the ending): enough to share a moment, not to rebuild the film.
- Only titles rated All or 13+: a clip is an ordinary feed post, and the feed
  has no per-post age gate, so clips of 16+/18+ films would reach younger
  viewers. Rated titles get clips once the feed can gate them.
- The same limits as posting (account limits, a birthday if required, the
  hourly/daily post limits), plus CLIPS_PER_DAY per person.
- The post says where it's from, and the film's makers keep the credit.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException

logger = logging.getLogger(__name__)

SWITCH = "tv_clip_it"
MIN_S, MAX_S = 5, 30
CLIPS_PER_DAY = 5
CLIPS_PER_TITLE = 2          # per person, ever: clips can't be stitched back into the film
NO_CLIPS_AFTER = 0.85        # the ending stays unspoiled (and off the feed)
OK_RATINGS = (0, 13)


async def make(db, me: str, title_id: str, start: float, length: float, caption: str = "") -> dict:
    from app.services import creator_push, post_limits, tv_cf, tv_more, video_posts
    await creator_push.require(db, SWITCH)
    if not ObjectId.is_valid(title_id):
        raise HTTPException(400, "Invalid title ID")
    doc = await db.tv_titles.find_one({"_id": ObjectId(title_id), "is_active": True})
    if not doc or not tv_more.allowed(doc, await tv_more.viewer_age(db, me)):
        raise HTTPException(404, "Title not available")
    if doc.get("type") == "series" or doc.get("clips_allowed") is not True \
            or not tv_cf.valid_uid(doc.get("cf_uid")) or doc.get("cf_ready") is not True:
        raise HTTPException(403, "Clips can't be posted from this title")
    if tv_more.min_age(doc.get("age_rating")) not in OK_RATINGS:
        raise HTTPException(403, "Clips can't be posted from rated titles yet")
    try:
        start, length = float(start), float(length)
    except (TypeError, ValueError):
        raise HTTPException(400, "Choose the moment to clip")
    if not (MIN_S <= length <= MAX_S) or start < 0:
        raise HTTPException(400, f"A clip is {MIN_S} to {MAX_S} seconds")
    film_s = float(doc.get("duration_minutes") or 0) * 60
    if film_s and start + length > film_s * NO_CLIPS_AFTER:
        raise HTTPException(400, "The ending can't be clipped: choose an earlier moment")
    await post_limits.check(db, me)              # staff limits, a required birthday, posting limits
    if await db.tv_user_clips.count_documents({"user": ObjectId(me), "title_id": doc["_id"]}) >= CLIPS_PER_TITLE:
        raise HTTPException(429, f"You've posted {CLIPS_PER_TITLE} clips of this film already")
    since = datetime.utcnow() - timedelta(days=1)
    if await db.tv_user_clips.count_documents({"user": ObjectId(me), "at": {"$gte": since}}) >= CLIPS_PER_DAY:
        raise HTTPException(429, f"You can post {CLIPS_PER_DAY} clips a day")
    title = (doc.get("title") or "Wano TV")[:80]
    try:
        uid = await asyncio.to_thread(tv_cf.clip, doc["cf_uid"], start, start + length, f"{doc['_id']}/{me}")
    except tv_cf.CfError as e:
        raise HTTPException(502, str(e))
    url = f"https://videodelivery.net/{uid}/manifest/video.m3u8"
    words = " ".join((caption or "").split())[:150]
    description = f"{words}\n\n🎬 From \"{title}\" on Wano TV #WanoTV".strip()
    video_id = await video_posts.create_video_post(
        me, remote_url=url, remote_url_cf=url, description=description,
        thumbnail=f"https://videodelivery.net/{uid}/thumbnails/thumbnail.jpg", duration=length,
        is_ready_to_stream=False, width=1920, height=1080, supports_landscape=True, remix_enabled=False)
    now = datetime.utcnow()
    await db.tv_user_clips.insert_one({"user": ObjectId(me), "title_id": doc["_id"], "video_id": ObjectId(video_id),
                                       "stream_uid": uid, "start": start, "length": length, "at": now})
    # "Watch the full film" on the post (tv_clips), like the clips Wano links by hand.
    await db.tv_clips.update_one({"_id": video_id}, {"$set": {"title_id": doc["_id"], "at": now, "by_viewer": True}},
                                 upsert=True)
    tv_more.clear_cache()
    return {"video_id": video_id, "processing": True}
