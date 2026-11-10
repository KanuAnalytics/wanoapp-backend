"""Livelier feeds (feed_fx): local reactions and Trending as a chart. Attacks
written with the code."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
R = "/api/v1/videos/{}/reactions"


async def _on(db, on=True):
    await db.config.insert_one({"features": {"feed_fx": on}})


async def _local(db, city="kampala", **extra):
    return await make_user(db, date_of_birth=datetime(1990, 1, 1), localization={"country": "ug"},
                           home_ground={"city": city, "set_at": datetime.utcnow()}, **extra)


def _hour():
    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)


async def _trend(db, vid, h_ago=1, **counts):
    stats = {"views": 50, "finished": 30, "likes": 20, **counts}
    await db.video_stats_hourly.insert_one({"_id": f"{vid}:{h_ago}", "video_id": ObjectId(vid),
                                            "hour": _hour() - timedelta(hours=h_ago), **stats})


def _fresh():
    from app.services import trending
    trending.clear_cache()


# ------------------------------------------------------------------ reactions

async def test_FX1_switch_off_everything_404s(client, db):
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    _, h = await _local(db)
    assert (await client.get(R.format(vid))).status_code == 404
    assert (await client.post(R.format(vid), json={"kind": "eish"}, headers=h)).status_code == 404
    assert (await client.get("/api/v1/feed/trending-board")).status_code == 404
    assert (await client.get("/api/v1/feed/trending-countries")).status_code == 404
    assert (await client.get("/api/v1/config/features")).json()["feed_fx"] is False


async def test_FX2_react_counts_once_per_kind_and_city_shows(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    _, h = await _local(db)
    _, h2 = await _local(db)
    for _ in range(3):   # repeat taps don't stack
        r = await client.post(R.format(vid), json={"kind": "plus_one"}, headers=h)
        assert r.status_code == 200, r.text
    await client.post(R.format(vid), json={"kind": "eish"}, headers=h)
    r = (await client.post(R.format(vid), json={"kind": "plus_one"}, headers=h2)).json()
    assert r["counts"]["plus_one"] == 2 and r["counts"]["eish"] == 1
    assert r["city"] == {"id": "kampala", "name": "Kampala", "count": 3}
    assert r["mine"] == ["plus_one"]
    # taking it back
    r = (await client.post(R.format(vid), json={"kind": "plus_one", "on": False}, headers=h2)).json()
    assert r["counts"]["plus_one"] == 1 and r["mine"] == [] and r["city"]["count"] == 2
    # taking back twice never goes negative
    for _ in range(3):
        await client.post(R.format(vid), json={"kind": "plus_one", "on": False}, headers=h2)
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["reactions"]["plus_one"] == 1 and v["city_reactions"]["kampala"] == 2
    # logged out can read, can't react
    anon = (await client.get(R.format(vid))).json()
    assert anon["counts"]["plus_one"] == 1 and anon["mine"] == [] and anon["city"] is None
    assert (await client.post(R.format(vid), json={"kind": "eish"})).status_code == 401


async def test_FX3_hostile_bodies_and_ids(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    _, h = await _local(db)
    for bad in [{"kind": "love"}, {"kind": {"$ne": 1}}, {"kind": "eish", "on": "yes"},
                {"kind": "eish", "extra": 1}, {}, {"kind": "reactions.plus_one"}]:
        assert (await client.post(R.format(vid), json=bad, headers=h)).status_code == 422, bad
    for bad_id in ["junk", str(ObjectId()), "$where"]:
        assert (await client.post(R.format(bad_id), json={"kind": "eish"}, headers=h)).status_code == 404
        assert (await client.get(R.format(bad_id))).status_code == 404


async def test_FX4_private_deleted_and_blocked_posts(client, db):
    await _on(db)
    c, ch = await make_user(db)
    me, h = await _local(db)
    private = await make_video(db, c, privacy="private")
    gone = await make_video(db, c, is_active=False)
    assert (await client.post(R.format(private), json={"kind": "eish"}, headers=h)).status_code == 404
    assert (await client.post(R.format(gone), json={"kind": "eish"}, headers=h)).status_code == 404
    # the owner can react to their own private post
    own = await make_video(db, me, privacy="private")
    assert (await client.post(R.format(own), json={"kind": "fire"}, headers=h)).status_code == 200
    pub = await make_video(db, c)
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"blocked_by": [ObjectId(c)]}})
    assert (await client.post(R.format(pub), json={"kind": "eish"}, headers=h)).status_code == 404


async def test_FX5_plus_one_needs_a_city_and_minors_never_count_for_a_city(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    _, no_city = await make_user(db)
    r = await client.post(R.format(vid), json={"kind": "plus_one"}, headers=no_city)
    assert r.status_code == 409 and "Home Ground" in r.json()["detail"]
    assert (await client.post(R.format(vid), json={"kind": "eish"}, headers=no_city)).status_code == 200
    _, kid = await make_user(db, date_of_birth=datetime.utcnow() - timedelta(days=365 * 14),
                             home_ground={"city": "kampala", "set_at": datetime.utcnow()})
    assert (await client.post(R.format(vid), json={"kind": "plus_one"}, headers=kid)).status_code == 409
    r = (await client.post(R.format(vid), json={"kind": "ayaa"}, headers=kid)).json()
    assert r["city"] is None
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert not (v.get("city_reactions") or {})
    # a made-up city in the profile counts for nothing
    _, fake = await make_user(db, date_of_birth=datetime(1990, 1, 1), home_ground={"city": "atlantis"})
    assert (await client.post(R.format(vid), json={"kind": "plus_one"}, headers=fake)).status_code == 409


async def test_FX6_daily_cap_and_parallel_taps(client, db):
    await _on(db)
    from app.services import feed_fx
    c, _ = await make_user(db)
    _, h = await _local(db)
    vids = [await make_video(db, c) for _ in range(4)]
    feed_fx.REACTIONS_PER_DAY, old = 3, feed_fx.REACTIONS_PER_DAY
    try:
        codes = [(await client.post(R.format(v), json={"kind": "fire"}, headers=h)).status_code for v in vids]
        assert codes == [200, 200, 200, 429]
    finally:
        feed_fx.REACTIONS_PER_DAY = old
    # 10 taps at once on one post still count once
    v = await make_video(db, c)
    await asyncio.gather(*[client.post(R.format(v), json={"kind": "eish"}, headers=h) for _ in range(10)])
    assert (await db.videos.find_one({"_id": ObjectId(v)}))["reactions"]["eish"] == 1


async def test_FX7_reactions_dont_move_trending(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    for _ in range(5):
        _, h = await _local(db)
        await client.post(R.format(vid), json={"kind": "fire"}, headers=h)
    assert await db.video_stats_hourly.count_documents({"video_id": ObjectId(vid)}) == 0


# ------------------------------------------------------------------ trending board

async def test_FX8_board_ranks_strictly_per_country_with_views(client, db):
    await _on(db)
    _fresh()
    ug, _ = await make_user(db)
    ke, _ = await make_user(db)
    a = await make_video(db, ug, country="ug", isReadyToStream=True)
    b = await make_video(db, ug, country="ug", isReadyToStream=True)
    k = await make_video(db, ke, country="ke", isReadyToStream=True)
    await _trend(db, a, likes=40)
    await _trend(db, b, likes=10)
    await _trend(db, k, likes=90)
    await db.video_stats_hourly.insert_one({"_id": f"{a}:now", "video_id": ObjectId(a), "hour": _hour(),
                                            "views": 7, "anon_views": 3})
    rows = (await client.get("/api/v1/feed/trending-board", params={"country": "ug"})).json()
    assert [r["id"] for r in rows] == [a, b]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["views_last_hour"] >= 10
    assert rows[0]["rank_change"] is None           # no ranks from a day ago yet
    assert rows[0]["user"] is not None               # a full feed item
    allr = (await client.get("/api/v1/feed/trending-board")).json()
    assert [r["id"] for r in allr][:3] == [k, a, b]
    c = (await client.get("/api/v1/feed/trending-countries")).json()["items"]
    assert c[0] == {"code": "ug", "name": "Uganda", "count": 2} and c[1]["code"] == "ke"


async def test_FX9_movement_since_a_day_ago(client, db):
    await _on(db)
    _fresh()
    u, _ = await make_user(db)
    a = await make_video(db, u, country="ug", isReadyToStream=True)
    b = await make_video(db, u, country="ug", isReadyToStream=True)
    n = await make_video(db, u, country="ug", isReadyToStream=True)
    await _trend(db, a, likes=50)
    await _trend(db, b, likes=10)
    await _trend(db, n, likes=5)
    await db.trending_snapshots.insert_one({"_id": "ug:old", "scope": "ug",
                                            "at": datetime.utcnow() - timedelta(hours=24),
                                            "ranks": {b: 1, a: 9}})
    rows = {r["id"]: r for r in (await client.get("/api/v1/feed/trending-board",
                                                  params={"country": "ug"})).json()}
    assert rows[a]["rank"] == 1 and rows[a]["rank_change"] == 8      # up 8 places
    assert rows[b]["rank_change"] == -1                                # down 1
    assert rows[n]["rank_change"] == "new"
    # one snapshot per hour, however many times the board is opened
    for _ in range(3):
        await client.get("/api/v1/feed/trending-board", params={"country": "ug"})
    assert await db.trending_snapshots.count_documents({"scope": "ug", "_id": {"$ne": "ug:old"}}) == 1


async def test_FX10_board_hides_what_cant_be_shown_and_rejects_junk(client, db):
    await _on(db)
    _fresh()
    u, _ = await make_user(db)
    banned, _ = await make_user(db, is_active=False)
    ok = await make_video(db, u, country="ug", isReadyToStream=True)
    priv = await make_video(db, u, country="ug", isReadyToStream=True)
    bad = await make_video(db, banned, country="ug", isReadyToStream=True)
    for v in (ok, priv, bad):
        await _trend(db, v)
    await db.videos.update_one({"_id": ObjectId(priv)}, {"$set": {"privacy": "private"}})
    rows = (await client.get("/api/v1/feed/trending-board", params={"country": "ug"})).json()
    assert [r["id"] for r in rows] == [ok] and rows[0]["rank"] == 1
    for bad_q in [{"country": "u1"}, {"country": "uga"}, {"country": "$n"}, {"limit": 500}, {"skip": -1}]:
        assert (await client.get("/api/v1/feed/trending-board", params=bad_q)).status_code == 422, bad_q
    assert (await client.get("/api/v1/feed/trending-board", params={"country": "zz"})).json() == []
    # paging
    p2 = (await client.get("/api/v1/feed/trending-board", params={"country": "ug", "skip": 1})).json()
    assert p2 == []


async def test_FX11_other_feeds_unchanged(client, db):
    await _on(db)
    _, h = await make_user(db)
    r = await client.get("/api/v1/feed/", params={"authenticated": "false", "limit": 5})
    assert r.status_code == 200
    for v in r.json():
        assert v.get("rank") is None


# ------------------------------------------------------------------ top comment

async def _comment(db, vid, uid, text, likes=0, **extra):
    r = await db.comments.insert_one({"video_id": ObjectId(vid), "user_id": ObjectId(uid), "parent_id": None,
                                      "content": text, "likes_count": likes, "is_active": True,
                                      "created_at": datetime.utcnow(), **extra})
    return str(r.inserted_id)


async def test_FX12_top_comment_is_pinned_or_most_liked_and_safe(client, db):
    await _on(db)
    c, _ = await make_user(db)
    vid = await make_video(db, c)
    a, _ = await make_user(db, display_name="Amina")
    b, _ = await make_user(db, display_name="Kofi")
    banned, _ = await make_user(db, display_name="Spam", is_active=False)
    T = f"/api/v1/videos/{vid}/top-comment"
    assert (await client.get(T)).json() == {"comment": None}
    await _comment(db, vid, a, "nice", likes=2)
    best = await _comment(db, vid, b, "this is Kampala!", likes=9)
    await _comment(db, vid, banned, "buy followers", likes=999)
    await _comment(db, vid, a, "deleted", likes=500, is_active=False)
    await _comment(db, vid, a, "   ", likes=300)
    got = (await client.get(T)).json()["comment"]
    assert got["id"] == best and got["user"]["name"] == "Kofi" and got["likes"] == 9
    pinned = await _comment(db, vid, a, "pinned one", likes=0, is_pinned=True)
    assert (await client.get(T)).json()["comment"]["id"] == pinned
    # someone who blocked Amina doesn't see her comment
    _, h = await make_user(db, blocked_users=[ObjectId(a)])
    assert (await client.get(T, headers=h)).json()["comment"]["id"] == best
    # long text is cut, comments off shows nothing, private post is hidden
    await db.comments.update_one({"_id": ObjectId(pinned)}, {"$set": {"content": "x" * 900}})
    assert len((await client.get(T)).json()["comment"]["text"]) == 140
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"comments_enabled": False}})
    assert (await client.get(T)).json() == {"comment": None}
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"privacy": "private"}})
    assert (await client.get(T)).status_code == 404
    assert (await client.get("/api/v1/videos/junk/top-comment")).status_code == 404


# ------------------------------------------------------------------ whose city

async def test_FX13_post_shows_the_creators_city_and_the_city_that_reacted_most(client, db):
    """A viewer whose own Home Ground is Accra sees where the post is FROM
    (the creator's Kampala) and which city reacted most, not just Accra."""
    await _on(db)
    c, _ = await _local(db, city="kampala")
    vid = await make_video(db, c)
    _, accra = await _local(db, city="accra")
    for _ in range(2):
        _, k = await _local(db, city="kampala")
        await client.post(R.format(vid), json={"kind": "fire"}, headers=k)
    for _ in range(3):
        _, n = await _local(db, city="nairobi")
        await client.post(R.format(vid), json={"kind": "eish"}, headers=n)
    r = (await client.get(R.format(vid), headers=accra)).json()
    assert r["creator_city"] == {"id": "kampala", "name": "Kampala", "count": 2}
    assert r["top_city"] == {"id": "nairobi", "name": "Nairobi", "count": 3}
    assert r["city"] == {"id": "accra", "name": "Accra", "count": 0}
    anon = (await client.get(R.format(vid))).json()   # same for signed-out viewers
    assert anon["creator_city"]["name"] == "Kampala" and anon["top_city"]["name"] == "Nairobi" and anon["city"] is None


async def test_FX14_no_city_for_minor_creators_creators_without_one_or_junk_counts(client, db):
    await _on(db)
    kid, _ = await make_user(db, date_of_birth=datetime.utcnow() - timedelta(days=365 * 15),
                             home_ground={"city": "kampala"})
    none_, _ = await make_user(db)
    for creator in (kid, none_):
        vid = await make_video(db, creator)
        await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"city_reactions": {
            "atlantis": 50, "kampala": "9", "lagos": True, "nairobi": -2}}})
        r = (await client.get(R.format(vid))).json()
        assert r["creator_city"] is None and r["top_city"] is None


async def test_FX15_admin_resets_someones_home_ground(client, db):
    _, adh = await make_user(db, user_type="admin")
    uid, uh = await _local(db, city="accra", username="eilmer")
    _, plain = await make_user(db)
    assert (await client.post("/api/v1/admin/home-ground/reset", json={"username": "eilmer"},
                              headers=plain)).status_code in (401, 403)
    assert (await client.post("/api/v1/admin/home-ground/reset", json={"username": "nobody"},
                              headers=adh)).status_code == 404
    assert (await client.post("/api/v1/admin/home-ground/reset", json={"username": {"$ne": 1}},
                              headers=adh)).status_code == 422
    r = await client.post("/api/v1/admin/home-ground/reset", json={"username": "@eilmer"}, headers=adh)
    assert r.status_code == 200 and r.json() == {"reset": True, "was": "accra"}
    assert "home_ground" not in await db.users.find_one({"_id": ObjectId(uid)})
    # Free to pick again straight away.
    await db.config.insert_one({"features": {"home_ground": True}})
    r = await client.put("/api/v1/home-ground/me", json={"city_id": "kampala"}, headers=uh)
    assert r.status_code == 200, r.text
