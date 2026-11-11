"""How fast Wano feels on real phones, by country and connection.

The app sends a few timings (in batches, once a minute or when it goes to the
background):

    first_video   app opened -> first video playing          (once per launch)
    video_start   a video came on screen -> it plays
    stall         a playing video stopped to buffer, how long
    feed_load     a feed page request, start to finish

with the connection type (wifi / cellular / unknown) and the phone's time
zone ("Africa/Lagos"), which gives the country without asking for location.

Nothing about the person is kept: each timing only adds 1 to a counter in a
daily histogram per (day, country, connection, metric). Percentiles come from
the histogram buckets.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pymongo import UpdateOne

METRICS = ("first_video", "video_start", "stall", "feed_load")
NETS = ("wifi", "cellular", "unknown")
# Bucket upper edges in ms; the last bucket is everything above.
EDGES = (100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000, 13000, 20000, 40000)
MAX_MS = 120_000
MAX_EVENTS = 60
KEEP_DAYS = 90

# Time zone -> country. Africa in full (where most of Wano's viewers are),
# plus the places the team and the diaspora are. Anything else is "other".
TZ_COUNTRY = {
    "Africa/Abidjan": "CI", "Africa/Accra": "GH", "Africa/Addis_Ababa": "ET", "Africa/Algiers": "DZ",
    "Africa/Asmara": "ER", "Africa/Bamako": "ML", "Africa/Bangui": "CF", "Africa/Banjul": "GM",
    "Africa/Bissau": "GW", "Africa/Blantyre": "MW", "Africa/Brazzaville": "CG", "Africa/Bujumbura": "BI",
    "Africa/Cairo": "EG", "Africa/Casablanca": "MA", "Africa/Ceuta": "ES", "Africa/Conakry": "GN",
    "Africa/Dakar": "SN", "Africa/Dar_es_Salaam": "TZ", "Africa/Djibouti": "DJ", "Africa/Douala": "CM",
    "Africa/El_Aaiun": "EH", "Africa/Freetown": "SL", "Africa/Gaborone": "BW", "Africa/Harare": "ZW",
    "Africa/Johannesburg": "ZA", "Africa/Juba": "SS", "Africa/Kampala": "UG", "Africa/Khartoum": "SD",
    "Africa/Kigali": "RW", "Africa/Kinshasa": "CD", "Africa/Lagos": "NG", "Africa/Libreville": "GA",
    "Africa/Lome": "TG", "Africa/Luanda": "AO", "Africa/Lubumbashi": "CD", "Africa/Lusaka": "ZM",
    "Africa/Malabo": "GQ", "Africa/Maputo": "MZ", "Africa/Maseru": "LS", "Africa/Mbabane": "SZ",
    "Africa/Mogadishu": "SO", "Africa/Monrovia": "LR", "Africa/Nairobi": "KE", "Africa/Ndjamena": "TD",
    "Africa/Niamey": "NE", "Africa/Nouakchott": "MR", "Africa/Ouagadougou": "BF", "Africa/Porto-Novo": "BJ",
    "Africa/Sao_Tome": "ST", "Africa/Tripoli": "LY", "Africa/Tunis": "TN", "Africa/Windhoek": "NA",
    "Indian/Mauritius": "MU", "Indian/Antananarivo": "MG", "Indian/Comoro": "KM", "Atlantic/Cape_Verde": "CV",
    "Indian/Mahe": "SC",
    "Europe/London": "GB", "Europe/Paris": "FR", "Europe/Brussels": "BE", "Europe/Berlin": "DE",
    "America/New_York": "US", "America/Chicago": "US", "America/Denver": "US", "America/Los_Angeles": "US",
    "America/Phoenix": "US", "America/Toronto": "CA", "America/Jamaica": "JM", "Asia/Dubai": "AE",
}


def country_of(tz: Optional[str]) -> str:
    return TZ_COUNTRY.get(tz or "", "other") if isinstance(tz, str) else "other"


def bucket(ms: float) -> int:
    for i, edge in enumerate(EDGES):
        if ms <= edge:
            return i
    return len(EDGES)


def percentile(buckets: Dict[str, int], p: float) -> Optional[int]:
    """Upper edge of the bucket holding the p-th percentile (ms)."""
    counts = [int(buckets.get(str(i), 0) or 0) for i in range(len(EDGES) + 1)]
    total = sum(counts)
    if not total:
        return None
    need, run = p * total, 0
    for i, c in enumerate(counts):
        run += c
        if run >= need:
            return EDGES[i] if i < len(EDGES) else MAX_MS
    return MAX_MS


async def record(db, events: List[dict], tz: Optional[str], net: str, now: Optional[datetime] = None) -> int:
    now = now or datetime.utcnow()
    day = now.strftime("%Y-%m-%d")
    country = country_of(tz)
    net = net if net in NETS else "unknown"
    ops = []
    for e in events[:MAX_EVENTS]:
        name, ms = e.get("name"), e.get("ms")
        if name not in METRICS or isinstance(ms, bool) or not isinstance(ms, (int, float)):
            continue
        if not (0 <= ms <= MAX_MS):
            continue
        ops.append(UpdateOne(
            {"_id": f"{day}:{country}:{net}:{name}"},
            {"$inc": {"count": 1, "sum": int(ms), f"b.{bucket(ms)}": 1},
             "$setOnInsert": {"day": now.replace(hour=0, minute=0, second=0, microsecond=0),
                              "country": country, "net": net, "metric": name}},
            upsert=True))
    if ops:
        await db.perf_daily.bulk_write(ops, ordered=False)
    return len(ops)


async def report(db, days: int = 7, now: Optional[datetime] = None) -> List[dict]:
    """Rows per (country, net, metric) over the last `days`: count, p50, p90, avg."""
    now = now or datetime.utcnow()
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    merged: Dict[tuple, dict] = {}
    async for d in db.perf_daily.find({"day": {"$gte": since}}):
        k = (d.get("country"), d.get("net"), d.get("metric"))
        m = merged.setdefault(k, {"count": 0, "sum": 0, "b": {}})
        m["count"] += int(d.get("count") or 0)
        m["sum"] += int(d.get("sum") or 0)
        for b, c in (d.get("b") or {}).items():
            m["b"][b] = m["b"].get(b, 0) + int(c or 0)
    rows = []
    for (country, net, metric), m in merged.items():
        rows.append({"country": country, "net": net, "metric": metric, "count": m["count"],
                     "avg": round(m["sum"] / m["count"]) if m["count"] else None,
                     "p50": percentile(m["b"], 0.5), "p90": percentile(m["b"], 0.9)})
    rows.sort(key=lambda r: (-r["count"], r["country"], r["metric"], r["net"]))
    return rows
