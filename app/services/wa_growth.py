"""WhatsApp as Wano's word-of-mouth channel ("wa_share" switch, app 2.3.62).

No paid ads: people forward Wano videos into family groups and Status. This
counts the three steps so Admin > WhatsApp shows what spreads:

1. Shared to WhatsApp: the app's WhatsApp button (app 2.3.62) sends the link
   with ?s=wa and tells the server (POST /videos/{id}/share, to="whatsapp").
2. Link opened: share pages carry a 1x1 picture (/s/open.gif) that browsers
   load and link-preview robots don't; it says which post, from where (s=wa,
   or "link" for any other share) and the phone type. Once per phone per post
   per day; nothing personal is kept (a hash, deleted after two days).
3. Installed from a link (Android only: Play's install referrer, which iPhone
   doesn't have): app 2.3.62's first launch reports the referrer's source,
   once per device.

Switch off: nothing is recorded and share pages carry no picture.
"""

import hashlib
import re
from datetime import datetime, timedelta
from typing import Optional

from bson import ObjectId

SOURCES = ("wa", "link")            # wa = sent with the WhatsApp button
KINDS = ("v", "u", "tv", "c", "app")
PLATFORMS = ("android", "ios", "other")
SHARE_TO = ("whatsapp", "other")
SEEN_DAYS = 2
_SRC = re.compile(r"^[a-z]{1,8}$")


def clean_source(value) -> str:
    v = value.strip().lower() if isinstance(value, str) else ""
    return v if v in SOURCES else "link"


async def on(db) -> bool:
    from app.services import creator_push
    return await creator_push.on(db, "wa_share")


def _day(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


async def _once(db, key: str, now: datetime) -> bool:
    """First time today for this key (stored as a hash, kept SEEN_DAYS)."""
    h = hashlib.sha256(key.encode()).hexdigest()[:32]
    try:
        await db.wa_seen.insert_one({"_id": h, "at": now})
    except Exception:
        return False
    try:
        await db.wa_seen.create_index("at", expireAfterSeconds=SEEN_DAYS * 86400)
    except Exception:
        pass
    return True


async def shared(db, video_id: str, actor: Optional[str], to: Optional[str],
                 now: Optional[datetime] = None) -> bool:
    """Someone shared a post from the app (to WhatsApp, or elsewhere)."""
    if to not in SHARE_TO or not ObjectId.is_valid(str(video_id)) or not await on(db):
        return False
    now = now or datetime.utcnow()
    day = _day(now)
    await db.wa_daily.update_one({"_id": f"{day}:share:{to}"},
                                 {"$inc": {"n": 1}, "$setOnInsert": {"day": day, "step": "share", "src": to}},
                                 upsert=True)
    if to == "whatsapp":
        await db.wa_posts.update_one({"_id": f"{day}:{video_id}"},
                                     {"$inc": {"shares": 1}, "$setOnInsert": {"day": day, "video_id": str(video_id)}},
                                     upsert=True)
    return True


async def opened(db, kind: str, item: str, src: str, platform: str, who: str,
                 now: Optional[datetime] = None) -> bool:
    """A share page was opened in a browser (the picture loaded)."""
    if kind not in KINDS or (item and not re.fullmatch(r"[0-9a-f]{24}", item)):
        return False
    if not await on(db):
        return False
    if kind == "v" and item:            # made-up ids would fill the table with junk
        from bson import ObjectId
        if not await db.videos.find_one({"_id": ObjectId(item)}, {"_id": 1}):
            return False
    now = now or datetime.utcnow()
    src = clean_source(src)
    platform = platform if platform in PLATFORMS else "other"
    day = _day(now)
    if not await _once(db, f"open|{who}|{kind}|{item}|{day}", now):
        return False
    await db.wa_daily.update_one({"_id": f"{day}:open:{src}:{platform}"},
                                 {"$inc": {"n": 1}, "$setOnInsert": {"day": day, "step": "open", "src": src,
                                                                     "platform": platform}},
                                 upsert=True)
    if kind == "v" and item and src == "wa":
        await db.wa_posts.update_one({"_id": f"{day}:{item}"},
                                     {"$inc": {"opens": 1}, "$setOnInsert": {"day": day, "video_id": item}},
                                     upsert=True)
    return True


def _from_referrer(referrer: str) -> Optional[dict]:
    from urllib.parse import parse_qs
    if not isinstance(referrer, str) or len(referrer) > 1000:
        return None
    q = parse_qs(referrer)
    if (q.get("utm_source") or [""])[0] != "wano_share":
        return None
    return {"src": clean_source((q.get("utm_content") or ["link"])[0]),
            "kind": (q.get("utm_medium") or ["app"])[0][:10]}


INSTALLS_PER_NETWORK_DAY = 30


async def _network_ok(db, network: str, now: datetime) -> bool:
    """A script inventing device ids can't fill the table: at most
    INSTALLS_PER_NETWORK_DAY counted installs per connection per day."""
    if not network:
        return True
    h = hashlib.sha256(f"net|{network}|{_day(now)}".encode()).hexdigest()[:32]
    d = await db.wa_seen.find_one_and_update({"_id": h}, {"$inc": {"n": 1}, "$setOnInsert": {"at": now}},
                                             upsert=True, return_document=True)
    return int((d or {}).get("n") or 0) <= INSTALLS_PER_NETWORK_DAY


async def installed(db, device: str, referrer: str, now: Optional[datetime] = None, network: str = "") -> bool:
    """App 2.3.62's first launch on Android: was it installed from a shared link?"""
    if not device or len(device) > 100 or not await on(db):
        return False
    ref = _from_referrer(referrer)
    if not ref:
        return False
    now = now or datetime.utcnow()
    if not await _network_ok(db, network[:100], now):
        return False
    h = hashlib.sha256(f"install|{device}".encode()).hexdigest()[:32]
    try:
        await db.wa_installs.insert_one({"_id": h, "at": now, "src": ref["src"], "kind": ref["kind"]})
    except Exception:
        return False               # this device already counted
    day = _day(now)
    await db.wa_daily.update_one({"_id": f"{day}:install:{ref['src']}"},
                                 {"$inc": {"n": 1}, "$setOnInsert": {"day": day, "step": "install", "src": ref["src"]}},
                                 upsert=True)
    return True


async def stats(db, days: int = 28, now: Optional[datetime] = None) -> dict:
    """Admin > WhatsApp: weekly steps, and the posts most sent to WhatsApp."""
    now = now or datetime.utcnow()
    days = max(7, min(int(days or 28), 91))
    since = _day(now - timedelta(days=days - 1))
    weeks: dict = {}
    async for d in db.wa_daily.find({"day": {"$gte": since}}):
        dt = datetime.strptime(d["day"], "%Y-%m-%d")
        wk = _day(dt - timedelta(days=dt.weekday()))
        w = weeks.setdefault(wk, {"week": wk, "shared_wa": 0, "shared_other": 0, "opens_wa": 0, "opens_link": 0,
                                  "opens_android": 0, "opens_ios": 0, "installs_wa": 0, "installs_link": 0})
        n = int(d.get("n") or 0)
        if d.get("step") == "share":
            w["shared_wa" if d.get("src") == "whatsapp" else "shared_other"] += n
        elif d.get("step") == "open":
            w["opens_wa" if d.get("src") == "wa" else "opens_link"] += n
            if d.get("platform") in ("android", "ios"):
                w[f"opens_{d['platform']}"] += n
        elif d.get("step") == "install":
            w["installs_wa" if d.get("src") == "wa" else "installs_link"] += n
    top = []
    async for p in db.wa_posts.aggregate([
        {"$match": {"day": {"$gte": since}}},
        {"$group": {"_id": "$video_id", "shares": {"$sum": {"$ifNull": ["$shares", 0]}},
                    "opens": {"$sum": {"$ifNull": ["$opens", 0]}}}},
        {"$sort": {"shares": -1, "opens": -1}},
        {"$limit": 15},
    ]):
        top.append({"video_id": p["_id"], "shares": p["shares"], "opens": p["opens"]})
    ids = [ObjectId(t["video_id"]) for t in top if ObjectId.is_valid(t["video_id"])]
    if ids:
        info = {str(v["_id"]): v async for v in db.videos.find({"_id": {"$in": ids}},
                                                                {"title": 1, "description": 1, "creator_id": 1})}
        creators = {str(u["_id"]): u async for u in db.users.find(
            {"_id": {"$in": list({v.get("creator_id") for v in info.values() if v.get("creator_id")})}},
            {"username": 1})}
        for t in top:
            v = info.get(t["video_id"]) or {}
            t["title"] = (v.get("title") or v.get("description") or "")[:80]
            t["creator"] = (creators.get(str(v.get("creator_id"))) or {}).get("username") or ""
    return {"days": days, "on": await on(db), "weeks": sorted(weeks.values(), key=lambda w: w["week"]), "top": top}
