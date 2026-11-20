"""The safety record (Nov 2026): what Wano's child-safety and abuse rules are,
since when they've been on, and what they did. Kept so that if anything goes
wrong we can show, from the database, the rule in force at the time, who
turned it on, and every comment it held back.

  safety_policies   one document per rule version (text, version, switch,
                    sources it follows); written once, never edited. A new
                    wording is a new version.
  admin_log         already records every switch change (who, when).
  comments          every held comment keeps why (held), when (held_at) and
                    which rule version (held_rule). Held comments are never
                    deleted by the system.
  comment_reports   every report and how Admin closed it.

GET /admin/safety-record puts it together; Admin > Safety record shows it.
"""

from datetime import datetime, timedelta
from typing import List

POLICIES: List[dict] = [
    {"_id": "teen_comments:v1", "switch": "teen_comments", "title": "Who can comment on teens' posts",
     "text": ("Under 16: only friends (people who follow each other) can comment on their posts, and this "
              "can't be opened to everyone. 16 and 17: only people the teen follows, by default; the teen can "
              "choose everyone in Settings. All teen posts: comments with offensive words are held. A comment "
              "that breaks this is kept visible only to its writer and never reaches the teen."),
     "follows": ["TikTok: under-16 comments friends or no one (tiktok.com/transparency/en/protecting-teens)",
                 "Instagram Teen Accounts: hidden words on by default (about.instagram.com, Sept 2024)"]},
    {"_id": "comment_guard:v1", "switch": "comment_guard", "title": "Comment safety",
     "text": ("Comments with links, from people going too fast (over 5 a minute, 20 on one post an hour, 200 a "
              "day), or between people who blocked each other are kept visible only to their writer. Three "
              "reports from accounts at least 3 days old hide a comment until Admin reviews it.")},
    {"_id": "teen_messages:v1", "switch": None, "title": "No messages or voice notes for teen accounts",
     "text": "Teen accounts can't send or receive direct messages or post Wano Voice notes (Status cards only)."},
    {"_id": "teen_place:v1", "switch": None, "title": "Teens' location",
     "text": "Profiles of under-18s show only their country, never their city."},
    {"_id": "challenge_fair:v1", "switch": "challenge_fair", "title": "Fair challenge scoring",
     "text": ("Only accounts at least 3 days old count, never the creator; one point-earning comment per person; "
              "held comments never count; admin accounts never rank; the board freezes at the end.")},
]


async def ensure(db) -> None:
    """Write any rule version not yet recorded (never changes an existing one)."""
    now = datetime.utcnow()
    for p in POLICIES:
        await db.safety_policies.update_one({"_id": p["_id"]}, {"$setOnInsert": {**p, "recorded_at": now}},
                                            upsert=True)


async def record(db, days: int = 90) -> dict:
    await ensure(db)
    since = datetime.utcnow() - timedelta(days=days)
    policies = await db.safety_policies.find({}).sort("_id", 1).to_list(200)
    switches = sorted({p["switch"] for p in policies if p.get("switch")})
    history = await db.admin_log.find({"action": "feature", "detail.key": {"$in": switches}}) \
        .sort("at", -1).limit(500).to_list(500)
    names = {u["_id"]: u.get("username") async for u in
             db.users.find({"_id": {"$in": list({h["admin"] for h in history})}}, {"username": 1})}
    held = await db.comments.aggregate([
        {"$match": {"held": {"$exists": True}, "created_at": {"$gte": since}}},
        {"$group": {"_id": "$held", "n": {"$sum": 1}}}]).to_list(50)
    reports = await db.comment_reports.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}}]).to_list(20)
    return {
        "policies": [{"id": p["_id"], "title": p.get("title"), "text": p.get("text"), "switch": p.get("switch"),
                      "follows": p.get("follows", []), "recorded_at": p.get("recorded_at")} for p in policies],
        "switch_history": [{"at": h["at"], "switch": h["detail"].get("key"), "on": h["detail"].get("on"),
                            "by": names.get(h["admin"])} for h in history],
        "days": days,
        "held_comments": {r["_id"]: r["n"] for r in held},
        "comment_reports": {r["_id"]: r["n"] for r in reports},
    }
