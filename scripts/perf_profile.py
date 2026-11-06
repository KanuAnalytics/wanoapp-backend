"""
Profiles the app's main endpoints against a real MongoDB with production-sized
data, using MongoDB's own profiler. For each request it reports how many
database round trips it made, how many documents/index keys the server read,
and which single operations read the most.

    WANO_REAL_MONGO=mongodb://127.0.0.1:27018 python -m scripts.perf_profile

Round trips matter as much as scans: the API and Atlas are in different data
centres, so every sequential query adds network latency to the request.
"""

import asyncio
import os
import random
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

URL = os.environ.get("WANO_REAL_MONGO", "mongodb://127.0.0.1:27018")

# Settings need values to import.
import re as _re
_cfg = open(os.path.join(os.path.dirname(__file__), "..", "app", "core", "config.py")).read()
for _k in _re.findall(r"^\s+([A-Z_][A-Z0-9_]*):\s*str\s*$", _cfg, _re.M):
    os.environ.setdefault(_k, "test")
os.environ.setdefault("SECRET_KEY", "perf-secret")

import httpx
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

N_USERS, N_VIDEOS, N_CREATORS = 9000, 10000, 600
N_WATCH, N_COMMENTS, N_NOTIFS = 20000, 15000, 20000


async def seed(db):
    rnd = random.Random(7)
    now = datetime.now(timezone.utc)
    users = []
    for i in range(N_USERS):
        u = {"_id": ObjectId(), "username": f"user{i}", "display_name": f"Person {i}",
             "is_active": True, "is_verified": True, "user_type": "standard",
             "created_at": now - timedelta(days=rnd.randint(1, 300)), "updated_at": now,
             "localization": {"country": rnd.choice(["ug", "ng", "gh", "ke"])},
             "followers": [], "following": [], "liked_videos": [], "bookmarked_videos": [],
             "blocked_users": [], "blocked_by": [], "tags": [], "expo_push_tokens": [f"ExponentPushToken[{i}]"],
             "followers_count": 0, "following_count": 0, "password_hash": "x"}
        if i % 3:
            u["email"] = f"u{i}@example.com"
        if i % 2:
            u["phone_number"] = f"+2567{i:08d}"
        users.append(u)
    creators = [u["_id"] for u in users[:N_CREATORS]]
    for u in users:
        follows = rnd.sample(creators, rnd.randint(0, 60))
        u["following"] = follows
        u["following_count"] = len(follows)
    by_id = {u["_id"]: u for u in users}
    for u in users:
        for c in u["following"]:
            by_id[c]["followers"].append(u["_id"])
    for u in users:
        u["followers_count"] = len(u["followers"])
    await db.users.insert_many(users)

    videos = []
    for i in range(N_VIDEOS):
        uid = secrets.token_hex(16)
        created = now - timedelta(minutes=rnd.randint(0, 90 * 24 * 60))
        videos.append({"_id": ObjectId(), "creator_id": rnd.choice(creators), "title": f"clip {i}",
                       "description": f"video {i} #wano", "privacy": rnd.choice(["public"] * 9 + ["private"]),
                       "is_active": rnd.random() > 0.03, "isReadyToStream": rnd.random() > 0.02,
                       "status": "published", "media_type": "video", "video_type": "bits", "images": [], "hashtags": ["wano"],
                       "comments_enabled": True, "bookmarks_count": 0, "shares_count": 0,
                       "created_at": created, "updated_at": created,
                       "views_count": rnd.randint(0, 5000), "likes_count": rnd.randint(0, 400),
                       "comments_count": rnd.randint(0, 50), "stream_uid": uid,
                       "remoteUrl_CF": f"https://videodelivery.net/{uid}/manifest/video.m3u8",
                       "urls": {"thumbnail": f"https://videodelivery.net/{uid}/thumbnails/thumbnail.jpg"}})
    await db.videos.insert_many(videos)
    vids = [v["_id"] for v in videos]
    for u in users[:3000]:
        u_l = rnd.sample(vids, rnd.randint(0, 80))
        await db.users.update_one({"_id": u["_id"]}, {"$set": {"liked_videos": u_l}})
    wh = [{"user_id": rnd.choice(users)["_id"], "video_id": rnd.choice(vids), "portion": rnd.random(),
           "watched_at": now - timedelta(minutes=rnd.randint(0, 60 * 24 * 30))} for _ in range(N_WATCH)]
    seen, uniq = set(), []
    for w in wh:
        k = (w["user_id"], w["video_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(w)
    await db.watch_history.insert_many(uniq)
    await db.comments.insert_many([{"video_id": rnd.choice(vids), "user_id": rnd.choice(users)["_id"],
                                    "content": "nice", "is_active": True, "parent_id": None, "likes_count": 0, "replies_count": 0,
                                    "created_at": now - timedelta(minutes=rnd.randint(0, 90 * 24 * 60)), "updated_at": now}
                                   for _ in range(N_COMMENTS)])
    await db.notifications.insert_many([{"recipient_id": rnd.choice(users)["_id"], "user_id": rnd.choice(users)["_id"],
                                         "type": "like", "post_id": rnd.choice(vids),
                                         "date": now - timedelta(minutes=rnd.randint(0, 60 * 24 * 30))}
                                        for _ in range(N_NOTIFS)])
    hour = now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    rows = []
    for v in rnd.sample(videos, 600):
        for h in range(0, 48, 3):
            rows.append({"_id": f"{v['_id']}:{h}", "video_id": v["_id"], "hour": hour - timedelta(hours=h),
                         "views": rnd.randint(1, 40), "finished": rnd.randint(0, 20), "likes": rnd.randint(0, 10)})
    await db.video_stats_hourly.insert_many(rows)
    return users, videos


async def main():
    client = AsyncIOMotorClient(URL)
    name = f"wano_perf_{ObjectId()}"
    db = client[name]
    from app.core import database as dm
    dm.db.db, dm.db.client = db, client
    await dm.create_indexes()
    await dm.create_comment_indexes(db)
    from app.main import app, ensure_indexes
    await ensure_indexes(db)
    print("seeding...", flush=True)
    users, videos = await seed(db)

    # Outside services off.
    import app.services.expo as expo
    for n in dir(expo):
        if n.startswith("send"):
            setattr(expo, n, lambda *a, **k: None)

    async def noop(*a, **k):
        return None
    import sys as _sys
    for mn, m in list(_sys.modules.items()):
        if mn.startswith("app.") and hasattr(m, "recombee_send"):
            setattr(m, "recombee_send", noop)

    from app.core.security import create_access_token, get_password_hash
    viewer = max(users[N_CREATORS:], key=lambda u: len(u["following"]))
    await db.users.update_one({"_id": viewer["_id"]}, {"$set": {"password_hash": get_password_hash("Password1")}})
    creator = users[0]
    h = {"Authorization": f"Bearer {create_access_token({'sub': str(viewer['_id'])})}"}
    ch = {"Authorization": f"Bearer {create_access_token({'sub': str(creator['_id'])})}"}
    a_video = next(v for v in videos if v["privacy"] == "public" and v["is_active"] and v["isReadyToStream"])
    vid = str(a_video["_id"])

    calls = [
        ("login (username)", "POST", "/api/v1/auth/login/json", None, {"username": viewer["username"], "password": "Password1"}),
        ("login (wrong user)", "POST", "/api/v1/auth/login/json", None, {"username": "nobody@example.com", "password": "Password1"}),
        ("my profile", "GET", "/api/v1/users/me", h, None),
        ("New tab (old app, skip)", "GET", "/api/v1/feed/?skip=0&limit=20", h, None),
        ("New tab (skip=2000)", "GET", "/api/v1/feed/?skip=2000&limit=20", h, None),
        ("New tab (cursor)", "GET", "/api/v1/feed/?cursor=start&limit=20&include_own=true", h, None),
        ("New bubble count", "GET", f"/api/v1/feed/new-count?since={vid}", h, None),
        ("For You", "GET", "/api/v1/feed/v2?scenario=for-you&limit=20", h, None),
        ("Trending", "GET", "/api/v1/feed/trending?limit=20", h, None),
        ("Profile grid", "GET", f"/api/v1/feed/?user_id={creator['_id']}&skip=0&limit=30", h, None),
        ("Video page", "GET", f"/api/v1/videos/{vid}", h, None),
        ("Comments", "GET", f"/api/v1/video/{vid}?skip=0&limit=20", h, None),
        ("Search users", "GET", "/api/v1/users/search?q=pers&limit=20", h, None),
        ("Notifications", "GET", "/api/v1/notifications/?limit=20", h, None),
        ("Creator stats", "GET", "/api/v1/creators/me/stats?days=7", ch, None),
        ("Watch (count a view)", "POST", f"/api/v1/videos/{vid}/watch", h, {"portion": 0.9}),
        ("Like", "POST", f"/api/v1/videos/{vid}/like", h, None),
        ("Follow", "POST", f"/api/v1/users/{creator['_id']}/follow", h, None),
    ]

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    rows = []
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for label, method, path, headers, body in calls:
            await db.command("profile", 0)
            await db.system.profile.drop()
            await db.command("profile", 2)
            t = time.perf_counter()
            r = await c.request(method, path, headers=headers or {}, json=body)
            ms = (time.perf_counter() - t) * 1000
            await db.command("profile", 0)
            ops = [p async for p in db.system.profile.find({"ns": {"$not": {"$regex": r"system\.profile$"}}})]
            ops = [p for p in ops if p.get("op") in ("query", "command", "update", "insert", "remove", "getmore")
                   and not str(p.get("command", {}).get("profile", "")).strip()]
            docs = sum(p.get("docsExamined", 0) for p in ops)
            keys = sum(p.get("keysExamined", 0) for p in ops)
            worst = max(ops, key=lambda p: p.get("docsExamined", 0), default=None)
            wdesc = ""
            if worst and worst.get("docsExamined", 0) > 200:
                cmd = worst.get("command", {})
                wdesc = f"{worst['ns'].split('.', 1)[1]}: {worst.get('docsExamined')} docs " \
                        f"({worst.get('planSummary', '?')}) {str(cmd.get('filter') or cmd.get('pipeline') or cmd.get('q') or '')[:90]}"
            rows.append((label, r.status_code, len(ops), docs, keys, ms, wdesc))
            if os.environ.get("DETAIL") and os.environ["DETAIL"] in label:
                print(f"\n--- {label}: {r.status_code} {ms:.0f} ms")
                for p in ops:
                    cmd = p.get("command", {})
                    what = cmd.get("filter") or cmd.get("pipeline") or cmd.get("q") or cmd.get("updates") or cmd
                    print(f"  {p.get('op'):8} {p['ns'].split('.', 1)[1]:22} {p.get('millis', 0):>4}ms "
                          f"docs={p.get('docsExamined', 0):<6} {str(what)[:110]}")
    print(f"\n{'endpoint':28} {'http':>4} {'trips':>5} {'docs read':>10} {'keys':>8} {'ms':>7}  worst single operation")
    for label, code, n, docs, keys, ms, w in rows:
        print(f"{label:28} {code:>4} {n:>5} {docs:>10} {keys:>8} {ms:>7.0f}  {w}")
    await client.drop_database(name)


if __name__ == "__main__":
    asyncio.run(main())
