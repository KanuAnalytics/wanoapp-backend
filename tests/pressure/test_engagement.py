"""Replies & duets, weekly challenges, trending sounds, the local mix.
Attacks written before the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_growth_notifications import _sent

pytestmark = pytest.mark.asyncio

CF = "https://videodelivery.net/{}/manifest/video.m3u8"


async def _post(client, h, **body):
    import secrets
    uid = secrets.token_hex(16)
    payload = {"remoteUrl": f"https://x/{uid}.mp4", "remoteUrl_CF": CF.format(uid), "description": "x",
               "isReadyToStream": True, **body}
    return await client.post("/api/v1/videos/post", headers=h, json=payload)


# ---------------------------------------------------------------- replies & duets

async def test_RP1_reply_links_counts_lists_and_tells_the_creator(client, db, calls, monkeypatch):
    from app.services import growth_notifications as g
    c, ch = await make_user(db, expo_push_tokens=["creator"], localization={"country": "ug"})
    monkeypatch.setattr(g, "_utcnow", lambda: datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))   # Kampala daytime
    target = await make_video(db, c, isReadyToStream=True)
    u, uh = await make_user(db, display_name="Fan")
    r = await _post(client, uh, reply_to=target, reply_type="duet")
    assert r.status_code == 201, r.text
    rid = r.json()["video_id"]
    doc = await db.videos.find_one({"_id": ObjectId(rid)})
    assert doc["reply_to"] == {"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "duet"}
    assert doc["is_remix"] is True
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 1
    lst = (await client.get(f"/api/v1/videos/{target}/replies", params={"authenticated": "false"})).json()
    assert [x["id"] for x in lst["items"]] == [rid] and lst["items"][0]["type"] == "duet"
    assert [s[0] for s in _sent(calls)] == ["creator"]


async def test_RP2_what_you_cannot_reply_to(client, db):
    c, ch = await make_user(db)
    _, uh = await make_user(db)
    private = await make_video(db, c, privacy="private")
    closed = await make_video(db, c, remix_enabled=False)
    gone = await make_video(db, c, is_active=False)
    held = await make_video(db, c, privacy="private", moderation={"state": "held"})
    assert (await _post(client, uh, reply_to=private)).status_code == 404
    assert (await _post(client, uh, reply_to=gone)).status_code == 404
    assert (await _post(client, uh, reply_to=held)).status_code == 404
    assert (await _post(client, uh, reply_to=closed)).status_code == 403
    assert (await _post(client, uh, reply_to=str(ObjectId()))).status_code == 404
    assert (await _post(client, uh, reply_to="bad")).status_code in (400, 422)
    assert (await _post(client, uh, reply_to=str(ObjectId()), reply_type="hack")).status_code == 422
    assert (await _post(client, ch, reply_to=private)).status_code == 201      # own private post: fine


async def test_RP3_blocked_people_cannot_reply(client, db):
    c, _ = await make_user(db)
    u, uh = await make_user(db)
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"blocked_users": [ObjectId(u)]}})
    target = await make_video(db, c)
    assert (await _post(client, uh, reply_to=target)).status_code == 403


async def test_RP4_reply_list_hides_hidden_replies_and_pages(client, db):
    c, _ = await make_user(db)
    target = await make_video(db, c)
    banned, _ = await make_user(db, is_active=False)
    ids = []
    for i in range(25):
        u, _ = await make_user(db)
        ids.append(await make_video(db, u, created_at=ago(minutes=i),
                                    reply_to={"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "reply"}))
    await make_video(db, c, privacy="private", reply_to={"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "reply"})
    await make_video(db, banned, reply_to={"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "reply"})
    p1 = (await client.get(f"/api/v1/videos/{target}/replies", params={"limit": 10, "authenticated": "false"})).json()
    p2 = (await client.get(f"/api/v1/videos/{target}/replies",
                           params={"limit": 10, "cursor": p1["next_cursor"], "authenticated": "false"})).json()
    p3 = (await client.get(f"/api/v1/videos/{target}/replies",
                           params={"limit": 10, "cursor": p2["next_cursor"], "authenticated": "false"})).json()
    got = [x["id"] for x in p1["items"] + p2["items"] + p3["items"]]
    assert got == ids and p3["next_cursor"] is None
    assert (await client.get(f"/api/v1/videos/{target}/replies",
                             params={"cursor": "junk", "authenticated": "false"})).status_code == 400


async def test_RP5_replies_to_a_private_post_are_not_listed_to_others(client, db):
    c, ch = await make_user(db)
    target = await make_video(db, c, privacy="private")
    await make_video(db, c, reply_to={"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "reply"})
    r = await client.get(f"/api/v1/videos/{target}/replies", params={"authenticated": "false"})
    assert r.status_code == 404
    assert (await client.get(f"/api/v1/videos/{target}/replies", headers=ch)).status_code == 200


async def test_RP6_remix_setting_is_saved_and_replies_tell_once_a_day(client, db, calls, monkeypatch):
    from app.services import growth_notifications as g
    c, ch = await make_user(db, expo_push_tokens=["c"], localization={"country": "ug"})
    monkeypatch.setattr(g, "_utcnow", lambda: datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))
    r = await _post(client, ch, remix_enabled=False)
    assert (await db.videos.find_one({"_id": ObjectId(r.json()["video_id"])}))["remix_enabled"] is False
    target = await make_video(db, c)
    for _ in range(6):
        _, uh = await make_user(db)
        assert (await _post(client, uh, reply_to=target)).status_code == 201
    from app.services import replies
    assert len([s for s in _sent(calls) if s[0] == "c"]) <= replies.TELLS_PER_DAY


# ---------------------------------------------------------------- challenges

async def _challenge(client, ah, **over):
    now = datetime.now(timezone.utc)
    body = {"hashtag": "WanoDance", "title": "Wano Dance Week", "description": "Show us your moves",
            "starts_at": (now - timedelta(days=1)).isoformat(), "ends_at": (now + timedelta(days=6)).isoformat(),
            "prize_points": 500, **over}
    return await client.post("/api/v1/challenges", headers=ah, json=body)


async def test_CH1_only_admins_create_and_the_rules_hold(client, db):
    _, ah = await make_user(db, user_type="admin")
    _, uh = await make_user(db)
    assert (await _challenge(client, uh)).status_code == 403
    for bad in ({"hashtag": "has space"}, {"hashtag": "#x$"}, {"hashtag": ".*"}, {"hashtag": ""},
                {"ends_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()},
                {"ends_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()},
                {"prize_points": -5}):
        assert (await _challenge(client, ah, **bad)).status_code == 422, bad
    r = await _challenge(client, ah, hashtag="#WanoDance")
    assert r.status_code == 201 and r.json()["hashtag"] == "wanodance"


async def test_CH2_list_and_leaderboard(client, db):
    _, ah = await make_user(db, user_type="admin")
    cid = (await _challenge(client, ah)).json()["id"]
    a, _ = await make_user(db, username="amy")
    b, _ = await make_user(db, username="ben")
    banned, _ = await make_user(db, is_active=False)
    a1 = await make_video(db, a, hashtags=["wanodance"], likes_count=10)
    await make_video(db, a, hashtags=["WanoDance"], likes_count=1)                # Amy's weaker entry
    b1 = await make_video(db, b, hashtags=["WANODANCE"], likes_count=5, comments_count=3)
    await make_video(db, b, hashtags=["wanodance"], privacy="private", likes_count=999)
    await make_video(db, b, hashtags=["wanodance"], created_at=ago(days=3), likes_count=999)   # before it started
    await make_video(db, b, hashtags=["other"], likes_count=999)
    await make_video(db, banned, hashtags=["wanodance"], likes_count=999)
    lst = (await client.get("/api/v1/challenges", params={"authenticated": "false"})).json()
    assert lst[0]["id"] == cid and lst[0]["state"] == "live"
    board = (await client.get(f"/api/v1/challenges/{cid}/leaderboard", params={"authenticated": "false"})).json()
    assert [e["video_id"] for e in board["entries"]] == [a1, b1]
    assert board["entries"][0]["user"]["username"] == "amy" and board["entries"][0]["rank"] == 1
    assert (await client.get(f"/api/v1/challenges/{ObjectId()}/leaderboard",
                             params={"authenticated": "false"})).status_code == 404


async def test_CH3_winners_get_badges_and_prize_points_once(client, db):
    from app.services import challenges, points
    _, ah = await make_user(db, user_type="admin")
    cid = (await _challenge(client, ah)).json()["id"]
    users = []
    for i in range(5):
        u, _ = await make_user(db)
        users.append(u)
        await make_video(db, u, hashtags=["wanodance"], likes_count=50 - i, created_at=ago(hours=5))
    await db.challenges.update_one({"_id": ObjectId(cid)}, {"$set": {"ends_at": datetime.utcnow() - timedelta(hours=2)}})
    await challenges.finish(db)
    await challenges.finish(db)
    for rank, u in enumerate(users[:3], 1):
        b = await db.user_badges.find_one({"user_id": ObjectId(u), "kind": "challenge_top3"})
        assert b and b["rank"] == rank
        assert (await points.summary(db, ObjectId(u)))["balance"] == 500
    assert await db.user_badges.count_documents({"user_id": ObjectId(users[3])}) == 0
    labels = (await client.get(f"/api/v1/users/{users[0]}/badges")).json()
    assert labels[0]["label"] == "#1 in Wano Dance Week"
    assert (await client.get("/api/v1/challenges", params={"authenticated": "false"})).json()[0]["state"] == "ended"


# ---------------------------------------------------------------- sounds

def _sound(i):
    return {"id": f"song{i}", "title": f"Song {i}", "uri": f"https://x/{i}.mp3", "artist": "A"}


async def test_SN1_trending_sounds_count_creators_not_spam(client, db):
    spammer, _ = await make_user(db)
    for _ in range(50):
        await make_video(db, spammer, sound=_sound(1), views_count=1)
    for _ in range(5):
        u, _ = await make_user(db)
        await make_video(db, u, sound=_sound(2), views_count=10)
    for _ in range(3):
        u, _ = await make_user(db)
        await make_video(db, u, sound=_sound(3), privacy="private")
        await make_video(db, u, sound=_sound(4), created_at=ago(days=10))
    r = (await client.get("/api/v1/music/trending", params={"authenticated": "false"})).json()
    assert [s["sound"]["id"] for s in r][:2] == ["song2", "song1"]
    assert r[0]["creators"] == 5 and r[1]["creators"] == 1
    assert "song3" not in [s["sound"]["id"] for s in r] and "song4" not in [s["sound"]["id"] for s in r]


async def test_SN2_sound_page_and_country(client, db):
    ug, _ = await make_user(db)
    ids = [await make_video(db, ug, sound=_sound(7), views_count=v, country="UG") for v in (5, 50, 20)]
    ke, _ = await make_user(db)
    kid = await make_video(db, ke, sound=_sound(7), country="KE", views_count=1000)
    await make_video(db, ke, sound=_sound(7), privacy="private")
    page = (await client.get("/api/v1/music/sounds/song7/posts", params={"authenticated": "false"})).json()
    assert [p["id"] for p in page["items"]] == [kid, ids[1], ids[2], ids[0]]
    assert page["sound"]["title"] == "Song 7"
    ug_only = (await client.get("/api/v1/music/trending", params={"country": "ug", "authenticated": "false"})).json()
    assert ug_only[0]["creators"] == 1 and ug_only[0]["posts"] == 3
    empty = (await client.get("/api/v1/music/sounds/nope/posts", params={"authenticated": "false"})).json()
    assert empty["items"] == [] and empty["sound"] is None
    assert (await client.get("/api/v1/music/trending",
                             params={"country": "$ne", "authenticated": "false"})).status_code == 422


# ---------------------------------------------------------------- local mix

async def test_LC1_city_is_saved_cleanly(client, db):
    u, h = await make_user(db)
    r = await client.put("/api/v1/users/me/city", headers=h, json={"city": "  kampala   CITY "})
    assert r.status_code == 200 and r.json()["city"] == "Kampala City"
    assert (await db.users.find_one({"_id": ObjectId(u)}))["localization"]["city_key"] == "kampala city"
    for bad in ("", "x" * 80, "<script>", "$where"):
        assert (await client.put("/api/v1/users/me/city", headers=h, json={"city": bad})).status_code == 422, bad
    assert (await client.put("/api/v1/users/me/city", headers=h, json={"city": None})).status_code == 200


async def test_LC2_for_you_mixes_in_posts_from_your_city(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    local = []
    for i in range(4):
        c, _ = await make_user(db, localization={"country": "UG", "city": "Gulu", "city_key": "gulu"})
        local.append(await make_video(db, c, created_at=ago(days=2), isReadyToStream=True, likes_count=5))
    _, h = await make_user(db, localization={"country": "UG", "city": "Gulu", "city_key": "gulu"})
    page = (await _feed(client, h, limit=20)).json()
    assert "local" in {v.get("source") for v in page}
    assert {v["id"] for v in page if v.get("source") == "local"} <= set(local)
    _, nh = await make_user(db, localization={"country": "UG"})
    assert "local" not in {v.get("source") for v in (await _feed(client, nh, limit=20)).json()}


async def test_LC3_local_never_shows_own_hidden_or_banned(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    me, h = await make_user(db, localization={"country": "UG", "city": "Gulu", "city_key": "gulu"})
    mine = await make_video(db, me, created_at=ago(days=1))
    b, _ = await make_user(db, is_active=False, localization={"city_key": "gulu"})
    banned = await make_video(db, b, created_at=ago(days=1))
    o, _ = await make_user(db, localization={"city_key": "gulu"})
    hidden = await make_video(db, o, created_at=ago(days=1), privacy="private")
    page = (await _feed(client, h, limit=20)).json()
    assert not {mine, banned, hidden} & {v["id"] for v in page}


# ---------------------------------------------------------------- round 2

async def test_RR1_deleting_a_reply_takes_it_off_the_count(client, db):
    c, _ = await make_user(db)
    _, uh = await make_user(db)
    target = await make_video(db, c, isReadyToStream=True)
    rid = (await _post(client, uh, reply_to=target)).json()["video_id"]
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 1
    r = await client.delete(f"/api/v1/videos/{rid}", headers=uh)
    assert r.status_code in (200, 204), r.text
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 0
    await client.delete(f"/api/v1/videos/{rid}", headers=uh)
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 0


async def test_RR2_a_scheduled_reply_counts_when_it_goes_live(client, db, monkeypatch):
    from app.services import scheduled_publisher
    c, _ = await make_user(db)
    _, uh = await make_user(db)
    target = await make_video(db, c, isReadyToStream=True)
    at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = await _post(client, uh, reply_to=target, scheduled_at=at, timezone="Africa/Kampala")
    assert r.status_code == 201, r.text
    assert (await db.videos.find_one({"_id": ObjectId(target)})).get("remix_count", 0) == 0
    rid = r.json()["video_id"]
    await db.videos.update_one({"_id": ObjectId(rid)}, {"$set": {"scheduled_at": datetime.utcnow() - timedelta(minutes=1)}})
    assert await scheduled_publisher.publish_post(rid)
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 1


async def test_RR3_city_names_from_around_africa(client, db):
    _, h = await make_user(db)
    for city, shown in [("Kigoma-Ujiji", "Kigoma-ujiji"), ("N'Djamena", "N'djamena"), ("São Tomé", "São Tomé"),
                        ("dar es salaam", "Dar Es Salaam"), ("Port Harcourt", "Port Harcourt")]:
        r = await client.put("/api/v1/users/me/city", headers=h, json={"city": city})
        assert r.status_code == 200 and r.json()["city"] == shown, (city, r.text)


async def test_RR4_challenge_with_naive_times_and_overlaps(client, db):
    _, ah = await make_user(db, user_type="admin")
    now = datetime.utcnow()
    body = {"hashtag": "Kampala_Eats", "title": "Kampala Eats", "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(days=7)).isoformat()}
    assert (await client.post("/api/v1/challenges", headers=ah, json=body)).status_code == 201
    assert (await client.post("/api/v1/challenges", headers=ah, json=body)).status_code == 201
    assert len((await client.get("/api/v1/challenges", params={"authenticated": "false"})).json()) == 2


async def test_RR5_sound_ids_are_plain_strings(client, db):
    for sid in ("../../etc", "%24ne", "x" * 65):
        r = await client.get(f"/api/v1/music/sounds/{sid}/posts", params={"authenticated": "false"})
        assert r.status_code in (200, 400, 404)
        if r.status_code == 200:
            assert r.json()["items"] == []
