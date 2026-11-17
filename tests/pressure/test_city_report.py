"""Admin > Kampala growth: weekly returning users for one city (Nov 2026)."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 18, 12, 0)          # a Wednesday
THIS_MON = datetime(2026, 11, 16)
LAST_MON = THIS_MON - timedelta(weeks=1)


async def _user(db, city=None, country="ug", created=None, profile_city=None):
    doc = {"username": f"u{ObjectId()}", "display_name": "U", "localization": {"country": country},
           "created_at": created or datetime(2026, 1, 1)}
    if city:
        doc["home_ground"] = {"city": city}
    if profile_city:
        doc["localization"]["city_key"] = profile_city
    return (await db.users.insert_one(doc)).inserted_id


async def _watch(db, uid, at):
    await db.watch_history.insert_one({"user_id": uid, "video_id": ObjectId(), "watched_at": at})


def _row(r, key, week):
    seg = next(s for s in r["segments"] if s["key"] == key)
    return next(x for x in seg["rows"] if x["week"] == week.strftime("%Y-%m-%d"))


async def test_CR1_returning_and_rates_per_city(db):
    from app.services import city_report
    a = await _user(db, city="kampala")
    b = await _user(db, profile_city="kampala")
    c = await _user(db, country="ug")                 # Uganda, not Kampala
    d = await _user(db, country="ke")                 # elsewhere
    for u in (a, b, c, d):
        await _watch(db, u, LAST_MON + timedelta(days=2))
    await _watch(db, a, THIS_MON + timedelta(days=1))     # a and c came back
    await _watch(db, c, THIS_MON + timedelta(days=1))
    r = await city_report.report(db, "kampala", now=NOW)
    assert r["members"] == {"city": 2, "country": 3}
    k = _row(r, "city", THIS_MON)
    assert (k["active"], k["returning"], k["return_pct"], k["partial"]) == (1, 1, 50, True)
    assert _row(r, "city", LAST_MON)["active"] == 2
    ug = _row(r, "country", THIS_MON)
    assert (ug["active"], ug["returning"], ug["return_pct"]) == (2, 2, 67)
    assert _row(r, "all", LAST_MON)["active"] == 4


async def test_CR2_new_accounts_and_posts_and_messages_count(db):
    from app.services import city_report
    n = await _user(db, city="kampala", created=LAST_MON + timedelta(days=1))
    await _watch(db, n, LAST_MON + timedelta(days=1))
    await db.videos.insert_one({"creator_id": n, "created_at": THIS_MON + timedelta(hours=5), "views_count": 40})
    m = await _user(db, city="kampala", created=LAST_MON + timedelta(days=3))       # never came back
    await db.messages.insert_one({"sender_id": str(m), "created_at": LAST_MON + timedelta(days=3)})
    r = await city_report.report(db, "kampala", now=NOW)
    last, this = _row(r, "city", LAST_MON), _row(r, "city", THIS_MON)
    assert (last["new"], last["active"]) == (2, 2)
    assert (this["new_back"], this["new_back_pct"], this["posters"], this["active"]) == (1, 50, 1, 1)
    assert [c["posts"] for c in r["creators"]] == [1] and r["creators"][0]["views"] == 40


async def test_CR3_eight_weeks_newest_last_and_nothing_from_the_future(db):
    from app.services import city_report
    u = await _user(db, city="kampala")
    await _watch(db, u, NOW + timedelta(days=1))
    await _watch(db, u, THIS_MON - timedelta(weeks=20))
    r = await city_report.report(db, "kampala", now=NOW)
    rows = r["segments"][0]["rows"]
    assert len(rows) == 8 and rows[-1]["week"] == "2026-11-16" and rows[0]["week"] == "2026-09-28"
    assert sum(x["active"] for x in rows) == 0


async def test_CR4_admin_only_and_unknown_city(client, db):
    from tests.pressure.conftest import make_user
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/city-report", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/admin/city-report?city=kampala&fresh=true", headers=ha)
    assert r.status_code == 200 and r.json()["city"]["name"] == "Kampala"
    assert (await client.get("/api/v1/admin/city-report?city=atlantis", headers=ha)).status_code == 404
