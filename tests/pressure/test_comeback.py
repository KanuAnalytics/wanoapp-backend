"""Comeback reminders ("comeback" switch): one helpful push to people away a
few days, never spam, never to old apps, never with nothing to say."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import comeback
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

# 15:00 UTC = 18:00 in Kampala (local evening window 18-21).
EVENING = datetime(2026, 11, 20, 15, 30)


def _sent(calls):
    out = []
    for name, a, k in calls["pushes"]:
        if name == "send_push_batch_data":
            out += [(m["to"], m.get("title"), m.get("body"), m.get("data") or {}) for m in a[0]]
    return out


async def _on(db):
    await db.config.insert_one({"features": {"comeback": True}})


async def _away(db, days=5, **extra):
    uid, h = await make_user(db, expo_push_tokens=[f"tok-{ObjectId()}"], last_open=EVENING - timedelta(days=days), **extra)
    return uid, h


async def test_CB1_open_saved_only_with_switch_and_throttled(client, db):
    uid, h = await make_user(db)
    r = await client.post("/api/v1/comeback/open", headers=h)
    assert r.status_code == 200 and r.json() == {"ok": False}
    assert "last_open" not in await db.users.find_one({"_id": ObjectId(uid)})
    await _on(db)
    assert (await client.post("/api/v1/comeback/open", headers=h)).json() == {"ok": True}
    first = (await db.users.find_one({"_id": ObjectId(uid)}))["last_open"]
    await client.post("/api/v1/comeback/open", headers=h)
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["last_open"] == first     # not rewritten within 6 h
    assert (await client.post("/api/v1/comeback/open")).status_code == 401


async def test_CB2_switch_off_sends_nothing(db, calls):
    uid, _ = await _away(db)
    await db.challenges.insert_one({"hashtag": "whereimfrom", "title": "Where I'm From", "description": "Show us home",
                                    "starts_at": EVENING - timedelta(days=1), "ends_at": EVENING + timedelta(days=5)})
    out = await comeback.comeback_nudges(db, now=EVENING)
    assert out == {"checked": 0, "sent": 0} and _sent(calls) == []


async def test_CB3_live_challenge_reminder_once_a_week(db, calls):
    await _on(db)
    uid, _ = await _away(db)
    cid = (await db.challenges.insert_one({"hashtag": "whereimfrom", "title": "Where I'm From", "description": "Show us home",
                                           "starts_at": EVENING - timedelta(days=1),
                                           "ends_at": EVENING + timedelta(days=5)})).inserted_id
    out = await comeback.comeback_nudges(db, now=EVENING)
    assert out["sent"] == 1
    (tok, title, body, data), = _sent(calls)
    assert title == "#whereimfrom is live" and body == "Show us home"
    assert data == {"type": "challenge", "challenge_id": str(cid)}
    calls["pushes"].clear()
    # Next run, next day: nothing (once a week).
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"last_open": EVENING - timedelta(days=5)}})
    assert (await comeback.comeback_nudges(db, now=EVENING + timedelta(days=1)))["sent"] == 0
    assert _sent(calls) == []


async def test_CB4_new_posts_from_people_followed(db, calls):
    await _on(db)
    a, _ = await make_user(db, display_name="Amina")
    b, _ = await make_user(db, display_name="Kofi")
    uid, _ = await _away(db, following=[ObjectId(a), ObjectId(b)])
    for _ in range(3):
        await make_video(db, a, created_at=EVENING - timedelta(days=1))
    await make_video(db, b, created_at=EVENING - timedelta(days=2))
    await make_video(db, b, created_at=EVENING - timedelta(days=9))        # before they left: not counted
    assert (await comeback.comeback_nudges(db, now=EVENING))["sent"] == 1
    (_, title, body, data), = _sent(calls)
    assert body == "Amina and 1 other you follow posted 4 new videos since you were last here."
    assert data["type"] == "comeback"


async def test_CB5_nothing_to_say_nothing_sent(db, calls):
    await _on(db)
    await _away(db)                      # follows nobody, no challenge
    assert (await comeback.comeback_nudges(db, now=EVENING))["sent"] == 0
    assert _sent(calls) == []


@pytest.mark.parametrize("case", ["recent", "long_gone", "old_app", "no_token", "staff", "prefs_off",
                                  "seen_challenge", "night", "inactive", "ending_soon"])
async def test_CB6_who_never_gets_one(db, calls, case):
    await _on(db)
    starts = EVENING - timedelta(days=1)
    ends = EVENING + timedelta(hours=6 if case == "ending_soon" else 120)
    await db.challenges.insert_one({"hashtag": "x", "title": "X", "description": "Go", "starts_at": starts, "ends_at": ends})
    kw = {"expo_push_tokens": [f"tok-{ObjectId()}"], "last_open": EVENING - timedelta(days=5)}
    if case == "recent":
        kw["last_open"] = EVENING - timedelta(days=1)
    if case == "long_gone":
        kw["last_open"] = EVENING - timedelta(days=30)
    if case == "old_app":
        kw.pop("last_open")
    if case == "no_token":
        kw["expo_push_tokens"] = []
    if case == "staff":
        kw["user_type"] = "admin"
    if case == "prefs_off":
        kw["notification_prefs"] = {"new_posts": False}
    if case == "seen_challenge":
        kw["last_open"] = starts + timedelta(hours=1)
        kw["following"] = []
        # opened after it started, but still away 3+ days for the test
        await db.challenges.update_many({}, {"$set": {"starts_at": EVENING - timedelta(days=6),
                                                      "ends_at": EVENING + timedelta(days=5)}})
        kw["last_open"] = EVENING - timedelta(days=4)
    if case == "night":
        kw["localization"] = {"country": "ng"}        # 16:30 in Lagos: before the evening window
    if case == "inactive":
        kw["is_active"] = False
    await make_user(db, **kw)
    assert (await comeback.comeback_nudges(db, now=EVENING))["sent"] == 0
    assert _sent(calls) == []


async def test_CB7_blocked_creators_not_mentioned(db, calls):
    await _on(db)
    a, _ = await make_user(db, display_name="Blocked one")
    uid, _ = await _away(db, following=[ObjectId(a)], blocked_users=[ObjectId(a)])
    await make_video(db, a, created_at=EVENING - timedelta(days=1))
    assert (await comeback.comeback_nudges(db, now=EVENING))["sent"] == 0


async def test_CB8_daily_budget_respected(db, calls):
    await _on(db)
    uid, _ = await _away(db)
    await db.challenges.insert_one({"hashtag": "x", "title": "X", "description": "Go",
                                    "starts_at": EVENING - timedelta(days=1), "ends_at": EVENING + timedelta(days=5)})
    from app.services import growth_notifications as gn
    from datetime import timezone
    u = await db.users.find_one({"_id": ObjectId(uid)})
    key = gn._day_key(u, EVENING.replace(tzinfo=timezone.utc))
    await db.notif_daily.insert_one({"_id": key, "user_id": u["_id"], "new_posts": gn.NEW_POSTS_PER_DAY})
    assert (await comeback.comeback_nudges(db, now=EVENING))["sent"] == 0


async def test_CB9_switch_listed_for_apps(client, db):
    r = (await client.get("/api/v1/config/features")).json()
    assert r["quick_launch"] is False and r["push_ask"] is False and r["comeback"] is False
    await db.config.insert_one({"features": {"quick_launch": True, "push_ask": True, "comeback": True}})
    r = (await client.get("/api/v1/config/features")).json()
    assert r["quick_launch"] and r["push_ask"] and r["comeback"]
