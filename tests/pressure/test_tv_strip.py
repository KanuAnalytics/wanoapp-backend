"""Wano TV strip (tv_strip): the ticket that points feed viewers at Wano TV.
Attacks: off means nothing at all, nothing broken or foreign is shown, counts
can't be pumped, and only admins see them."""

from datetime import date, datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

T = "/api/v1/tv"
CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"


async def _on(db):
    await db.config.insert_one({"features": {"tv_strip": True}})


async def _title(db, title="Yaoundé Police Criminelle", type_="series", featured=True, days_ago=7, **extra):
    doc = {"title": title, "type": type_, "featured": featured, "is_active": True,
           "release_date": (date.today() - timedelta(days=days_ago)).isoformat(),
           "poster": f"{CDN}/wano-tv/{title[:5]}/poster.jpg", **extra}
    return str((await db.tv_titles.insert_one(doc)).inserted_id)


async def _cfg(client, h=None):
    return await client.get(f"{T}/strip", headers=h or {}, params={} if h else {"authenticated": "false"})


async def test_TS1_off_by_default_everything_closed(client, db):
    tid = await _title(db)
    assert (await _cfg(client)).status_code == 404
    r = await client.post(f"{T}/strip/event", json={"title_id": tid, "kind": "shown"}, params={"authenticated": "false"})
    assert r.status_code == 404
    assert await db.tv_strip_daily.count_documents({}) == 0


async def test_TS2_config_titles_creators_rules(client, db):
    await _on(db)
    new = await _title(db, "New Series", days_ago=3)
    old = await _title(db, "Old Film", type_="movie", featured=False, days_ago=400)
    await _title(db, "Pulled", is_active=False)
    await _title(db, "No poster", poster=None)
    await _title(db, "Bad poster", poster="http://evil.com/x.jpg")
    house, _ = await make_user(db, username="diasporastories", house_account=True)
    await make_user(db, username="banned_house", house_account=True, is_active=False)
    await make_user(db, username="normal")
    d = (await _cfg(client)).json()
    assert [t["title"] for t in d["titles"]] == ["New Series", "Old Film"]
    t0, t1 = d["titles"]
    assert t0["kicker"] == "WANO TV · NEW SERIES" and t1["kicker"] == "WANO TV · FILM"
    assert t0["poster_small"] == f"{CDN}/small/w160/wano-tv/New S/poster.webp"
    assert t0["id"] == new and t1["id"] == old
    assert d["creators"] == [house]
    assert d["rules"]["min_posts"] == 5 and d["rules"]["max_per_session"] == 2 and d["rules"]["show_seconds"] == 6


async def test_TS3_at_most_four_titles_featured_first_then_newest(client, db):
    await _on(db)
    for i in range(6):
        await _title(db, f"Film {i}", featured=(i == 0), days_ago=i * 10)
    titles = [t["title"] for t in (await _cfg(client)).json()["titles"]]
    assert titles == ["Film 0", "Film 1", "Film 2", "Film 3"]


async def test_TS4_events_counted_and_shown_to_admins_only(client, db):
    await _on(db)
    tid = await _title(db)
    _, uh = await make_user(db)
    for kind in ("shown", "shown", "shown", "shown", "tap", "close"):
        r = await client.post(f"{T}/strip/event", json={"title_id": tid, "kind": kind}, headers=uh)
        assert r.status_code == 200 and r.json()["counted"] is True
    _, ah = await make_user(db, user_type="admin")
    rows = (await client.get("/api/v1/admin/tv-strip", headers=ah)).json()["items"]
    assert rows == [{"title_id": tid, "title": "Yaoundé Police Criminelle", "shown": 4, "tap": 1, "close": 1,
                     "tap_rate": 0.25, "close_rate": 0.25}]
    assert (await client.get("/api/v1/admin/tv-strip", headers=uh)).status_code == 403


async def test_TS5_junk_events_refused_or_ignored(client, db):
    await _on(db)
    tid = await _title(db)
    pulled = await _title(db, "Gone", is_active=False)
    _, uh = await make_user(db)
    for bad in ({"title_id": tid, "kind": "view"}, {"title_id": tid}, {"kind": "tap"},
                {"title_id": tid, "kind": "tap", "extra": 1}, {"title_id": ["x"], "kind": "tap"}):
        assert (await client.post(f"{T}/strip/event", json=bad, headers=uh)).status_code == 422, bad
    for tid_bad in ("nope", str(ObjectId()), pulled, "$where"):
        r = await client.post(f"{T}/strip/event", json={"title_id": tid_bad, "kind": "tap"}, headers=uh)
        assert r.status_code == 200 and r.json()["counted"] is False
    assert await db.tv_strip_daily.count_documents({}) == 0


async def test_TS6_one_person_cant_pump_the_numbers(client, db):
    from app.services import tv_strip
    await _on(db)
    tid = await _title(db)
    _, uh = await make_user(db)
    counted = 0
    for _ in range(tv_strip.EVENTS_PER_DAY + 25):
        counted += (await client.post(f"{T}/strip/event", json={"title_id": tid, "kind": "tap"}, headers=uh)).json()["counted"]
    assert counted == tv_strip.EVENTS_PER_DAY
    # Logged out: capped per address too.
    n = 0
    for _ in range(tv_strip.EVENTS_PER_DAY + 5):
        r = await client.post(f"{T}/strip/event", json={"title_id": tid, "kind": "shown"},
                              params={"authenticated": "false"})
        n += r.json()["counted"]
    assert n == tv_strip.EVENTS_PER_DAY


async def test_TS7_broken_catalogue_never_breaks_it(client, db):
    await _on(db)
    await db.tv_titles.insert_many([
        {"is_active": True, "title": None, "poster": f"{CDN}/a.jpg"},
        {"is_active": True, "title": "  ", "poster": f"{CDN}/a.jpg"},
        {"is_active": True, "title": "Weird date", "poster": f"{CDN}/a.jpg", "release_date": "not-a-date"},
        {"is_active": True, "title": "Dict date", "poster": f"{CDN}/a.jpg", "release_date": {"$gt": 1}},
        {"is_active": True, "title": ["list"], "poster": f"{CDN}/a.jpg"},
    ])
    r = await _cfg(client)
    assert r.status_code == 200
    assert sorted(t["title"] for t in r.json()["titles"]) == ["Dict date", "Weird date"]
    assert all(t["kicker"] == "WANO TV · FILM" for t in r.json()["titles"])


async def test_TS8_stats_window(client, db):
    from app.services import tv_strip
    await _on(db)
    tid = await _title(db)
    now = datetime.utcnow()
    await tv_strip.record(db, "u1", tid, "shown", now=now - timedelta(days=10))
    await tv_strip.record(db, "u1", tid, "shown", now=now)
    assert (await tv_strip.stats(db, 7, now=now))[0]["shown"] == 1
    assert (await tv_strip.stats(db, 30, now=now))[0]["shown"] == 2
