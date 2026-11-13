"""Pushing creators to post more. Each part has its own switch.

- picks ("picks"): Wano Picks, posts an admin features on Discover for
  PICK_DAYS. The creator gets a "wano_pick" badge (and its push) once per pick.
- road: the "Road to Wano TV" ladder shown on Achievements: Rising tier ->
  a Home Ground crown -> a Wano Pick -> featured on Wano TV (a "wano_tv"
  badge an admin gives).
- missions ("missions"): weekly tasks (Monday 00:00 UTC weeks). A finished
  mission is claimed once for points that can be spent on boosts. They are
  ledger kind "mission", never "earned", so they never count toward the cash
  pool: posting junk to finish missions can't earn money.
- stats ("creator_stats"): your views, likes, comments and shares per day over
  7 or 28 days (creator_daily: views once per viewer, own views never count),
  your best posts in that time and the hours your posts do best.
- trial posts ("trial_posts"): a post marked trial isn't announced to
  followers and isn't on the profile for others, so creators can experiment.
  It goes out to followers when they choose (or stays a normal post that was
  just never announced)."""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException, status

PICK_DAYS = 7
MAX_PICKS = 20

MISSIONS = [
    # key, title, target, points
    ("post_3", "Post 3 times this week", 3, 30),
    ("reply_1", "Reply to a post with a video", 1, 20),
    ("challenge_1", "Join a challenge", 1, 20),
    ("comment_5", "Comment on 5 other creators' posts", 5, 10),
    ("home_1", "Post for your Home Ground", 1, 10),
]
MISSION = {k: (t, n, p) for k, t, n, p in MISSIONS}

VIDEO_IN_LINK = re.compile(r"(?:videoId=|/v/|/embed/)([0-9a-fA-F]{24})")


async def on(db, key: str) -> bool:
    from app.services import features
    return (await features.current(db)).get(key) is True


async def require(db, key: str):
    if not await on(db, key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _visible() -> dict:
    from app.services.creators import _showable
    return {**_showable(), "moderation.state": {"$nin": ["held", "removed"]}}


def _week_start(now: Optional[datetime] = None) -> datetime:
    from app.services import leaderboard
    return leaderboard.week_start(now or datetime.now(timezone.utc))


# ---------------------------------------------------------------- picks

def video_id_from(text: str) -> Optional[ObjectId]:
    text = (text or "").strip()
    if ObjectId.is_valid(text) and len(text) == 24:
        return ObjectId(text)
    m = VIDEO_IN_LINK.search(text)
    return ObjectId(m.group(1)) if m else None


async def add_pick(db, admin: str, video_text: str, note: str) -> dict:
    from app.services import leaderboard
    vid = video_id_from(video_text)
    if not vid:
        raise HTTPException(status_code=422, detail="Paste a post link or post ID")
    v = await db.videos.find_one({"_id": vid, **_visible()}, {"creator_id": 1})
    if not v:
        raise HTTPException(status_code=404, detail="That post isn't public (or doesn't exist)")
    creator = await db.users.find_one({"_id": v.get("creator_id"), "is_active": True}, {"_id": 1})
    if not creator:
        raise HTTPException(status_code=404, detail="That creator's account isn't active")
    now = datetime.utcnow()
    if await db.picks.find_one({"video_id": vid, "active": True, "until": {"$gt": now}}):
        raise HTTPException(status_code=409, detail="That post is already a Wano Pick")
    doc = {"video_id": vid, "creator_id": creator["_id"], "note": (note or "").strip()[:140],
           "at": now, "until": now + timedelta(days=PICK_DAYS), "active": True, "by": ObjectId(admin)}
    r = await db.picks.insert_one(doc)
    await leaderboard._award(db, creator["_id"], "wano_pick", now, week=str(r.inserted_id), video_id=vid)
    return {"id": str(r.inserted_id)}


async def remove_pick(db, pick_id: str) -> None:
    if not ObjectId.is_valid(pick_id):
        raise HTTPException(status_code=404, detail="Not found")
    r = await db.picks.update_one({"_id": ObjectId(pick_id), "active": True}, {"$set": {"active": False}})
    if not r.matched_count:
        raise HTTPException(status_code=404, detail="Not found")


async def current_picks(db, admin: bool = False) -> List[dict]:
    from app.services import thumbnails
    now = datetime.utcnow()
    rows = await db.picks.find({"active": True, "until": {"$gt": now}}).sort("at", -1).limit(MAX_PICKS * 2).to_list(
        MAX_PICKS * 2)
    if not rows:
        return []
    vids = {v["_id"]: v async for v in db.videos.find({"_id": {"$in": [r["video_id"] for r in rows]}, **_visible()})}
    users = {u["_id"]: u async for u in db.users.find(
        {"_id": {"$in": [r["creator_id"] for r in rows]}, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1})}
    out = []
    for r in rows:
        v, u = vids.get(r["video_id"]), users.get(r["creator_id"])
        if not v or not u:
            continue
        out.append({"id": str(r["_id"]), "video_id": str(v["_id"]), "note": r.get("note") or "",
                    "thumbnail": thumbnails.best_thumbnail(v), "views_count": v.get("views_count", 0),
                    "title": v.get("title") if isinstance(v.get("title"), str) else None,
                    "until": r["until"],
                    "creator": {"id": str(u["_id"]), "username": u.get("username"),
                                "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}})
        if len(out) >= MAX_PICKS:
            break
    return out


SUBMIT_FILM_URL = "https://wanoafrica.com/tv#submit-film"

# "What we look for" (content_guide switch): the standard behind a Wano Pick and
# Wano TV, shown under the Road to Wano TV. Served from here so the Wano team
# can change the words without an app release.
GUIDE = {
    "title": "What we look for",
    "sections": [
        {"key": "pick", "title": "Wano Pick",
         "intro": "Posts the Wano team features on Discover for a week.",
         "must": ["Your own original video: no reposts, no other apps' watermarks",
                  "Follows the community rules and is safe for everyone",
                  "Labelled if it was made with AI"],
         "look_for": ["Culture: real places, people, language, food, music",
                      "A strong start in the first 3 seconds",
                      "Clear sound and a steady, well-lit picture (a phone is fine)",
                      "Captions when you speak",
                      "A complete moment or story"],
         "signals": ["More people watch it to the end than usual for you",
                     "Real conversation in the comments"]},
        {"key": "tv", "title": "Wano TV",
         "intro": "Films and series, licensed by Wano. Quality over quantity.",
         "must": ["18 or over, and ready to sign a licence agreement",
                  "You own the rights to everything in it, music included",
                  "Everyone on camera agreed to be in it"],
         "look_for": ["1080p or better, with clean sound",
                      "Subtitles in English and the original language",
                      "A real story with a beginning, middle and end"],
         "signals": []},
    ],
    "never_earns": ["Reposts and copies", "Money-making offers and \"comment interested\" posts",
                    "False health or news claims", "Political campaigning"],
    "submit": {"label": "Submit your film", "url": SUBMIT_FILM_URL},
}


async def give_wano_tv(db, admin: str, creator_text: str) -> dict:
    """The last step of the Road to Wano TV, given by an admin. Never to a
    banned or deleted account, and never to an under-18: Wano TV needs a signed
    licence agreement, which a minor can't sign."""
    from app.api.v1.admin import _find_creator
    from app.services import home_ground, leaderboard
    u = await _find_creator(db, creator_text)
    if not u or u.get("is_active") is False:
        raise HTTPException(status_code=404, detail="No account found")
    if home_ground.is_minor(u):
        raise HTTPException(status_code=409,
                            detail="Wano TV is for creators 18 and over (it needs a signed licence agreement)")
    ok = await leaderboard._award(db, u["_id"], "wano_tv", datetime.utcnow())
    return {"user_id": str(u["_id"]), "awarded": ok}


async def undo_wano_tv(db, admin: str, creator_text: str) -> dict:
    """Takes back a mistaken "On Wano TV" (the step and its badge). If its
    notification already went out, that can't be recalled."""
    from app.api.v1.admin import _find_creator
    u = await _find_creator(db, creator_text)
    if not u:
        raise HTTPException(status_code=404, detail="No account found")
    r = await db.user_badges.delete_one({"_id": f"{u['_id']}:wano_tv", "user_id": u["_id"], "kind": "wano_tv"})
    return {"user_id": str(u["_id"]), "removed": r.deleted_count == 1}


async def credited_on_wano_tv(db, u: dict) -> bool:
    """Credited on a live Wano TV title (by account or username) counts as being
    on Wano TV, without anyone having to press a button."""
    me = u.get("_id")
    if not isinstance(me, ObjectId):
        return False
    ors: List[dict] = [{"credits.people.user_id": str(me)}, {"makers.user_id": me}, {"makers.user_id": str(me)}]
    name = u.get("username")
    if isinstance(name, str) and 0 < len(name) <= 64:
        ors.append({"credits.people.username": {"$in": sorted({name, name.lower(), f"@{name}", f"@{name.lower()}"})}})
    return await db.tv_titles.find_one({"is_active": True, "$or": ors}, {"_id": 1}) is not None


async def road(db, me: ObjectId) -> List[dict]:
    kinds = {k for k in await db.user_badges.distinct("kind", {"user_id": me}) if isinstance(k, str)}
    u = await db.users.find_one({"_id": me}, {"tier": 1, "username": 1}) or {}
    tier = u.get("tier") if isinstance(u.get("tier"), str) else None
    on_tv = "wano_tv" in kinds or await credited_on_wano_tv(db, u)
    return [
        {"key": "rising", "label": "Rising creator", "hint": "Reach 1K real followers",
         "done": tier in ("rising", "star", "gold", "legend")},
        {"key": "crowned", "label": "Crowned", "hint": "Be the week's top creator in your city or country",
         "done": bool(kinds & {"crown_city", "crown_country"})},
        {"key": "picked", "label": "Wano Pick", "hint": "Get a post picked by the Wano team",
         "done": "wano_pick" in kinds},
        {"key": "wano_tv", "label": "On Wano TV", "hint": "Featured on Wano TV", "done": on_tv,
         **({"action": {"label": "Submit your film", "url": SUBMIT_FILM_URL}} if await on(db, "content_guide") else {})},
    ]


async def pick_check(db, video_text: str) -> dict:
    """The numbers behind a post before it's picked: reach, how many watched to
    the end (and how that compares with the creator's usual), conversation, and
    anything that should stop it (reports, not original, unlabelled AI, minor)."""
    from app.services import home_ground
    vid = video_id_from(video_text)
    if not vid:
        raise HTTPException(status_code=422, detail="Paste a post link or post ID")
    v = await db.videos.find_one({"_id": vid}, {"creator_id": 1, "title": 1, "description": 1, "likes_count": 1,
                                                "comments_count": 1, "shares_count": 1, "category": 1,
                                                "ai_generated": 1, "originality": 1, "is_active": 1,
                                                "privacy": 1, "created_at": 1})
    if not v:
        raise HTTPException(status_code=404, detail="No such post")
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0  # noqa: E731
    reach = await db.video_reach.find_one({"_id": vid}) or {}
    views, finished = num(reach.get("views")), num(reach.get("finished"))
    rate = round(finished / views, 3) if views else None
    # The creator's usual: median watched-to-end rate over their last 20 posts with 20+ views.
    others = [d["_id"] async for d in db.videos.find({"creator_id": v.get("creator_id"), "_id": {"$ne": vid}},
                                                     {"_id": 1}).sort("created_at", -1).limit(20)]
    rates = sorted([num(r.get("finished")) / num(r.get("views")) async for r in db.video_reach.find(
        {"_id": {"$in": others}, "views": {"$gte": 20}}, {"views": 1, "finished": 1}) if num(r.get("views"))])
    usual = round(rates[len(rates) // 2], 3) if rates else None
    reports = {r["_id"]: r["n"] async for r in db.reports.aggregate([
        {"$match": {"video_id": vid, "is_active": True}},
        {"$group": {"_id": "$category", "n": {"$sum": 1}}}])}
    creator = await db.users.find_one({"_id": v.get("creator_id")},
                                      {"username": 1, "is_active": 1, "date_of_birth": 1}) or {}
    warnings = []
    if v.get("is_active") is False or v.get("privacy") not in (None, "public"):
        warnings.append("Not public")
    if creator.get("is_active") is False:
        warnings.append("Creator's account isn't active")
    if creator and home_ground.is_minor(creator):
        warnings.append("Creator is under 18")
    if isinstance(v.get("originality"), dict) and v["originality"].get("state") == "rejected":
        warnings.append("Marked not original")
    for k, label in (("intellectual_property", "Reported as a copy"), ("spam", "Reported as spam"),
                     ("false_information", "Reported as false information")):
        if reports.get(k):
            warnings.append(f"{label} ({reports[k]})")
    return {"video_id": str(vid), "creator": creator.get("username"),
            "title": v.get("title") if isinstance(v.get("title"), str) else None,
            "category": v.get("category") if isinstance(v.get("category"), str) else None,
            "ai_label": v.get("ai_generated") is True,
            "views": views, "watched_to_end": finished, "watched_to_end_rate": rate,
            "creators_usual_rate": usual, "likes": num(v.get("likes_count")),
            "comments": num(v.get("comments_count")), "shares": num(v.get("shares_count")),
            "reports": sum(reports.values()), "warnings": warnings}


# ---------------------------------------------------------------- missions

async def _live_challenge_tags(db, start: datetime, end: datetime) -> List[str]:
    return [c["hashtag"] async for c in db.challenges.find(
        {"starts_at": {"$lt": end}, "ends_at": {"$gt": start}}, {"hashtag": 1}) if isinstance(c.get("hashtag"), str)]


async def mission_progress(db, me: ObjectId, now: Optional[datetime] = None) -> List[dict]:
    from app.services import home_ground
    start = _week_start(now)
    end = start + timedelta(days=7)
    posts = await db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": start, "$lt": end}},
                                 {"reply_to": 1, "hashtags": 1}).to_list(500)
    tags = {t.casefold().lstrip("#") for t in await _live_challenge_tags(db, start, end)}
    in_challenge = sum(1 for p in posts if any(isinstance(h, str) and h.casefold().lstrip("#") in tags
                                               for h in (p.get("hashtags") or [])))
    commented = await db.comments.distinct("video_id", {"user_id": me, "created_at": {"$gte": start, "$lt": end}})
    others = await db.videos.count_documents({"_id": {"$in": commented[:200]}, "creator_id": {"$ne": me}}) \
        if commented else 0
    u = await db.users.find_one({"_id": me}, {"home_ground": 1}) or {}
    home_on = await on(db, "home_ground")
    have = {"post_3": len(posts), "reply_1": sum(1 for p in posts if isinstance(p.get("reply_to"), dict)),
            "challenge_1": in_challenge, "comment_5": others,
            "home_1": len(posts) if home_ground.city_of(u) else 0}
    week = start.strftime("%Y-%m-%d")
    claimed = set(await db.points_ledger.distinct("mission", {"user_id": me, "kind": "mission", "week": week}))
    out = []
    for key, title, target, pts in MISSIONS:
        if key == "home_1" and not home_on:
            continue
        out.append({"key": key, "title": title, "target": target, "points": pts,
                    "progress": min(have[key], target), "done": have[key] >= target, "claimed": key in claimed})
    return out


async def claim(db, me: ObjectId, key: str) -> dict:
    from app.services import points
    if key not in MISSION:
        raise HTTPException(status_code=404, detail="No such mission")
    rows = {m["key"]: m for m in await mission_progress(db, me)}
    m = rows.get(key)
    if not m:
        raise HTTPException(status_code=404, detail="No such mission")
    if not m["done"]:
        raise HTTPException(status_code=400, detail="Finish the mission first")
    week = _week_start().strftime("%Y-%m-%d")
    now = datetime.utcnow()
    if not await points._ledger(db, f"mission:{me}:{week}:{key}", me, "mission", m["points"], now,
                                mission=key, week=week):
        raise HTTPException(status_code=409, detail="Already claimed")
    await points._wallet(db, me, m["points"])
    return {"points": m["points"]}


# ---------------------------------------------------------------- stats

async def stats(db, me: ObjectId, days: int) -> dict:
    from app.services import creators, growth_notifications as g
    today = creators._day(datetime.now(timezone.utc))
    start = today - timedelta(days=days - 1)
    by_day: Dict[datetime, dict] = {}
    async for r in db.creator_daily.find({"creator_id": me, "day": {"$gte": start, "$lte": today}}):
        d = r.get("day")
        if not isinstance(d, datetime):
            continue
        row = by_day.setdefault(d, {"views": 0, "likes": 0, "comments": 0, "shares": 0})
        for k in row:
            v = r.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf"):
                row[k] += max(0, int(v))
    series, totals = [], {"views": 0, "likes": 0, "comments": 0, "shares": 0}
    for i in range(days):
        d = start + timedelta(days=i)
        row = by_day.get(d, {"views": 0, "likes": 0, "comments": 0, "shares": 0})
        series.append({"day": d.strftime("%Y-%m-%d"), **row})
        for k in totals:
            totals[k] += row[k]
    posts = await db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": start}},
                                 {"title": 1, "description": 1, "views_count": 1, "likes_count": 1,
                                  "created_at": 1, "urls": 1, "thumbnail": 1, "remoteUrl_CF": 1}
                                 ).sort("views_count", -1).limit(3).to_list(3)
    from app.services import thumbnails
    best = [{"id": str(p["_id"]), "title": (p.get("title") or p.get("description") or "")[:80]
             if isinstance(p.get("title") or p.get("description") or "", str) else "",
             "views": int(p.get("views_count") or 0) if isinstance(p.get("views_count"), (int, float)) else 0,
             "thumbnail": thumbnails.best_thumbnail(p)} for p in posts]
    # When your posts do best: average views by the local hour they went up (last 28 days).
    user = await db.users.find_one({"_id": me}, {"localization": 1}) or {}
    offset = g._offset(user)
    hours: Dict[int, list] = {}
    async for p in db.videos.find({"creator_id": me, **_visible(),
                                   "created_at": {"$gte": today - timedelta(days=27)}},
                                  {"created_at": 1, "views_count": 1}):
        c, v = p.get("created_at"), p.get("views_count")
        if isinstance(c, datetime) and isinstance(v, (int, float)) and not isinstance(v, bool):
            hours.setdefault(((c.replace(tzinfo=None) + offset).hour // 3) * 3, []).append(max(0, v))
    best_hours = None
    if sum(len(x) for x in hours.values()) >= 5:
        h = max(hours, key=lambda k: (sum(hours[k]) / len(hours[k]), len(hours[k])))
        best_hours = {"from": h, "to": (h + 3) % 24}
    posts_month = await _posts_month(db, me, offset)
    return {"days": days, "totals": totals, "series": series, "best_posts": best, "best_hours": best_hours,
            "posts_month": posts_month,
            "followers": int((await db.users.find_one({"_id": me}, {"followers_count": 1}) or {})
                             .get("followers_count") or 0)}


async def _posts_month(db, me: ObjectId, offset: timedelta) -> dict:
    """Posts you put up this calendar month (your local time), on how many
    different days, and last month's count to compare. Only posts people can
    see count: private, removed, held and still-processing ones don't."""
    local_now = datetime.now(timezone.utc).replace(tzinfo=None) + offset
    this_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_start = (this_start - timedelta(days=1)).replace(day=1)
    # Back to UTC for the query (created_at is stored in UTC).
    q_from = last_start - offset
    count = last = 0
    days = set()
    async for p in db.videos.find({"creator_id": me, **_visible(), "created_at": {"$gte": q_from}},
                                  {"created_at": 1}):
        c = p.get("created_at")
        if not isinstance(c, datetime):
            continue
        local = c.replace(tzinfo=None) + offset
        if local >= this_start:
            if local <= local_now + timedelta(minutes=5):
                count += 1
                days.add(local.date())
        elif local >= last_start:
            last += 1
    return {"month": this_start.strftime("%Y-%m"), "count": count, "days_posted": len(days),
            "last_month": last}


# ---------------------------------------------------------------- trial posts

async def share_trial(db, me: ObjectId, video_id: str) -> dict:
    if not ObjectId.is_valid(video_id):
        raise HTTPException(status_code=404, detail="Post not found")
    now = datetime.utcnow()
    r = await db.videos.update_one({"_id": ObjectId(video_id), "creator_id": me, "trial": True},
                                   {"$set": {"trial": False, "trial_shared_at": now}})
    if not r.matched_count:
        raise HTTPException(status_code=404, detail="Post not found")
    return {"shared": True}


async def my_trials(db, me: ObjectId) -> List[dict]:
    from app.services import thumbnails
    rows = await db.videos.find({"creator_id": me, "trial": True, "is_active": True}).sort("created_at", -1) \
        .limit(30).to_list(30)
    return [{"id": str(v["_id"]), "title": (v.get("title") or v.get("description") or "")[:80]
             if isinstance(v.get("title") or v.get("description") or "", str) else "",
             "thumbnail": thumbnails.best_thumbnail(v),
             "views_count": v.get("views_count", 0) if isinstance(v.get("views_count"), int) else 0,
             "created_at": v.get("created_at")} for v in rows]
