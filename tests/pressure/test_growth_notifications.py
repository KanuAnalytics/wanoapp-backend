"""Attacks on growth notifications: followers told about new posts, creator tips
(first-viewer results, streak reminders). Every rule here is about not spamming."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio

# 12:00 UTC: 15:00 in Kampala, 13:00 in Lagos, 12:00 in Accra. Daytime everywhere we test.
NOON = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    from app.services import growth_notifications as g
    state = {"now": NOON}
    monkeypatch.setattr(g, "_utcnow", lambda: state["now"])
    g.set_started(NOON - timedelta(days=2))
    return state


def _sent(calls):
    """(token, title, body, data) for every push that went out, batch or single."""
    out = []
    for name, a, k in calls["pushes"]:
        if name == "send_push_batch_data":
            for m in a[0]:
                out.append((m["to"], m.get("title"), m.get("body"), m.get("data") or {}))
        elif name == "send_push_message":
            token, body = a[0], a[1]
            data = a[2] if len(a) > 2 else k.get("extra")
            title = a[3] if len(a) > 3 else k.get("title")
            out.append((token, title, body, data or {}))
    return out


async def _fan(db, n, *, country="ug", tokens=True, **extra):
    """n followers of nobody yet, each with a push token."""
    out = []
    for i in range(n):
        uid, h = await make_user(db, localization={"country": country},
                                 expo_push_tokens=[f"tok-{ObjectId()}"] if tokens else [], **extra)
        out.append(uid)
    return out


async def _creator_with(db, followers, **extra):
    c, ch = await make_user(db, display_name="Amina", followers=[ObjectId(f) for f in followers],
                            created_at=NOON - timedelta(days=60), **extra)
    for f in followers:
        await db.users.update_one({"_id": ObjectId(f)}, {"$addToSet": {"following": ObjectId(c)}})
    return c, ch


async def _post(db, creator, minutes_ago=5, **extra):
    return await make_video(db, creator, created_at=NOON - timedelta(minutes=minutes_ago),
                            **{"isReadyToStream": True, "description": "Street food in Kampala", **extra})


async def _run(db):
    from app.services import growth_notifications as g
    return await g.notify_new_posts(db)


async def _tokens_of(db, uids):
    return {u["expo_push_tokens"][0] async for u in db.users.find({"_id": {"$in": [ObjectId(x) for x in uids]}})
            if u.get("expo_push_tokens")}


# ---------------------------------------------------------------- followers: new posts

async def test_A1_followers_hear_about_a_new_post_once(client, db, rb, calls, clock):
    fans = await _fan(db, 5)
    silent = await _fan(db, 1, tokens=False)
    gone = await _fan(db, 1, is_active=False)
    c, _ = await _creator_with(db, fans + silent + gone)
    vid = await _post(db, c)
    await _run(db)
    sent = _sent(calls)
    assert {s[0] for s in sent} == await _tokens_of(db, fans)
    assert all(s[3].get("video_id") == vid for s in sent)
    assert all("Amina" in (s[1] or "") for s in sent)


async def test_A2_running_twice_or_at_once_never_doubles(client, db, rb, calls, clock):
    import asyncio
    fans = await _fan(db, 4)
    c, _ = await _creator_with(db, fans)
    await _post(db, c)
    await asyncio.gather(_run(db), _run(db), _run(db))
    await _run(db)
    tokens = [s[0] for s in _sent(calls)]
    assert len(tokens) == 4 and len(set(tokens)) == 4


async def test_A3_a_creator_posting_five_times_alerts_followers_once_a_day(client, db, rb, calls, clock):
    fans = await _fan(db, 3)
    c, _ = await _creator_with(db, fans)
    for m in (50, 40, 30, 20, 10):
        await _post(db, c, minutes_ago=m)
        await _run(db)
    assert len(_sent(calls)) == 3
    clock["now"] = NOON + timedelta(hours=25)
    await _post(db, c, minutes_ago=-24 * 60 - 55)      # posted the next day
    await _run(db)
    assert len(_sent(calls)) == 6


async def test_A4_a_follower_gets_at_most_five_new_post_alerts_a_day(client, db, rb, calls, clock):
    [fan] = await _fan(db, 1)
    for i in range(9):
        c, _ = await _creator_with(db, [fan])
        await _post(db, c, minutes_ago=30 - i)
    await _run(db)
    assert len(_sent(calls)) == 5


async def test_A5_quiet_hours_in_the_followers_own_time(client, db, rb, calls, clock):
    clock["now"] = datetime(2026, 11, 5, 20, 30, tzinfo=timezone.utc)   # 23:30 Kampala, 21:30 Lagos
    ug = await _fan(db, 2, country="ug")
    ng = await _fan(db, 2, country="ng")
    c, _ = await _creator_with(db, ug + ng)
    await make_video(db, c, created_at=clock["now"] - timedelta(minutes=3), isReadyToStream=True)
    await _run(db)
    assert {s[0] for s in _sent(calls)} == await _tokens_of(db, ng)


async def test_A6_opted_out_followers_get_nothing_and_preferences_work(client, db, rb, calls, clock):
    fans = await _fan(db, 2)
    out_id, out_h = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["tok-out"])
    r = await client.put("/api/v1/notifications/preferences", headers=out_h, json={"new_posts": False})
    assert r.status_code == 200 and r.json() == {"new_posts": False, "creator_tips": True}
    assert (await client.get("/api/v1/notifications/preferences", headers=out_h)).json()["new_posts"] is False
    assert (await client.get("/api/v1/notifications/preferences")).status_code == 401
    assert (await client.put("/api/v1/notifications/preferences", headers=out_h,
                             json={"new_posts": "maybe"})).status_code == 422
    assert (await client.put("/api/v1/notifications/preferences", headers=out_h,
                             json={"is_admin": True})).status_code in (200, 422)
    me = await db.users.find_one({"_id": ObjectId(out_id)})
    assert "is_admin" not in (me.get("notification_prefs") or {}) and me.get("user_type") == "standard"
    c, _ = await _creator_with(db, fans + [out_id])
    await _post(db, c)
    await _run(db)
    assert "tok-out" not in {s[0] for s in _sent(calls)} and len(_sent(calls)) == 2


async def test_A7_blocking_either_way_means_no_alert(client, db, rb, calls, clock):
    fans = await _fan(db, 3)
    c, _ = await _creator_with(db, fans)
    await db.users.update_one({"_id": ObjectId(fans[0])}, {"$set": {"blocked_users": [ObjectId(c)]}})
    await db.users.update_one({"_id": ObjectId(fans[1])}, {"$set": {"blocked_by": [ObjectId(c)]}})
    await _post(db, c)
    await _run(db)
    assert {s[0] for s in _sent(calls)} == await _tokens_of(db, fans[2:])


async def test_A8_only_posts_people_can_actually_see(client, db, rb, calls, clock):
    fans = await _fan(db, 2)
    c, _ = await _creator_with(db, fans)
    await _post(db, c, privacy="private")
    await _post(db, c, privacy="unlisted")
    await _post(db, c, isReadyToStream=False)
    await _post(db, c, is_active=False)
    await _post(db, c, status="scheduled", is_active=False)
    await _post(db, c, moderation={"state": "held"}, privacy="private")
    await _run(db)
    assert _sent(calls) == []
    await _post(db, c)               # a real one
    await _run(db)
    assert len(_sent(calls)) == 2


async def test_A8_scheduled_post_alerts_when_it_goes_live(client, db, rb, calls, clock):
    from app.services.scheduled_publisher import publish_post
    fans = await _fan(db, 2)
    c, _ = await _creator_with(db, fans)
    vid = await make_video(db, c, created_at=NOON - timedelta(days=3), is_active=False, status="scheduled",
                           scheduled_at=(NOON - timedelta(minutes=1)).replace(tzinfo=None), isReadyToStream=True)
    await _run(db)
    assert _sent(calls) == []
    assert await publish_post(ObjectId(vid))
    # publish_post stamps the real time; keep the test's daytime clock and date it to now there.
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"created_at": (NOON - timedelta(minutes=1)).replace(tzinfo=None)}})
    await _run(db)
    assert len([s for s in _sent(calls) if s[3].get("video_id") == vid]) == 2


async def test_A9_deploying_does_not_blast_yesterdays_posts(client, db, rb, calls, clock):
    from app.services import growth_notifications as g
    g.set_started(NOON)                       # server just started
    fans = await _fan(db, 2)
    c, _ = await _creator_with(db, fans)
    await _post(db, c, minutes_ago=120)       # posted before the deploy
    await _run(db)
    assert _sent(calls) == []


async def test_A10_big_creator_fans_out_in_batches_fast(client, db, rb, calls, clock):
    now = datetime.now(timezone.utc)
    docs = [{"username": f"f{i}", "email": f"f{i}@x.com", "is_active": True, "created_at": now,
             "localization": {"country": "ug"}, "expo_push_tokens": [f"tok{i}"]} for i in range(6000)]
    res = await db.users.insert_many(docs)
    c, _ = await make_user(db, display_name="Big", followers=list(res.inserted_ids))
    await _post(db, c)
    t = time.perf_counter()
    await _run(db)
    took = time.perf_counter() - t
    batches = [a[0] for n, a, k in calls["pushes"] if n == "send_push_batch_data"]
    assert sum(len(b) for b in batches) == 6000 and max(len(b) for b in batches) <= 100
    assert took < 20, took


async def test_A11_expo_failing_does_not_stop_or_repeat(client, db, rb, calls, clock, monkeypatch):
    from app.services import expo
    seen = []

    def flaky(messages):
        seen.append(len(messages))
        if len(seen) == 1:
            raise RuntimeError("expo 502")
    monkeypatch.setattr(expo, "send_push_batch_data", flaky)
    now = datetime.now(timezone.utc)
    res = await db.users.insert_many([{"username": f"g{i}", "is_active": True, "created_at": now,
                                       "localization": {"country": "ug"}, "expo_push_tokens": [f"t{i}"]}
                                      for i in range(250)])
    c, _ = await make_user(db, followers=list(res.inserted_ids))
    await _post(db, c)
    await _run(db)
    await _run(db)
    assert sum(seen) == 250 and len(seen) == 3          # failed batch not retried in a loop


async def test_A12_message_is_short_and_leaks_nothing(client, db, rb, calls, clock):
    fans = await _fan(db, 1)
    c, _ = await _creator_with(db, fans, email="amina@private.com", phone_number="+256700111222")
    await _post(db, c, description="x" * 5000 + " #wano", privacy="public")
    await _run(db)
    [(token, title, body, data)] = _sent(calls)
    assert len(body) <= 178 and len(title) <= 65
    blob = f"{title}{body}{data}"
    assert "amina@private.com" not in blob and "+256700111222" not in blob
    assert set(data) <= {"video_id", "screen", "type"}


async def test_A13_odd_users_do_not_break_the_run(client, db, rb, calls, clock):
    fans = await _fan(db, 2)
    weird = (await db.users.insert_one({"username": "weird", "is_active": True, "expo_push_tokens": "notalist",
                                        "localization": "nowhere", "notification_prefs": "yes"})).inserted_id
    c, _ = await _creator_with(db, fans)
    await db.users.update_one({"_id": ObjectId(c)}, {"$push": {"followers": {"$each": [weird, "junk", None]}}})
    await _post(db, c)
    r = await _run(db)
    assert len(_sent(calls)) == 2 and r["sent"] == 2


# ---------------------------------------------------------------- creator tips

async def _reach_test(db, vid, n, portion):
    from app.services import creators
    for _ in range(n):
        await creators.record_fy_watch(db, vid, portion)


async def test_C1_creator_hears_how_the_first_viewers_liked_it_once(client, db, rb, calls, clock):
    from app.services import creators
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["creator-tok"])
    vid = await _post(db, c)
    await _reach_test(db, vid, creators.TEST_AUDIENCE - 1, 0.8)
    assert _sent(calls) == []
    await _reach_test(db, vid, 1, 0.8)
    [(tok, title, body, data)] = _sent(calls)
    assert tok == "creator-tok" and "80%" in body and data.get("video_id") == vid
    assert "more people" in body                               # it passed, so it's being boosted
    await _reach_test(db, vid, 40, 0.8)
    assert len(_sent(calls)) == 1


async def test_C1_a_flop_gets_a_tip_not_a_cheer(client, db, rb, calls, clock):
    from app.services import creators
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["creator-tok"])
    vid = await _post(db, c)
    await _reach_test(db, vid, creators.TEST_AUDIENCE, 0.1)
    [(_, _, body, _)] = _sent(calls)
    assert "10%" in body and "more people" not in body and "first" in body.lower()


async def test_C2_at_most_one_creator_tip_a_day(client, db, rb, calls, clock):
    from app.services import creators
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["creator-tok"])
    for i in range(3):
        vid = await _post(db, c, minutes_ago=30 + i)
        await _reach_test(db, vid, creators.TEST_AUDIENCE, 0.7)
    assert len(_sent(calls)) == 1


async def test_C3_C4_quiet_hours_and_opt_out(client, db, rb, calls, clock):
    from app.services import creators
    clock["now"] = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)      # midnight in Kampala
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["night"])
    await _reach_test(db, await _post(db, c), creators.TEST_AUDIENCE, 0.7)
    clock["now"] = NOON
    c2, h2 = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["optout"])
    await client.put("/api/v1/notifications/preferences", headers=h2, json={"creator_tips": False})
    await _reach_test(db, await _post(db, c2), creators.TEST_AUDIENCE, 0.7)
    assert _sent(calls) == []


async def _days(db, c, days_ago, at):
    for d in days_ago:
        await make_video(db, c, created_at=at.replace(hour=9) - timedelta(days=d), isReadyToStream=True)


async def test_C5_streak_reminder_in_the_evening_once(client, db, rb, calls, clock):
    from app.services import growth_notifications as g
    clock["now"] = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)      # 20:00 Kampala
    at_risk, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["risk"])
    await _days(db, at_risk, [1, 2, 3], clock["now"])
    safe, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["safe"])
    await _days(db, safe, [0, 1, 2], clock["now"])
    new, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["one"])
    await _days(db, new, [1], clock["now"])
    await g.streak_reminders(db)
    await g.streak_reminders(db)
    sent = _sent(calls)
    assert [s[0] for s in sent] == ["risk"] and "3-day" in sent[0][2]


async def test_C5_not_in_the_afternoon(client, db, rb, calls, clock):
    from app.services import growth_notifications as g
    clock["now"] = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)      # 15:00 Kampala
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["risk"])
    await _days(db, c, [1, 2, 3], clock["now"])
    await g.streak_reminders(db)
    assert _sent(calls) == []


async def test_C6_streak_reminder_shares_the_daily_tip_budget(client, db, rb, calls, clock):
    from app.services import creators, growth_notifications as g
    clock["now"] = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)      # 19:00 Kampala
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": "ug"}, expo_push_tokens=["tok"])
    await _days(db, c, [1, 2, 3], clock["now"])
    vid = await make_video(db, c, created_at=clock["now"] - timedelta(days=1, hours=2), isReadyToStream=True)
    await _reach_test(db, vid, creators.TEST_AUDIENCE, 0.7)
    await g.streak_reminders(db)
    assert len(_sent(calls)) == 1


async def test_C7_streak_scan_stays_fast_with_many_creators(client, db, rb, calls, clock):
    from app.services import growth_notifications as g
    clock["now"] = datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)
    now = clock["now"]
    users = await db.users.insert_many([{"username": f"c{i}", "is_active": True, "created_at": now,
                                         "localization": {"country": "ug"}, "expo_push_tokens": [f"s{i}"]}
                                        for i in range(1500)])
    await db.videos.insert_many([{"creator_id": u, "privacy": "public", "is_active": True, "isReadyToStream": True,
                                  "created_at": (now - timedelta(days=d)).replace(hour=9)}
                                 for u in users.inserted_ids for d in (1, 2)])
    t = time.perf_counter()
    r = await g.streak_reminders(db)
    assert r["sent"] == 1500 and time.perf_counter() - t < 20


async def test_C8_creator_without_tokens_or_with_junk_localization(client, db, rb, calls, clock):
    from app.services import creators
    c, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization=None, expo_push_tokens=[])
    await _reach_test(db, await _post(db, c), creators.TEST_AUDIENCE, 0.7)
    c2, _ = await make_user(db, created_at=NOON - timedelta(days=60), localization={"country": 42},
                            expo_push_tokens=["junk-loc"])
    await _reach_test(db, await _post(db, c2), creators.TEST_AUDIENCE, 0.7)
    assert [s[0] for s in _sent(calls)] == ["junk-loc"]
