"""Interactive replies (feed shows the reply link and real count; edited/multi-clip
replies keep their link) and the hashtag library (suggestions while typing,
ideas). Attacks written before the code."""

import asyncio
import secrets
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_engagement import _post

pytestmark = pytest.mark.asyncio

CF = "https://videodelivery.net/{}/manifest/video.m3u8"


@pytest.fixture(autouse=True)
def _fresh_hashtag_cache():
    from app.services import hashtags
    hashtags._cache.clear()
    yield
    hashtags._cache.clear()


async def _feed(client, **params):
    r = await client.get("/api/v1/feed/", params={"authenticated": "false", "limit": 20, **params})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- replies

async def test_RX1_reply_link_saves_the_original_creators_username(client, db):
    c, _ = await make_user(db, username="amina")
    _, uh = await make_user(db)
    target = await make_video(db, c)
    rid = (await _post(client, uh, reply_to=target)).json()["video_id"]
    doc = await db.videos.find_one({"_id": ObjectId(rid)})
    assert doc["reply_to"]["username"] == "amina" and doc["reply_to"]["type"] == "reply"


async def test_RX2_feed_shows_real_reply_count_and_the_reply_link(client, db):
    c, _ = await make_user(db, username="amina")
    _, uh = await make_user(db)
    target = await make_video(db, c, isReadyToStream=True)
    for _ in range(3):
        assert (await _post(client, uh, reply_to=target)).status_code == 201
    feed = await _feed(client)
    by_id = {v["id"]: v for v in feed}
    assert by_id[target]["replies_count"] == 3
    reply = next(v for v in feed if v["id"] != target)
    assert reply["reply_to"] == {"video_id": target, "type": "reply", "username": "amina"}
    assert by_id[target].get("reply_to") is None


async def test_RX3_reply_link_in_feed_never_leaks_a_hidden_original(client, db):
    c, _ = await make_user(db, username="amina")
    _, uh = await make_user(db)
    target = await make_video(db, c)
    rid = (await _post(client, uh, reply_to=target)).json()["video_id"]
    await db.videos.update_one({"_id": ObjectId(rid)}, {"$set": {"isReadyToStream": True}})
    await db.videos.update_one({"_id": ObjectId(target)}, {"$set": {"privacy": "private"}})
    reply = next(v for v in await _feed(client) if v["id"] == rid)
    assert reply.get("reply_to") is None          # original went private: no link to it


async def test_RX4_old_reply_without_username_and_garbage_counts(client, db):
    c, _ = await make_user(db, username="kofi")
    target = await make_video(db, c, remix_count="lots")
    await make_video(db, c, reply_to={"video_id": ObjectId(target), "creator_id": ObjectId(c), "type": "duet"})
    await make_video(db, c, reply_to="junk")
    feed = await _feed(client)
    assert next(v for v in feed if v["id"] == target)["replies_count"] == 0
    links = [v.get("reply_to") for v in feed if v["id"] != target]
    assert {"video_id": target, "type": "duet", "username": "kofi"} in links and None in links


def _stitch_body(**over):
    uid = secrets.token_hex(16)
    return {"videoUrls": [{"url": CF.format(uid), "start": 0, "end": 5}], "title": "t", "description": "x", **over}


@pytest.fixture
def stream_not_ready(monkeypatch):
    from app.services import stitch_jobs
    monkeypatch.setattr(stitch_jobs, "get_stream_video_status", lambda uid: {"readyToStream": False})


async def test_RX5_edited_multi_clip_reply_keeps_its_link(client, db, stream_not_ready):
    from app.services import stitch_jobs
    c, _ = await make_user(db, username="amina")
    _, uh = await make_user(db)
    target = await make_video(db, c)
    r = await client.post("/api/v1/videos/stitch", headers=uh, json=_stitch_body(reply_to=target))
    assert r.status_code == 202, r.text
    job = await db.pending_videos.find_one({"_id": ObjectId(r.json()["job_id"])})
    vid = await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert doc["reply_to"]["video_id"] == ObjectId(target) and doc["reply_to"]["username"] == "amina"
    assert (await db.videos.find_one({"_id": ObjectId(target)}))["remix_count"] == 1


async def test_RX6_stitch_reply_rules_are_checked_before_the_job_starts(client, db, stream_not_ready):
    c, ch = await make_user(db)
    _, uh = await make_user(db)
    closed = await make_video(db, c, remix_enabled=False)
    private = await make_video(db, c, privacy="private")
    assert (await client.post("/api/v1/videos/stitch", headers=uh, json=_stitch_body(reply_to=closed))).status_code == 403
    assert (await client.post("/api/v1/videos/stitch", headers=uh, json=_stitch_body(reply_to=private))).status_code == 404
    assert (await client.post("/api/v1/videos/stitch", headers=uh,
                              json=_stitch_body(reply_to=str(ObjectId()), reply_type="hack"))).status_code == 422
    assert await db.pending_videos.count_documents({}) == 0
    # a plain stitch with no reply still works and has no link
    r = await client.post("/api/v1/videos/stitch", headers=uh, json=_stitch_body())
    assert r.status_code == 202
    job = await db.pending_videos.find_one({})
    assert job["post"].get("reply_to") is None


# ---------------------------------------------------------------- hashtags

async def test_HT1_suggest_mixes_live_challenges_trending_and_the_library(client, db):
    _, ah = await make_user(db, user_type="admin")
    c, _ = await make_user(db)
    now = datetime.utcnow()
    await db.challenges.insert_one({"hashtag": "azontoug", "title": "Azonto", "starts_at": now - timedelta(days=1),
                                    "ends_at": now + timedelta(days=3), "prize_points": 0})
    for i in range(5):
        await make_video(db, c, hashtags=["AzontoDance", "kampala"], created_at=now - timedelta(days=1))
    r = await client.get("/api/v1/hashtags/suggest", params={"q": "#azo", "authenticated": "false"})
    assert r.status_code == 200
    tags = r.json()
    assert tags[0] == {"tag": "azontoug", "source": "challenge", "posts": 0, "category": None}
    assert {"tag": "AzontoDance", "source": "trending", "posts": 5, "category": None} in tags
    assert all(t["tag"].lower().startswith("azo") for t in tags)


async def test_HT2_library_is_deep_multilingual_and_accent_insensitive(client, db):
    lib = (await client.get("/api/v1/hashtags/library", params={"authenticated": "false"})).json()
    assert len({t["tag"].lower() for c in lib for t in c["tags"]}) >= 300
    cats = {c["category"] for c in lib}
    assert {"Music & dance", "Comedy", "Food", "Fashion & beauty", "Culture", "Places", "Sports", "Faith",
            "Business", "Wano TV"} <= cats
    langs = {t.get("lang") for c in lib for t in c["tags"]}
    assert {"en", "fr", "pt", "sw"} <= langs
    r = await client.get("/api/v1/hashtags/suggest", params={"q": "ecole", "authenticated": "false"})
    assert any(t["tag"].lower().startswith("école") or t["tag"].lower().startswith("ecole") for t in r.json())


async def test_HT3_empty_query_gives_popular_and_hidden_posts_dont_count(client, db):
    c, _ = await make_user(db)
    banned, _ = await make_user(db, is_active=False)
    now = datetime.utcnow()
    for _ in range(4):
        await make_video(db, c, hashtags=["wanochallenge"], created_at=now)
    for _ in range(9):
        await make_video(db, c, hashtags=["secret"], privacy="private", created_at=now)
        await make_video(db, banned, hashtags=["spam"], created_at=now)
        await make_video(db, c, hashtags=["old"], created_at=now - timedelta(days=60))
    tags = (await client.get("/api/v1/hashtags/suggest", params={"authenticated": "false"})).json()
    names = [t["tag"] for t in tags]
    assert "wanochallenge" in names and "secret" not in names and "old" not in names
    assert len(tags) <= 20


async def test_HT4_garbage_queries_are_safe(client, db):
    for q in (".*", "$where", "a" * 500, "((((", "#", "##", "😀", "%00", "\\"):
        r = await client.get("/api/v1/hashtags/suggest", params={"q": q[:200], "authenticated": "false"})
        assert r.status_code in (200, 422), q
        if r.status_code == 200:
            assert isinstance(r.json(), list) and len(r.json()) <= 20
    assert (await client.get("/api/v1/hashtags/suggest", params={"q": "x", "limit": 999})).status_code == 422


async def test_HT5_ideas_are_prompts_with_a_hashtag_in_each_language(client, db):
    for lang in ("en", "fr", "pt", "sw"):
        ideas = (await client.get("/api/v1/hashtags/ideas", params={"lang": lang, "authenticated": "false"})).json()
        assert 5 <= len(ideas) <= 12
        assert all(i["text"] and i["hashtag"] and " " not in i["hashtag"] for i in ideas)
    other = (await client.get("/api/v1/hashtags/ideas", params={"lang": "zz", "authenticated": "false"})).json()
    assert other   # unknown language falls back to English


async def test_HT6_hashtags_switch_exists_and_is_off_by_default(client, db):
    f = (await client.get("/api/v1/config/features")).json()
    assert f["hashtags"] is False


# ---------------------------------------------------------------- round 2

async def test_R2_1_scheduled_edited_reply_counts_only_when_it_goes_live(client, db, stream_not_ready):
    from app.services import stitch_jobs
    c, _ = await make_user(db)
    _, uh = await make_user(db)
    target = await make_video(db, c)
    at = (datetime.utcnow() + timedelta(hours=3)).isoformat()
    r = await client.post("/api/v1/videos/stitch", headers=uh,
                          json=_stitch_body(reply_to=target, scheduled_at=at, timezone="Africa/Kampala"))
    assert r.status_code == 202, r.text
    job = await db.pending_videos.find_one({})
    await stitch_jobs._create_post(job, {"file_url": CF.format(secrets.token_hex(16)), "duration": 5})
    assert (await db.videos.find_one({"_id": ObjectId(target)})).get("remix_count", 0) == 0


async def test_R2_2_trending_merges_cases_and_ignores_junk(client, db):
    c, _ = await make_user(db)
    now = datetime.utcnow()
    for t in (["Kampala"], ["kampala"], ["KAMPALA"], [None, 5, {"x": 1}], "notalist"):
        await make_video(db, c, hashtags=t, created_at=now)
    tags = (await client.get("/api/v1/hashtags/suggest", params={"q": "kamp", "authenticated": "false"})).json()
    trending = [t for t in tags if t["source"] == "trending"]
    assert len(trending) == 1 and trending[0]["posts"] == 3
    assert not any(t["tag"].lower() == "kampala" and t["source"] == "library" for t in tags)   # no duplicate


async def test_R2_3_reply_link_hidden_when_the_original_creator_is_banned(client, db):
    c, _ = await make_user(db, username="gone")
    _, uh = await make_user(db)
    target = await make_video(db, c)
    rid = (await _post(client, uh, reply_to=target)).json()["video_id"]
    await db.videos.update_one({"_id": ObjectId(rid)}, {"$set": {"isReadyToStream": True}})
    await db.users.update_one({"_id": ObjectId(c)}, {"$set": {"is_active": False}})
    reply = next(v for v in await _feed(client) if v["id"] == rid)
    assert reply.get("reply_to") is None


async def test_R2_4_a_broken_reply_lookup_never_breaks_the_feed(client, db, monkeypatch):
    from app.services import replies
    c, _ = await make_user(db)
    await make_video(db, c)

    async def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(replies, "feed_links", boom)
    feed = await _feed(client)
    assert feed and feed[0]["replies_count"] == 0 and feed[0]["reply_to"] is None


async def test_R2_5_many_posts_and_tags_stay_fast(client, db):
    import time
    c, _ = await make_user(db)
    now = datetime.utcnow()
    await db.videos.insert_many([{"creator_id": ObjectId(c), "is_active": True, "privacy": "public",
                                  "created_at": now, "hashtags": [f"tag{i % 300}", "wano"]} for i in range(3000)])
    t0 = time.monotonic()
    tags = (await client.get("/api/v1/hashtags/suggest", params={"authenticated": "false"})).json()
    first = time.monotonic() - t0
    t0 = time.monotonic()
    await client.get("/api/v1/hashtags/suggest", params={"q": "tag1", "authenticated": "false"})
    cached = time.monotonic() - t0
    assert tags[0]["tag"] == "wano" and first < 5 and cached < 1


# ---------------------------------------------------------------- round 3 (deep review)

async def test_R3_1_page_of_banned_replies_doesnt_end_the_list_early(client, db):
    c, ch = await make_user(db, username="amina")
    target = await make_video(db, c)
    good, gh = await make_user(db, username="good")
    rid = (await _post(client, gh, reply_to=target)).json()["video_id"]
    for _ in range(5):
        b, bh = await make_user(db)
        assert (await _post(client, bh, reply_to=target)).status_code == 201
        await db.users.update_one({"_id": ObjectId(b)}, {"$set": {"is_active": False}})
    r = await client.get(f"/api/v1/videos/{target}/replies", params={"limit": 3, "authenticated": "false"})
    body = r.json()
    assert [i["id"] for i in body["items"]] == [rid] and body["next_cursor"] is None


async def test_R3_2_blocked_people_dont_show_in_my_replies_list(client, db):
    c, ch = await make_user(db, username="amina")
    target = await make_video(db, c)
    troll, th = await make_user(db, username="troll")
    assert (await _post(client, th, reply_to=target)).status_code == 201
    viewer, vh = await make_user(db, username="viewer")
    assert (await client.post(f"/api/v1/users/block_user/{troll}", headers=vh)).status_code == 204
    mine = (await client.get(f"/api/v1/videos/{target}/replies", headers=vh)).json()["items"]
    assert mine == []
    anon = (await client.get(f"/api/v1/videos/{target}/replies", params={"authenticated": "false"})).json()["items"]
    assert len(anon) == 1
