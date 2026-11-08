"""Wano Picks + Road to Wano TV, weekly missions, creator stats, trial posts.
Attacks written with the code."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_engagement import _post

pytestmark = pytest.mark.asyncio
C = "/api/v1/creator"


async def _on(db, **sw):
    await db.config.insert_one({"features": {k: True for k in sw} or {}})


def _ws():
    from app.services import leaderboard
    return leaderboard.week_start(datetime.now(timezone.utc))


# ---------------------------------------------------------------- picks

async def test_CP1_switches_off_everything_closed(client, db):
    _, h = await make_user(db)
    for m, p in [("GET", "/picks"), ("GET", "/missions"), ("GET", "/stats"), ("POST", "/trial/x/share"),
                 ("POST", "/missions/post_3/claim")]:
        assert (await client.request(m, C + p, headers=h)).status_code == 404, p
    f = (await client.get("/api/v1/config/features")).json()
    assert not any(f[k] for k in ("picks", "missions", "creator_stats", "trial_posts"))


async def test_CP2_admin_picks_a_post_creator_gets_badge_and_it_shows(client, db):
    await _on(db, picks=True)
    c, _ = await make_user(db, username="maker")
    v = await make_video(db, c, title="Kampala nights")
    _, ah = await make_user(db, user_type="admin")
    link = f"https://share.wanoafrica.com/pages/feed?videoId={v}&single=true"
    r = await client.post("/api/v1/admin/picks", json={"video": link, "note": "Beautiful"}, headers=ah)
    assert r.status_code == 201, r.text
    assert (await client.post("/api/v1/admin/picks", json={"video": v}, headers=ah)).status_code == 409
    items = (await client.get(f"{C}/picks", params={"authenticated": "false"})).json()["items"]
    assert items[0]["video_id"] == v and items[0]["note"] == "Beautiful" and items[0]["creator"]["username"] == "maker"
    assert await db.user_badges.count_documents({"kind": "wano_pick", "user_id": ObjectId(c)}) == 1
    assert (await client.get(f"/api/v1/users/{c}/badges")).json()[0]["label"] == "Wano Pick"


async def test_CP3_cant_pick_private_removed_banned_or_junk(client, db):
    await _on(db, picks=True)
    c, _ = await make_user(db)
    b, _ = await make_user(db, is_active=False)
    _, ah = await make_user(db, user_type="admin")
    for v in [await make_video(db, c, privacy="private"), await make_video(db, c, moderation={"state": "removed"}),
              await make_video(db, b)]:
        assert (await client.post("/api/v1/admin/picks", json={"video": v}, headers=ah)).status_code == 404
    for junk in ["hello", "", "https://evil.com/x"]:
        assert (await client.post("/api/v1/admin/picks", json={"video": junk}, headers=ah)).status_code == 422
    _, uh = await make_user(db)
    assert (await client.post("/api/v1/admin/picks", json={"video": "x"}, headers=uh)).status_code == 403


async def test_CP4_picks_expire_and_hide_when_post_or_creator_goes(client, db):
    await _on(db, picks=True)
    c, _ = await make_user(db)
    v1, v2, v3 = [await make_video(db, c) for _ in range(3)]
    _, ah = await make_user(db, user_type="admin")
    for v in (v1, v2, v3):
        await client.post("/api/v1/admin/picks", json={"video": v}, headers=ah)
    await db.picks.update_one({"video_id": ObjectId(v1)}, {"$set": {"until": datetime.utcnow() - timedelta(minutes=1)}})
    await db.videos.update_one({"_id": ObjectId(v2)}, {"$set": {"is_active": False}})
    items = (await client.get(f"{C}/picks", params={"authenticated": "false"})).json()["items"]
    assert [i["video_id"] for i in items] == [v3]
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"is_active": False}})
    assert (await client.get(f"{C}/picks", params={"authenticated": "false"})).json()["items"] == []


async def test_CP5_remove_pick_and_road_to_wano_tv(client, db):
    await _on(db, picks=True, milestones=True)
    c, ch = await make_user(db, username="star")
    v = await make_video(db, c)
    _, ah = await make_user(db, user_type="admin")
    pid = (await client.post("/api/v1/admin/picks", json={"video": v}, headers=ah)).json()["id"]
    assert (await client.delete(f"/api/v1/admin/picks/{pid}", headers=ah)).status_code == 200
    assert (await client.delete(f"/api/v1/admin/picks/{pid}", headers=ah)).status_code == 404
    road = {s["key"]: s["done"] for s in (await client.get("/api/v1/milestones/me", headers=ch)).json()["road"]}
    assert road == {"rising": False, "crowned": False, "picked": True, "wano_tv": False}
    assert (await client.post("/api/v1/admin/wano-tv", json={"creator": "@star"}, headers=ah)).status_code == 200
    road = {s["key"]: s["done"] for s in (await client.get("/api/v1/milestones/me", headers=ch)).json()["road"]}
    assert road["wano_tv"] is True
    assert await db.admin_log.count_documents({"action": "wano_tv_featured"}) == 1


# ---------------------------------------------------------------- missions

async def test_CP6_missions_progress_claim_once_and_never_cash(client, db):
    await _on(db, missions=True)
    uid, h = await make_user(db)
    for _ in range(3):
        await make_video(db, uid, created_at=_ws() + timedelta(hours=1))
    rows = {m["key"]: m for m in (await client.get(f"{C}/missions", headers=h)).json()["items"]}
    assert rows["post_3"]["done"] and rows["post_3"]["progress"] == 3 and not rows["reply_1"]["done"]
    assert "home_1" not in rows                         # Home Ground off
    r = await client.post(f"{C}/missions/post_3/claim", headers=h)
    assert r.status_code == 200 and r.json()["points"] == 30
    assert (await client.post(f"{C}/missions/post_3/claim", headers=h)).status_code == 409
    assert (await client.post(f"{C}/missions/reply_1/claim", headers=h)).status_code == 400
    assert (await client.post(f"{C}/missions/hack/claim", headers=h)).status_code == 404
    assert (await db.points_wallet.find_one({"_id": ObjectId(uid)}))["balance"] == 30
    from app.services import points
    assert await points._month_shares(db, points.month_of(datetime.utcnow())) == {}      # not cash


async def test_CP7_parallel_claims_pay_once(client, db):
    await _on(db, missions=True)
    uid, h = await make_user(db)
    for _ in range(3):
        await make_video(db, uid, created_at=_ws() + timedelta(hours=1))
    rs = await asyncio.gather(*[client.post(f"{C}/missions/post_3/claim", headers=h) for _ in range(8)])
    assert sorted(r.status_code for r in rs).count(200) == 1
    assert (await db.points_wallet.find_one({"_id": ObjectId(uid)}))["balance"] == 30


async def test_CP8_last_weeks_hidden_private_posts_dont_count(client, db):
    await _on(db, missions=True)
    uid, h = await make_user(db)
    await make_video(db, uid, created_at=_ws() - timedelta(hours=1))
    await make_video(db, uid, created_at=_ws() + timedelta(hours=1), privacy="private")
    await make_video(db, uid, created_at=_ws() + timedelta(hours=1), moderation={"state": "held"})
    rows = {m["key"]: m for m in (await client.get(f"{C}/missions", headers=h)).json()["items"]}
    assert rows["post_3"]["progress"] == 0


async def test_CP9_reply_challenge_comment_home_missions(client, db):
    await _on(db, missions=True, home_ground=True)
    uid, h = await make_user(db, home_ground={"city": "kampala", "set_at": datetime.utcnow()})
    other, _ = await make_user(db)
    target = await make_video(db, other)
    assert (await _post(client, h, reply_to=target)).status_code == 201
    now = datetime.utcnow()
    await db.challenges.insert_one({"hashtag": "DanceUG", "starts_at": now - timedelta(days=1),
                                    "ends_at": now + timedelta(days=3)})
    await make_video(db, uid, created_at=datetime.now(timezone.utc), hashtags=["danceug"])
    for i in range(5):
        v = await make_video(db, other)
        await db.comments.insert_one({"video_id": ObjectId(v), "user_id": ObjectId(uid), "created_at": datetime.utcnow()})
    mine = await make_video(db, uid)
    for _ in range(3):   # comments on my own post don't count
        await db.comments.insert_one({"video_id": ObjectId(mine), "user_id": ObjectId(uid), "created_at": datetime.utcnow()})
    rows = {m["key"]: m for m in (await client.get(f"{C}/missions", headers=h)).json()["items"]}
    assert rows["reply_1"]["done"] and rows["challenge_1"]["done"] and rows["home_1"]["done"]
    assert rows["comment_5"]["progress"] == 5


# ---------------------------------------------------------------- stats

async def test_CP10_stats_series_totals_best_posts_and_hours(client, db):
    await _on(db, creator_stats=True)
    from app.services import creators
    uid, h = await make_user(db, followers_count=42)
    today = creators._day(datetime.now(timezone.utc))
    await db.creator_daily.insert_many([
        {"creator_id": ObjectId(uid), "day": today, "views": 10, "likes": 2, "comments": 1, "shares": 0},
        {"creator_id": ObjectId(uid), "day": today - timedelta(days=3), "views": 5, "likes": "x", "comments": None},
        {"creator_id": ObjectId(uid), "day": today - timedelta(days=20), "views": 999},
    ])
    for i in range(6):
        await make_video(db, uid, views_count=100 * i, created_at=today - timedelta(days=1) + timedelta(hours=i))
    s = (await client.get(f"{C}/stats", params={"days": 7}, headers=h)).json()
    assert len(s["series"]) == 7 and s["series"][-1]["views"] == 10
    assert s["totals"] == {"views": 15, "likes": 2, "comments": 1, "shares": 0}
    assert [p["views"] for p in s["best_posts"]] == [500, 400, 300]
    assert s["best_hours"] is not None and s["followers"] == 42
    s28 = (await client.get(f"{C}/stats", params={"days": 28}, headers=h)).json()
    assert s28["totals"]["views"] == 1014
    assert (await client.get(f"{C}/stats", params={"days": 365}, headers=h)).status_code == 422


async def test_CP11_stats_are_only_mine(client, db):
    await _on(db, creator_stats=True)
    from app.services import creators
    a, ah = await make_user(db)
    b, _ = await make_user(db)
    await db.creator_daily.insert_one({"creator_id": ObjectId(b), "day": creators._day(datetime.now(timezone.utc)),
                                       "views": 77})
    assert (await client.get(f"{C}/stats", headers=ah)).json()["totals"]["views"] == 0
    assert (await client.get(f"{C}/stats")).status_code == 401


# ---------------------------------------------------------------- trial posts

async def test_CP12_trial_post_isnt_announced_or_on_profile_for_others(client, db, calls, monkeypatch):
    await _on(db, trial_posts=True)
    from app.services import growth_notifications as g
    monkeypatch.setattr(g, "_quiet", lambda u, now: False)     # not clock-dependent
    c, ch = await make_user(db, username="tester")
    f, fh = await make_user(db, expo_push_tokens=["ExponentPushToken[f]"], notification_prefs={"new_posts": True})
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"followers": [ObjectId(f)]}})
    r = await _post(client, ch, trial=True)
    vid = r.json()["video_id"]
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["trial"] is True
    g.set_started(datetime.now(timezone.utc) - timedelta(hours=1))
    calls["pushes"].clear()
    await g.notify_new_posts(db)
    assert calls["pushes"] == []
    other = (await client.get(f"/api/v1/users/{c}/complete", params={"include_videos": "true"}, headers=fh)).json()
    assert other["recent_videos"] == []
    own = (await client.get(f"/api/v1/users/{c}/complete", params={"include_videos": "true"}, headers=ch)).json()
    assert own["recent_videos"][0]["trial"] is True
    trials = (await client.get(f"{C}/trials", headers=ch)).json()["items"]
    assert [t["id"] for t in trials] == [vid]
    assert (await client.get(f"{C}/trials", headers=fh)).json()["items"] == []
    # share it: followers hear about it, it shows on the profile
    assert (await client.post(f"{C}/trial/{vid}/share", headers=ch)).status_code == 200
    await g.notify_new_posts(db)
    assert len(calls["pushes"]) >= 1
    other = (await client.get(f"/api/v1/users/{c}/complete", params={"include_videos": "true"}, headers=fh)).json()
    assert [v["id"] for v in other["recent_videos"]] == [vid]


async def test_CP13_trial_ignored_when_switch_off_and_only_owner_can_share(client, db):
    c, ch = await make_user(db)
    vid = (await _post(client, ch, trial=True)).json()["video_id"]
    assert "trial" not in await db.videos.find_one({"_id": ObjectId(vid)})
    await _on(db, trial_posts=True)
    vid2 = (await _post(client, ch, trial=True)).json()["video_id"]
    _, xh = await make_user(db)
    assert (await client.post(f"{C}/trial/{vid2}/share", headers=xh)).status_code == 404
    assert (await client.post(f"{C}/trial/{vid}/share", headers=ch)).status_code == 404      # not a trial
    assert (await client.post(f"{C}/trial/junk/share", headers=ch)).status_code == 404
