"""Weekly creator leaderboard and badges. Attacks written before the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import REAL_MONGO, make_user, make_video
from tests.pressure.test_growth_notifications import _sent

pytestmark = pytest.mark.asyncio
real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")

# Wednesday 11 Nov 2026, 12:00 UTC. This week started Monday 9 Nov.
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
MON = datetime(2026, 11, 9)


@pytest.fixture
def clock(monkeypatch):
    from app.services import leaderboard
    state = {"now": NOW}
    monkeypatch.setattr(leaderboard, "_utcnow", lambda: state["now"])
    leaderboard.clear_cache()
    return state


async def _day(db, creator, day, **stats):
    await db.creator_daily.update_one({"_id": f"{creator}:{day.date().isoformat()}"},
                                      {"$inc": stats, "$setOnInsert": {"creator_id": ObjectId(creator), "day": day}},
                                      upsert=True)


async def _creator(db, name, score_views, posted=MON + timedelta(days=1), day=MON + timedelta(days=1), **extra):
    c, h = await make_user(db, username=name, display_name=name.title(), **extra)
    if posted:
        await make_video(db, c, created_at=posted.replace(tzinfo=timezone.utc), isReadyToStream=True)
    if score_views:
        await _day(db, c, day, views=score_views)
    return c, h


async def _board(client, h=None, **params):
    if not h:
        params = {**params, "authenticated": "false"}          # how the app calls it logged out
    r = await client.get("/api/v1/leaderboard/weekly", params=params, headers=h or {})
    assert r.status_code == 200, r.text
    return r.json()


async def test_L1_ranks_by_this_weeks_engagement_and_shows_me(client, db, clock):
    a, _ = await _creator(db, "amara", 100)
    b, bh = await _creator(db, "bayo", 50)
    c, _ = await _creator(db, "chidi", 10)
    await _day(db, c, MON + timedelta(days=2), likes=40)          # 40 likes * 3 = 120 -> 130 total
    await _creator(db, "nopost", 999, posted=None)                  # no post this week
    await _creator(db, "oldpost", 999, posted=MON - timedelta(days=2))
    await _creator(db, "banned", 999, is_active=False)
    await _creator(db, "lastweek", 0, day=MON - timedelta(days=1))
    out = await _board(client, bh)
    names = [e["user"]["username"] for e in out["entries"]]
    assert names == ["chidi", "amara", "bayo"]
    assert [e["rank"] for e in out["entries"]] == [1, 2, 3]
    assert out["me"]["rank"] == 3 and out["me"]["user"]["id"] == b
    assert out["week_start"].startswith("2026-11-09")
    assert out["entries"][0]["posts"] == 1


async def test_L2_country_board(client, db, clock):
    await _creator(db, "kofi", 10, localization={"country": "gh"})
    await _creator(db, "wanjiru", 20, localization={"country": "KE"})
    assert [e["user"]["username"] for e in (await _board(client, country="gh"))["entries"]] == ["kofi"]
    assert [e["user"]["username"] for e in (await _board(client, country="KE"))["entries"]] == ["wanjiru"]
    for bad in ("ghana", "g", "$ne", "../"):
        assert (await client.get("/api/v1/leaderboard/weekly", params={"country": bad, "authenticated": "false"})).status_code == 422


async def test_L3_last_week(client, db, clock):
    await _creator(db, "past", 500, posted=MON - timedelta(days=3), day=MON - timedelta(days=3))
    await _creator(db, "now", 5)
    assert [e["user"]["username"] for e in (await _board(client, week="last"))["entries"]] == ["past"]
    assert [e["user"]["username"] for e in (await _board(client))["entries"]] == ["now"]
    assert (await client.get("/api/v1/leaderboard/weekly", params={"week": "2019", "authenticated": "false"})).status_code == 422


async def test_L4_public_fields_only_and_logged_out(client, db, clock):
    await _creator(db, "shy", 10, email="private@x.com", phone_number="+254700000000", expo_push_tokens=["tok"])
    r = await client.get("/api/v1/leaderboard/weekly", params={"authenticated": "false"})
    assert r.status_code == 200 and r.json()["me"] is None
    for secret in ("private@x.com", "+254700000000", "tok"):
        assert secret not in r.text
    assert set(r.json()["entries"][0]["user"]) == {"id", "username", "display_name", "profile_picture"}


async def test_L5_held_posts_and_zero_scores_dont_qualify(client, db, clock):
    c, _ = await make_user(db, username="held")
    await make_video(db, c, created_at=(MON + timedelta(days=1)).replace(tzinfo=timezone.utc),
                     privacy="private", moderation={"state": "held"})
    await _day(db, c, MON + timedelta(days=1), views=900)
    await _creator(db, "zero", 0)
    assert (await _board(client))["entries"] == []


async def test_L6_ties_are_stable_and_limit_caps(client, db, clock):
    ids = [(await _creator(db, f"tie{i}", 10))[0] for i in range(5)]
    first = [e["user"]["id"] for e in (await _board(client))["entries"]]
    assert sorted(first) == sorted(ids)
    from app.services import leaderboard
    leaderboard.clear_cache()
    assert [e["user"]["id"] for e in (await _board(client))["entries"]] == first
    assert len((await _board(client, limit=2))["entries"]) == 2
    assert (await client.get("/api/v1/leaderboard/weekly", params={"limit": 1000, "authenticated": "false"})).status_code == 422


async def test_L7_garbage_daily_rows_are_ignored(client, db, clock):
    c, _ = await _creator(db, "ok", 10)
    await db.creator_daily.insert_one({"_id": "junk1", "creator_id": ObjectId(c), "day": MON + timedelta(days=1),
                                       "views": "lots", "likes": None})
    await db.creator_daily.insert_one({"_id": "junk2", "creator_id": "notanid", "day": MON, "views": 5})
    out = await _board(client)
    assert out["entries"][0]["score"] == 10


@real_only
async def test_L8_big_week_is_quick(client, db, clock):
    import time
    users = [{"username": f"c{i}", "is_active": True} for i in range(4000)]
    ids = (await db.users.insert_many(users)).inserted_ids
    await db.videos.insert_many([{"creator_id": u, "is_active": True, "privacy": "public", "isReadyToStream": True,
                                  "created_at": MON + timedelta(days=1)} for u in ids])
    await db.creator_daily.insert_many([{"_id": f"{u}:{d}", "creator_id": u, "day": MON + timedelta(days=d),
                                         "views": i % 97, "likes": d} for i, u in enumerate(ids) for d in range(3)])
    t0 = time.perf_counter()
    out = await _board(client, limit=50)
    assert len(out["entries"]) == 50 and time.perf_counter() - t0 < 3


# ---------------------------------------------------------------- badges

async def test_B1_top_ten_of_last_week_get_a_badge_once(client, db, clock, calls):
    from app.services import leaderboard
    winners = [(await _creator(db, f"w{i}", 100 - i, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                               localization={"country": "ng"}, expo_push_tokens=[f"t{i}"]))[0] for i in range(12)]
    clock["now"] = datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc)       # too early: rows may still arrive
    assert (await leaderboard.award_badges(db))["weekly"] == 0
    clock["now"] = NOW
    assert (await leaderboard.award_badges(db))["weekly"] == 10
    assert (await leaderboard.award_badges(db))["weekly"] == 0              # idempotent
    got = await db.user_badges.find({"kind": "weekly_top10"}).to_list(None)
    assert sorted(str(b["user_id"]) for b in got) == sorted(winners[:10])
    top = await db.user_badges.find_one({"user_id": ObjectId(winners[0]), "kind": "weekly_top10"})
    assert top["rank"] == 1 and top["week"] == "2026-11-02"


async def test_B2_winners_are_told_but_not_at_night_and_only_once(client, db, clock, calls, monkeypatch):
    from app.services import growth_notifications as g, leaderboard
    day, _ = await _creator(db, "dayowl", 50, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                            localization={"country": "ng"}, expo_push_tokens=["day"])
    night, _ = await _creator(db, "nightowl", 40, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                              localization={"country": "ng"}, expo_push_tokens=["night"],
                              notification_prefs={"creator_tips": True})
    off, _ = await _creator(db, "tipsoff", 30, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                            localization={"country": "ng"}, expo_push_tokens=["off"],
                            notification_prefs={"creator_tips": False})
    late = datetime(2026, 11, 9, 23, 0, tzinfo=timezone.utc)                 # midnight in Lagos
    clock["now"] = late
    monkeypatch.setattr(g, "_utcnow", lambda: late)
    await leaderboard.award_badges(db)
    assert _sent(calls) == []
    morning = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
    clock["now"] = morning
    monkeypatch.setattr(g, "_utcnow", lambda: morning)
    await leaderboard.award_badges(db)
    await leaderboard.award_badges(db)
    tokens = sorted(s[0] for s in _sent(calls))
    assert tokens == ["day", "night"]
    assert "#1" in str([s for s in _sent(calls) if s[0] == "day"][0])
    assert await db.user_badges.count_documents({"user_id": ObjectId(off)}) == 1     # badge, no push


async def test_B3_streak_badges(client, db, clock, calls):
    from app.services import leaderboard
    c, _ = await make_user(db, username="steady")
    for d in range(31):
        await make_video(db, c, created_at=NOW - timedelta(days=d), isReadyToStream=True)
    s, _ = await make_user(db, username="short")
    for d in range(3):
        await make_video(db, s, created_at=NOW - timedelta(days=d), isReadyToStream=True)
    await leaderboard.award_badges(db)
    await leaderboard.award_badges(db)
    kinds = sorted(b["kind"] for b in await db.user_badges.find({"user_id": ObjectId(c)}).to_list(None))
    assert kinds == ["streak_30", "streak_7"]
    assert await db.user_badges.count_documents({"user_id": ObjectId(s)}) == 0


async def test_B4_badges_endpoint(client, db, clock):
    from app.services import leaderboard
    c, ch = await _creator(db, "star", 10, posted=MON - timedelta(days=2), day=MON - timedelta(days=2))
    await leaderboard.award_badges(db)
    r = await client.get(f"/api/v1/users/{c}/badges")
    assert r.status_code == 200
    b = r.json()[0]
    assert b["kind"] == "weekly_top10" and b["rank"] == 1 and b["label"] == "#1 on Wano, week of 2 Nov 2026"
    assert (await client.get("/api/v1/users/notanid/badges")).status_code == 400
    assert (await client.get(f"/api/v1/users/{ObjectId()}/badges")).json() == []
    stats = (await client.get("/api/v1/creators/me/stats", headers=ch)).json()
    assert stats["badges"][0]["kind"] == "weekly_top10"


async def test_B5_empty_week_and_missing_users_dont_crash(client, db, clock):
    from app.services import leaderboard
    assert (await leaderboard.award_badges(db))["weekly"] == 0
    await _day(db, ObjectId(), MON - timedelta(days=2), views=100)        # a deleted account
    assert (await leaderboard.award_badges(db))["weekly"] == 0


# ---------------------------------------------------------------- round 2

async def test_BR1_two_servers_running_the_job_send_one_push(client, db, clock, calls, monkeypatch):
    import asyncio
    from app.services import growth_notifications as g, leaderboard
    await _creator(db, "solo", 50, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                   localization={"country": "ng"}, expo_push_tokens=["solo"])
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    await asyncio.gather(*[leaderboard.award_badges(db) for _ in range(4)])
    assert [s[0] for s in _sent(calls)] == ["solo"]
    assert await db.user_badges.count_documents({}) == 1


async def test_BR2_banned_creator_badges_are_hidden(client, db, clock):
    from app.services import leaderboard
    c, _ = await _creator(db, "later_banned", 10, posted=MON - timedelta(days=2), day=MON - timedelta(days=2))
    await leaderboard.award_badges(db)
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"is_active": False}})
    assert (await client.get(f"/api/v1/users/{c}/badges")).json() == []


async def test_BR3_recap_and_badge_on_the_same_morning_both_arrive(client, db, clock, calls, monkeypatch):
    from app.services import growth_notifications as g, leaderboard
    c, _ = await _creator(db, "busy", 50, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                          localization={"country": "ng"}, expo_push_tokens=["busy"])
    morning = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)          # Monday 10:00 Lagos
    clock["now"] = morning
    monkeypatch.setattr(g, "_utcnow", lambda: morning)
    await g.posting_recaps(db)
    await leaderboard.award_badges(db)
    assert len([s for s in _sent(calls) if s[0] == "busy"]) == 2


async def test_BR4_a_failed_push_is_retried_later(client, db, clock, calls, monkeypatch):
    from app.services import growth_notifications as g, leaderboard
    await _creator(db, "retry", 50, posted=MON - timedelta(days=2), day=MON - timedelta(days=2),
                   localization={"country": "ng"}, expo_push_tokens=["retry"])
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    real = leaderboard._tell

    async def boom(*a, **k):
        raise RuntimeError("expo down")
    monkeypatch.setattr(leaderboard, "_tell", boom)
    try:
        await leaderboard.award_badges(db)
    except RuntimeError:
        pass
    monkeypatch.setattr(leaderboard, "_tell", real)
    await leaderboard.award_badges(db)
    assert [s[0] for s in _sent(calls)] == ["retry"]
