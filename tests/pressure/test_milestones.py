"""Creator milestones: follower / view ladders celebrated once, a public tier
next to the name, an achievements endpoint, real followers only. Behind the
"milestones" switch. Attacks written before the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

MS = "/api/v1/milestones"
OLD = datetime.now(timezone.utc) - timedelta(days=30)


async def _on(db, on=True):
    await db.config.insert_one({"features": {"milestones": on}})


async def _fans(db, n, **extra):
    if not n:
        return []
    docs = [{"username": f"f{ObjectId()}", "is_active": True, "created_at": OLD, **extra} for _ in range(n)]
    res = await db.users.insert_many(docs)
    return res.inserted_ids


async def _creator(db, followers=0, fake=0, **extra):
    uid, h = await make_user(db, username=extra.pop("username", "amina"), **extra)
    real = await _fans(db, followers)
    bots = await _fans(db, fake, created_at=datetime.now(timezone.utc) - timedelta(hours=2)) if fake else []
    ids = list(real) + list(bots)
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"followers": ids, "followers_count": len(ids)}})
    return uid, h


async def _run(db):
    from app.services import milestones
    return await milestones.award(db)


async def _me(client, h):
    r = await client.get(f"{MS}/me", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_ML1_switch_off_nothing_awarded_and_endpoint_closed(client, db):
    uid, h = await _creator(db, followers=120)
    await _run(db)
    assert await db.user_badges.count_documents({}) == 0
    assert (await client.get(f"{MS}/me", headers=h)).status_code == 404
    assert (await client.get("/api/v1/config/features")).json()["milestones"] is False


async def test_ML2_crossing_100_followers_is_celebrated_once(client, db, calls):
    await _on(db)
    uid, h = await _creator(db, followers=105, expo_push_tokens=["ExponentPushToken[a]"])
    await _run(db)
    await _run(db)
    me = await _me(client, h)
    assert [c["kind"] for c in me["celebrate"]] == ["followers_100"]
    assert me["celebrate"][0]["label"] == "100 followers"
    assert me["user"]["username"] == "amina"
    assert await db.user_badges.count_documents({"kind": "followers_100"}) == 1
    # seen -> not again
    ids = [c["id"] for c in me["celebrate"]]
    assert (await client.post(f"{MS}/seen", json={"ids": ids}, headers=h)).status_code == 200
    assert (await _me(client, h))["celebrate"] == []


async def test_ML3_fake_new_accounts_and_banned_followers_dont_count(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=60, fake=80)     # count says 140, real is 60
    await _run(db)
    assert await db.user_badges.count_documents({}) == 0
    uid2, h2 = await _creator(db, followers=150, username="kofi")
    await db.users.update_many({"_id": {"$in": (await db.users.find_one({"_id": ObjectId(uid2)}))["followers"][:60]}},
                               {"$set": {"is_active": False}})
    await _run(db)
    assert await db.user_badges.count_documents({"user_id": ObjectId(uid2)}) == 0


async def test_ML4_existing_big_creators_get_one_celebration_not_a_flood(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=1200)
    await _run(db)
    kinds = sorted([b["kind"] async for b in db.user_badges.find({"user_id": ObjectId(uid)})])
    assert kinds == ["followers_100", "followers_1000"]
    me = await _me(client, h)
    assert [c["kind"] for c in me["celebrate"]] == ["followers_1000"]       # only the highest pops up
    assert await db.user_badges.count_documents({"notified": False}) == 1   # and only one push


async def test_ML5_public_tier_from_followers(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=1001)
    await _run(db)
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["tier"] == "rising"
    assert (await _me(client, h))["tier"] == {"key": "rising", "label": "Rising"}
    # a viewer sees it on the profile and next to posts in the feed
    _, vh = await make_user(db, username="viewer")
    prof = (await client.get(f"/api/v1/users/{uid}/complete", headers=vh)).json()
    assert prof["tier"] == "rising"
    await make_video(db, uid, isReadyToStream=True)
    feed = (await client.get("/api/v1/feed/", params={"authenticated": "false", "limit": 5})).json()
    assert feed and feed[0]["user"]["tier"] == "rising"


async def test_ML6_tier_is_kept_when_followers_dip(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=1001)
    await _run(db)
    u = await db.users.find_one({"_id": ObjectId(uid)})
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"followers": u["followers"][:900],
                                                                "followers_count": 900}})
    await _run(db)
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["tier"] == "rising"


async def test_ML7_views_ladder_and_a_post_going_far(client, db):
    await _on(db)
    uid, h = await _creator(db)
    await make_video(db, uid, views_count=8000)
    await make_video(db, uid, views_count=3500)
    await make_video(db, uid, views_count=999999, is_active=False)          # deleted posts don't count
    await make_video(db, uid, views_count=999999, privacy="private")
    await _run(db)
    kinds = sorted([b["kind"] async for b in db.user_badges.find({"user_id": ObjectId(uid)})])
    assert kinds == ["post_views_1000", "views_10000"]


async def test_ML8_achievements_page_shows_progress_to_next(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=258)
    await make_video(db, uid, views_count=4200)
    await _run(db)
    me = await _me(client, h)
    f = next(l for l in me["ladders"] if l["key"] == "followers")
    assert f["current"] == 258 and f["next"] == 1000 and f["reached"] == [100]
    v = next(l for l in me["ladders"] if l["key"] == "views")
    assert v["current"] == 4200 and v["next"] == 10000 and v["reached"] == []
    assert me["tier"] is None and me["next_tier"] == {"key": "rising", "label": "Rising", "at": 1000}


async def test_ML9_seen_only_touches_my_own_and_bad_ids_are_ignored(client, db):
    await _on(db)
    a, ah = await _creator(db, followers=101)
    b, bh = await _creator(db, followers=101, username="kofi")
    await _run(db)
    bid = (await _me(client, bh))["celebrate"][0]["id"]
    assert (await client.post(f"{MS}/seen", json={"ids": [bid, "x", {"$ne": 1}]}, headers=ah)).status_code in (200, 422)
    assert (await client.post(f"{MS}/seen", json={"ids": [bid]}, headers=ah)).status_code == 200
    assert len((await _me(client, bh))["celebrate"]) == 1
    assert (await client.post(f"{MS}/seen", json={"ids": ["a"] * 500}, headers=ah)).status_code == 422


async def test_ML10_push_text_is_about_the_milestone(client, db, calls):
    await _on(db)
    from app.services import leaderboard
    uid, h = await _creator(db, followers=101, expo_push_tokens=["ExponentPushToken[a]"],
                            localization={"country": "ug", "timezone": "Africa/Kampala"})
    await _run(db)
    b = await db.user_badges.find_one({"kind": "followers_100"})
    noon = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)    # midday in Kampala
    calls["pushes"].clear()
    assert await leaderboard._tell(db, b, noon) is True
    flat = repr(calls["pushes"])
    assert "100 followers" in flat


async def test_ML11_banned_creators_get_nothing(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=150, is_active=False)
    await _run(db)
    assert await db.user_badges.count_documents({}) == 0


async def test_ML12_logged_out(client, db):
    await _on(db)
    assert (await client.get(f"{MS}/me")).status_code == 401


async def test_ML13_old_badges_dont_pop_up_as_celebrations(client, db):
    await _on(db)
    uid, h = await _creator(db)
    await db.user_badges.insert_one({"_id": f"{uid}:streak_7", "user_id": ObjectId(uid), "kind": "streak_7",
                                     "at": datetime.utcnow(), "notified": True})
    assert (await _me(client, h))["celebrate"] == []


async def test_ML14_user_badges_list_labels_milestones(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=101)
    await _run(db)
    rows = (await client.get(f"/api/v1/users/{uid}/badges")).json()
    assert rows[0]["label"] == "100 followers"


# ---------------------------------------------------------------- round 2

async def test_R2_1_junk_counts_and_lists_dont_break_the_run(client, db):
    await _on(db)
    a, _ = await make_user(db, username="a1", followers_count=500, followers="junk")
    b, _ = await make_user(db, username="b1", followers_count="many")
    c, _ = await make_user(db, username="c1")
    await make_video(db, c, views_count="12000")
    await make_video(db, c, views_count=float("inf"))
    await make_video(db, c, views_count=True)
    d, dh = await _creator(db, followers=101, username="d1")
    out = await _run(db)
    assert await db.user_badges.count_documents({"user_id": ObjectId(d)}) == 1   # the good one still awarded
    assert await db.user_badges.count_documents({"user_id": ObjectId(a)}) == 0


async def test_R2_2_followers_saved_as_text_ids_still_count(client, db):
    await _on(db)
    uid, h = await make_user(db, username="legacy")
    fans = await _fans(db, 120)
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"followers": [str(f) for f in fans],
                                                                "followers_count": 120}})
    await _run(db)
    assert await db.user_badges.count_documents({"kind": "followers_100"}) == 1


async def test_R2_3_scheduled_and_held_posts_dont_count(client, db):
    await _on(db)
    uid, h = await _creator(db)
    await make_video(db, uid, views_count=50000, status="scheduled")
    await make_video(db, uid, views_count=50000, moderation={"state": "held"})
    await _run(db)
    assert await db.user_badges.count_documents({}) == 0


async def test_R2_4_two_runs_at_once_award_once(client, db):
    import asyncio
    await _on(db)
    uid, h = await _creator(db, followers=1500)
    await asyncio.gather(_run(db), _run(db), _run(db))
    assert await db.user_badges.count_documents({"kind": "followers_1000"}) == 1
    assert await db.user_badges.count_documents({"notified": False}) == 1


async def test_R2_5_turning_the_switch_off_stops_awards_and_celebrations(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=101)
    await db.config.update_many({}, {"$set": {"features.milestones": False}})
    await _run(db)
    assert await db.user_badges.count_documents({}) == 0
    assert (await client.get(f"{MS}/seen")).status_code in (401, 404, 405)


async def test_R2_6_next_step_jump_awards_skipped_steps_quietly(client, db):
    await _on(db)
    uid, h = await _creator(db, followers=101)
    await _run(db)
    fans = await _fans(db, 1000)
    u = await db.users.find_one({"_id": ObjectId(uid)})
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"followers": u["followers"] + list(fans),
                                                                "followers_count": 1101}})
    await _run(db)
    me = await _me(client, h)
    assert [c["kind"] for c in me["celebrate"]] == ["followers_1000", "followers_100"]
    assert me["tier"]["key"] == "rising"


async def test_R2_7_celebrations_capped_at_three(client, db):
    await _on(db)
    uid, h = await _creator(db)
    for k in ["followers_100", "views_10000", "post_views_1000", "followers_1000", "views_100000"]:
        await db.user_badges.insert_one({"_id": f"{uid}:{k}", "user_id": ObjectId(uid), "kind": k,
                                         "at": datetime.utcnow(), "notified": True, "seen": False})
    assert len((await _me(client, h))["celebrate"]) == 3


async def test_R2_8_tier_in_feed_never_leaks_junk(client, db):
    await _on(db)
    uid, h = await make_user(db, username="odd", tier={"$ne": 1})
    await make_video(db, uid, isReadyToStream=True)
    _, vh = await make_user(db, username="viewer2")
    prof = (await client.get(f"/api/v1/users/{uid}/complete", headers=vh)).json()
    assert prof.get("tier") is None
