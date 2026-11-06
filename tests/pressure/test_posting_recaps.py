"""Attacks on the weekly / monthly posting recap ("Last week you posted 5 videos")."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401
from tests.pressure.test_growth_notifications import _sent

pytestmark = pytest.mark.asyncio

UTC = timezone.utc
# Monday 9 Nov 2026, 10:00 in Kampala (UTC+3) = 07:00 UTC. "Last week" there is
# Mon 2 Nov 00:00 - Sun 8 Nov 24:00 Kampala time = 1 Nov 21:00 - 8 Nov 21:00 UTC.
MON = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    from app.services import growth_notifications as g
    state = {"now": MON}
    monkeypatch.setattr(g, "_utcnow", lambda: state["now"])
    g.set_started(MON - timedelta(days=60))
    return state


async def _creator(db, country="ug", tokens=("tok",), **extra):
    uid, h = await make_user(db, created_at=MON - timedelta(days=90), localization={"country": country},
                             expo_push_tokens=list(tokens), **extra)
    return uid, h


async def _posts(db, c, times, **extra):
    for t in times:
        await make_video(db, c, created_at=t, **{"isReadyToStream": True, **extra})


async def _run(db):
    from app.services import growth_notifications as g
    return await g.posting_recaps(db)


def _bodies(calls):
    return [s[2] for s in _sent(calls)]


async def test_W1_weekly_recap_counts_last_week_in_the_creators_own_time(client, db, rb, calls, clock):
    c, _ = await _creator(db)
    await _posts(db, c, [
        datetime(2026, 11, 1, 20, 30, tzinfo=UTC),   # Sun 1 Nov 23:30 Kampala: week before
        datetime(2026, 11, 1, 21, 30, tzinfo=UTC),   # Mon 2 Nov 00:30 Kampala: last week
        datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 6, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 8, 20, 30, tzinfo=UTC),   # Sun 8 Nov 23:30 Kampala: last week
        datetime(2026, 11, 8, 21, 30, tzinfo=UTC),   # Mon 9 Nov 00:30 Kampala: this week
    ])
    await db.creator_daily.insert_many([
        {"_id": f"{c}:{d}", "creator_id": ObjectId(c), "day": datetime(2026, 11, d), "views": 300}
        for d in range(2, 9)])
    r = await _run(db)
    [body] = _bodies(calls)
    assert "4 videos" in body and "3 more than the week before" in body and "2,100 views" in body, body
    assert r["weekly"] == 1


async def test_W2_only_once_per_week(client, db, rb, calls, clock):
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    for minutes in (0, 30, 60, 90):
        clock["now"] = MON + timedelta(minutes=minutes)
        await _run(db)
    assert len(_sent(calls)) == 1


@pytest.mark.parametrize("when", [
    datetime(2026, 11, 9, 5, 30, tzinfo=UTC),     # Monday 08:30 Kampala: too early
    datetime(2026, 11, 9, 9, 30, tzinfo=UTC),     # Monday 12:30 Kampala: too late
    datetime(2026, 11, 10, 7, 0, tzinfo=UTC),     # Tuesday
])
async def test_W3_only_monday_morning_local(client, db, rb, calls, clock, when):
    clock["now"] = when
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    await _run(db)
    assert _sent(calls) == []


async def test_W3_each_country_on_its_own_monday_morning(client, db, rb, calls, clock):
    ug, _ = await _creator(db, "ug", tokens=("ug",))
    gh, _ = await _creator(db, "gh", tokens=("gh",))
    for c in (ug, gh):
        await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    clock["now"] = datetime(2026, 11, 9, 7, 0, tzinfo=UTC)      # 10:00 Kampala, 07:00 Accra
    await _run(db)
    clock["now"] = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)     # 13:00 Kampala, 10:00 Accra
    await _run(db)
    assert [s[0] for s in _sent(calls)] == ["ug", "gh"]


async def test_W4_quiet_week_after_a_busy_one_gets_a_nudge_nobody_else(client, db, rb, calls, clock):
    lapsed, _ = await _creator(db, tokens=("lapsed",))
    await _posts(db, lapsed, [datetime(2026, 10, 28, 9, 0, tzinfo=UTC)])     # week before last only
    never, _ = await _creator(db, tokens=("never",))
    await _run(db)
    sent = _sent(calls)
    assert [s[0] for s in sent] == ["lapsed"] and "didn't post" in sent[0][2]


async def test_W5_only_real_posts_count(client, db, rb, calls, clock):
    c, _ = await _creator(db)
    t = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    await _posts(db, c, [t, t])                                          # 2 real
    await _posts(db, c, [t], privacy="unlisted")                         # 1 real (shared by link)
    await _posts(db, c, [t], privacy="private")
    await _posts(db, c, [t], is_active=False)
    await _posts(db, c, [t], status="scheduled", is_active=False)
    await _posts(db, c, [t], isReadyToStream=False)
    await _run(db)
    [body] = _bodies(calls)
    assert "3 videos" in body, body


async def test_W6_one_video_reads_right_and_message_is_short(client, db, rb, calls, clock):
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    await _run(db)
    [(tok, title, body, data)] = _sent(calls)
    assert "1 video " in body or "1 video," in body or body.count("1 video") == 1
    assert "1 videos" not in body and len(body) <= 178 and len(title) <= 65
    assert data.get("type") == "weekly_recap"


async def test_M1_monthly_recap_on_the_first(client, db, rb, calls, clock):
    clock["now"] = datetime(2026, 12, 1, 7, 0, tzinfo=UTC)               # Tue 1 Dec, 10:00 Kampala
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, d, 9, 0, tzinfo=UTC) for d in (2, 5, 9, 14, 20, 29)])
    await _posts(db, c, [datetime(2026, 10, d, 9, 0, tzinfo=UTC) for d in (3, 8)])
    await _posts(db, c, [datetime(2026, 11, 30, 21, 30, tzinfo=UTC)])    # 1 Dec 00:30 Kampala: December
    await db.creator_daily.insert_one({"_id": f"{c}:x", "creator_id": ObjectId(c),
                                       "day": datetime(2026, 11, 15), "views": 12400})
    r = await _run(db)
    [body] = _bodies(calls)
    assert "November" in body and "6 videos" in body and "12.4k views" in body, body
    assert r["monthly"] == 1


async def test_M2_monday_the_first_sends_only_the_monthly(client, db, rb, calls, clock):
    clock["now"] = datetime(2027, 2, 1, 7, 0, tzinfo=UTC)                # Mon 1 Feb 2027, 10:00 Kampala
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2027, 1, 28, 9, 0, tzinfo=UTC)])
    await _run(db)
    await _run(db)
    bodies = _bodies(calls)
    assert len(bodies) == 1 and "January" in bodies[0]


async def test_R1_quiet_opt_out_no_tokens_inactive(client, db, rb, calls, clock):
    t = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
    off, oh = await _creator(db, tokens=("off",))
    await client.put("/api/v1/notifications/preferences", headers=oh, json={"creator_tips": False})
    silent, _ = await _creator(db, tokens=())
    gone, _ = await _creator(db, tokens=("gone",), is_active=False)
    for c in (off, silent, gone):
        await _posts(db, c, [t])
    await _run(db)
    assert _sent(calls) == []


async def test_R2_stats_show_weekly_and_monthly_counts(client, db, rb, calls, monkeypatch):
    from app.api.v1 import creators as api
    monkeypatch.setattr(api, "_utcnow", lambda: datetime(2026, 11, 11, 9, 0, tzinfo=UTC))   # Wed 11 Nov
    c, h = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, 9, 9, 0, tzinfo=UTC), datetime(2026, 11, 10, 9, 0, tzinfo=UTC)])
    await _posts(db, c, [datetime(2026, 11, 3, 9, 0, tzinfo=UTC)])
    await _posts(db, c, [datetime(2026, 10, 20, 9, 0, tzinfo=UTC)])
    r = await client.get("/api/v1/creators/me/stats", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["posting"] == {"this_week": 2, "last_week": 1, "this_month": 3, "last_month": 1}


async def test_R3_many_creators_stay_fast(client, db, rb, calls, clock):
    now = datetime.now(UTC)
    users = await db.users.insert_many([{"username": f"r{i}", "is_active": True, "created_at": now,
                                         "localization": {"country": "ug"}, "expo_push_tokens": [f"r{i}"]}
                                        for i in range(1500)])
    await db.videos.insert_many([{"creator_id": u, "privacy": "public", "is_active": True, "isReadyToStream": True,
                                  "created_at": datetime(2026, 11, 3 + k, 9, 0, tzinfo=UTC)}
                                 for u in users.inserted_ids for k in range(3)])
    t = time.perf_counter()
    r = await _run(db)
    assert r["weekly"] == 1500 and time.perf_counter() - t < 20


async def test_R4_junk_documents(client, db, rb, calls, clock):
    c, _ = await _creator(db, country=None)
    await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    await db.videos.insert_one({"creator_id": "notanid", "privacy": "public", "is_active": True,
                                "created_at": datetime(2026, 11, 4, 9, 0, tzinfo=UTC)})
    await db.videos.insert_one({"creator_id": ObjectId(c), "privacy": "public", "is_active": True,
                                "created_at": "last tuesday"})
    r = await _run(db)
    assert isinstance(r, dict)


async def test_R5_this_month_on_the_31st_stops_at_the_month_end(client, db, rb, calls, monkeypatch):
    from app.api.v1 import creators as api
    monkeypatch.setattr(api, "_utcnow", lambda: datetime(2027, 1, 31, 9, 0, tzinfo=UTC))
    c, h = await _creator(db)
    await _posts(db, c, [datetime(2027, 1, 30, 9, 0, tzinfo=UTC)])
    await _posts(db, c, [datetime(2027, 2, 1, 9, 0, tzinfo=UTC)])       # mis-dated post in February
    assert (await client.get("/api/v1/creators/me/stats", headers=h)).json()["posting"]["this_month"] == 1


async def test_R6_off_days_cost_nothing(client, db, rb, calls, clock, monkeypatch):
    """Every 30 min, six days a week, no country is in its recap window: no database scan."""
    clock["now"] = datetime(2026, 11, 11, 7, 0, tzinfo=UTC)          # Wednesday 11th, nowhere Monday/1st
    c, _ = await _creator(db)
    await _posts(db, c, [datetime(2026, 11, 4, 9, 0, tzinfo=UTC)])
    scans = []
    real = type(db.videos).aggregate

    def counting(self, *a, **k):
        scans.append(self.name)
        return real(self, *a, **k)
    monkeypatch.setattr(type(db.videos), "aggregate", counting)
    r = await _run(db)
    assert scans == [] and r == {"weekly": 0, "monthly": 0, "nudges": 0}
