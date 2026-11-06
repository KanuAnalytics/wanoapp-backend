"""Attacks on creator growth: first viewers for every new post, posting streaks, creator stats."""

import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio

STATS = "/api/v1/creators/me/stats"


async def _fresh_served(db):
    """video id -> number of For You impressions it got as a 'fresh' pick."""
    c = Counter()
    async for imp in db.feed_impressions.find({"source": "fresh"}, {"video_id": 1}):
        c[str(imp["video_id"])] += 1
    return c


async def _reach(db, vid):
    return await db.video_reach.find_one({"_id": ObjectId(vid)}) or {}


async def _viewers(db, n):
    return [await make_user(db) for _ in range(n)]


async def _watch(client, vid, viewers, portion):
    for _, h in viewers:
        r = await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": portion})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------- first viewers

async def test_F1_every_new_post_gets_first_viewers(client, db, rb):
    _, ids = await _catalog(db, n=60, creators=12)
    rb.pool = ids
    new = []
    for i in range(120):
        c, _ = await make_user(db)
        new.append(await make_video(db, c, created_at=ago(minutes=5 + i * 3), isReadyToStream=True))
    for _, h in await _viewers(db, 60):
        assert (await _feed(client, h, limit=20)).status_code == 200
    served = await _fresh_served(db)
    starved = [v for v in new if served[v] == 0]
    assert not starved, f"{len(starved)} of 120 new posts never got a single viewer"


async def test_F2_longest_waiting_with_fewest_viewers_goes_first(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    waited = await make_video(db, a, created_at=ago(hours=30), isReadyToStream=True)
    seen_enough = await make_video(db, b, created_at=ago(hours=1), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(seen_enough), "impressions": 25, "views": 0})
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    fresh = [v["id"] for v in page if v.get("source") == "fresh"]
    assert fresh and fresh[0] == waited, fresh


async def test_F3_post_people_skip_leaves_the_pool_after_its_test(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    flop = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(flop), "impressions": 60, "fy_views": 40,
                                     "fy_portion_sum": 2.0, "fy_finished": 0})
    for _, h in await _viewers(db, 5):
        page = (await _feed(client, h, limit=10)).json()
        assert flop not in [v["id"] for v in page if v.get("source") == "fresh"]


async def test_F4_post_people_love_keeps_getting_shown(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    hit = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(hit), "impressions": 60, "fy_views": 40,
                                     "fy_portion_sum": 34.0, "fy_finished": 28})
    hits = 0
    for _, h in await _viewers(db, 5):
        page = (await _feed(client, h, limit=10)).json()
        hits += hit in [v["id"] for v in page if v.get("source") == "fresh"]
    assert hits >= 4


async def test_F4_even_a_hit_stops_at_the_cap(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    hit = await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(hit), "impressions": 8000, "fy_views": 5000,
                                     "fy_portion_sum": 4500.0, "fy_finished": 4000})
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    assert hit not in [v["id"] for v in page if v.get("source") == "fresh"]


async def test_F5_one_creator_flooding_cannot_eat_the_first_viewers(client, db, rb):
    _, ids = await _catalog(db, n=60, creators=12)
    rb.pool = ids
    flooder, _ = await make_user(db)
    flood = [await make_video(db, flooder, created_at=ago(minutes=i), isReadyToStream=True) for i in range(40)]
    others = []
    for i in range(20):
        c, _ = await make_user(db)
        others.append(await make_video(db, c, created_at=ago(minutes=50 + i), isReadyToStream=True))
    for _, h in await _viewers(db, 30):
        await _feed(client, h, limit=20)
    served = await _fresh_served(db)
    assert len([v for v in flood if served[v]]) <= 3, "flooder got more than 3 posts tested"
    assert all(served[v] for v in others)


async def test_F6_viewer_never_gets_their_own_post_or_a_watched_one(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    me, h = await make_user(db)
    mine = await make_video(db, me, created_at=ago(minutes=5), isReadyToStream=True)
    c, _ = await make_user(db)
    seen = await make_video(db, c, created_at=ago(minutes=6), isReadyToStream=True)
    await client.post(f"/api/v1/videos/{seen}/watch", headers=h, json={"portion": 1})
    page = [v["id"] for v in (await _feed(client, h, limit=20)).json()]
    assert mine not in page and seen not in page


async def test_F7_replayed_or_self_views_do_not_inflate_reach(client, db, rb):
    creator, ch = await make_user(db)
    vid = await make_video(db, creator, created_at=ago(minutes=5), isReadyToStream=True)
    _, h = await make_user(db)
    for _ in range(30):
        await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": 1})
        await client.post(f"/api/v1/videos/{vid}/watch", headers=ch, json={"portion": 1})
    r = await _reach(db, vid)
    assert r.get("views") == 1 and r.get("finished") == 1, r


async def test_F8_real_watches_build_reach(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True)
    await _watch(client, vid, await _viewers(db, 6), 1.0)
    await _watch(client, vid, await _viewers(db, 4), 0.1)
    r = await _reach(db, vid)
    assert r["views"] == 10 and r["finished"] == 6 and abs(r["portion_sum"] - 6.4) < 1e-6


async def test_F9_impressions_only_from_for_you_pages(client, db, rb):
    _, ids = await _catalog(db, n=20, creators=5)
    rb.pool = ids
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True)
    _, h = await make_user(db)
    await client.get("/api/v1/feed/?cursor=start&limit=20", headers=h)     # New tab: no impression
    assert (await _reach(db, vid)).get("impressions", 0) == 0
    await _feed(client, h, limit=20)
    assert (await _reach(db, vid)).get("impressions", 0) >= 1


async def test_F10_old_posts_are_not_first_viewer_candidates(client, db, rb):
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    old = await make_video(db, c, created_at=ago(days=4), isReadyToStream=True, views_count=0)
    for _, h in await _viewers(db, 5):
        page = (await _feed(client, h, limit=20)).json()
        assert old not in [v["id"] for v in page if v.get("source") == "fresh"]


# ---------------------------------------------------------------- streaks

async def _post_days(db, creator, days_ago, **extra):
    for d in days_ago:
        day = datetime.now(timezone.utc).replace(hour=0, minute=30) - timedelta(days=d)
        await make_video(db, creator, created_at=day, **{"isReadyToStream": True, **extra})


async def test_K1_streak_counts_days_not_posts(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    await _post_days(db, c, [0, 0, 0, 0, 0, 1, 2, 4])
    s = (await creators.streaks(db, [ObjectId(c)]))[ObjectId(c)]
    assert s["current"] == 3 and s["posted_today"] is True


async def test_K2_streak_survives_until_the_day_ends(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    await _post_days(db, c, [1, 2, 3, 4])        # nothing yet today
    s = (await creators.streaks(db, [ObjectId(c)]))[ObjectId(c)]
    assert s["current"] == 4 and s["posted_today"] is False


async def test_K3_hidden_posts_do_not_count(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    await _post_days(db, c, [0], privacy="private")
    await _post_days(db, c, [1], is_active=False)
    await _post_days(db, c, [2], isReadyToStream=False)
    await _post_days(db, c, [3], status="scheduled")
    s = (await creators.streaks(db, [ObjectId(c)]))[ObjectId(c)]
    assert s["current"] == 0


async def test_K4_streak_creator_gets_a_bigger_test_and_ranks_higher(client, db, rb):
    from app.services import creators
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    steady, _ = await make_user(db)
    await _post_days(db, steady, [1, 2, 3, 4])
    s_vid = await make_video(db, steady, created_at=ago(hours=2), isReadyToStream=True)
    casual, _ = await make_user(db)
    c_vid = await make_video(db, casual, created_at=ago(hours=2), isReadyToStream=True)
    same = {"impressions": 50, "fy_views": 35, "fy_portion_sum": 21.0, "fy_finished": 15}
    await db.video_reach.insert_many([{"_id": ObjectId(s_vid), **same}, {"_id": ObjectId(c_vid), **same}])
    pool = await creators.first_viewers(db, [], None)
    order = [str(d["_id"]) for d in pool]
    assert s_vid in order and c_vid in order and order.index(s_vid) < order.index(c_vid)
    assert creators.test_audience(4) > creators.test_audience(0)
    # 35 impressions: still in test for the streak creator, past it for the casual one.
    by_id = {str(d["_id"]): d for d in pool}
    assert by_id[s_vid]["stage"] == "test" and by_id[c_vid]["stage"] == "boost"


async def test_K5_streak_lookups_stay_bounded(client, db, rb, monkeypatch):
    """One grouped query for any number of creators, then cached."""
    from app.services import creators
    cs = [ObjectId((await make_user(db))[0]) for _ in range(80)]
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": cs[i % 80], "privacy": "public", "is_active": True,
                                  "isReadyToStream": True, "created_at": now - timedelta(minutes=i * 7)}
                                 for i in range(12000)])           # everyone posts daily for ~58 days
    calls = []
    real = type(db.videos).aggregate

    def counting(self, *a, **k):
        if self.name == "videos":
            calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(type(db.videos), "aggregate", counting)
    t = time.perf_counter()
    s = await creators.streaks(db, cs)
    assert len(calls) == 1 and time.perf_counter() - t < 5
    assert all(57 <= v["current"] <= 60 for v in s.values()), sorted(v["current"] for v in s.values())[:3]
    calls.clear()
    await creators.streaks(db, cs)
    assert not calls, "second lookup wasn't cached"


# ---------------------------------------------------------------- creator stats

async def test_S1_stats_need_login(client, db, rb):
    assert (await client.get(STATS)).status_code == 401
    assert (await client.get(STATS + "?authenticated=false")).status_code in (401, 403, 422)


async def test_S2_stats_add_up(client, db, rb):
    me, mh = await make_user(db)
    v1 = await make_video(db, me, created_at=ago(days=1), isReadyToStream=True, title="one")
    v2 = await make_video(db, me, created_at=ago(hours=3), isReadyToStream=True, title="two")
    fans = await _viewers(db, 5)
    await _watch(client, v1, fans, 1.0)
    await _watch(client, v2, fans[:2], 0.5)
    for _, h in fans[:3]:
        await client.post(f"/api/v1/videos/{v1}/like", headers=h)
    await client.post("/api/v1/", headers=fans[0][1], json={"video_id": v1, "content": "nice"})
    await client.post(f"/api/v1/videos/{v1}/share", headers=fans[1][1])
    r = await client.get(STATS + "?days=7", headers=mh)
    assert r.status_code == 200, r.text
    body = r.json()
    t = body["totals"]
    assert t["views"] == 7 and t["finished"] == 5 and t["likes"] == 3 and t["comments"] == 1 and t["shares"] == 1
    assert t["posts"] == 2 and abs(t["avg_watch_pct"] - round(100 * 6.0 / 7, 1)) < 0.11
    posts = {p["id"]: p for p in body["posts"]}
    assert posts[v1]["views"] == 5 and posts[v1]["finish_rate_pct"] == 100.0
    assert posts[v2]["views"] == 2 and posts[v2]["avg_watch_pct"] == 50.0


async def test_S3_growth_vs_previous_period(client, db, rb):
    me, mh = await make_user(db)
    today = datetime.now(timezone.utc).date()
    rows = []
    for d in range(14):
        day = today - timedelta(days=d)
        rows.append({"_id": f"{me}:{day.isoformat()}", "creator_id": ObjectId(me),
                     "day": datetime(day.year, day.month, day.day), "views": 20 if d < 7 else 10})
    await db.creator_daily.insert_many(rows)
    body = (await client.get(STATS + "?days=7", headers=mh)).json()
    assert body["totals"]["views"] == 140 and body["totals"]["previous_views"] == 70
    assert body["totals"]["views_change_pct"] == 100.0


async def test_S4_like_toggling_and_spam_count_once(client, db, rb):
    me, mh = await make_user(db)
    vid = await make_video(db, me, created_at=ago(hours=1), isReadyToStream=True)
    _, h = await make_user(db)
    for _ in range(20):
        await client.post(f"/api/v1/videos/{vid}/like", headers=h)
        await client.delete(f"/api/v1/videos/{vid}/like", headers=h)
        await client.post("/api/v1/", headers=h, json={"video_id": vid, "content": "spam"})
        await client.post(f"/api/v1/videos/{vid}/share", headers=h)
    t = (await client.get(STATS, headers=mh)).json()["totals"]
    assert t["likes"] <= 1 and t["comments"] <= 1 and t["shares"] <= 1


async def test_S5_my_own_engagement_is_not_counted(client, db, rb):
    me, mh = await make_user(db)
    vid = await make_video(db, me, created_at=ago(hours=1), isReadyToStream=True)
    await client.post(f"/api/v1/videos/{vid}/watch", headers=mh, json={"portion": 1})
    await client.post(f"/api/v1/videos/{vid}/like", headers=mh)
    await client.post(f"/api/v1/videos/{vid}/share", headers=mh)
    t = (await client.get(STATS, headers=mh)).json()["totals"]
    assert t["views"] == 0 and t["likes"] == 0 and t["shares"] == 0


async def test_S6_streak_in_stats(client, db, rb):
    me, mh = await make_user(db)
    await _post_days(db, me, [0, 1, 2])
    s = (await client.get(STATS, headers=mh)).json()["streak"]
    assert s == {"current": 3, "posted_today": True, "boost_active": True, "next_milestone": 7}


@pytest.mark.parametrize("days", ["0", "31", "-1", "abc", "1e9"])
async def test_S7_bad_period_rejected(client, db, rb, days):
    _, mh = await make_user(db)
    assert (await client.get(f"{STATS}?days={days}", headers=mh)).status_code == 422


async def test_S8_only_my_posts_and_nothing_about_viewers(client, db, rb):
    me, mh = await make_user(db)
    other, _ = await make_user(db)
    mine = await make_video(db, me, created_at=ago(hours=1), isReadyToStream=True)
    theirs = await make_video(db, other, created_at=ago(hours=1), isReadyToStream=True)
    gone = await make_video(db, me, created_at=ago(hours=1), isReadyToStream=True, is_active=False)
    fan_id, fh = await make_user(db, username="secretfan")
    await client.post(f"/api/v1/videos/{mine}/watch", headers=fh, json={"portion": 1})
    r = await client.get(STATS, headers=mh)
    ids = [p["id"] for p in r.json()["posts"]]
    assert mine in ids and theirs not in ids and gone not in ids
    assert fan_id not in r.text and "secretfan" not in r.text


async def test_S9_big_creator_stats_are_fast(client, db, rb):
    me, mh = await make_user(db)
    now = datetime.now(timezone.utc)
    res = await db.videos.insert_many([{"creator_id": ObjectId(me), "privacy": "public", "is_active": True,
                                        "isReadyToStream": True, "created_at": now - timedelta(minutes=i * 7),
                                        "views_count": i} for i in range(3000)])
    await db.video_reach.insert_many([{"_id": oid, "views": 3, "portion_sum": 2.0, "finished": 1}
                                      for oid in res.inserted_ids])
    t = time.perf_counter()
    r = await client.get(STATS + "?days=30", headers=mh)
    assert r.status_code == 200 and time.perf_counter() - t < 3
    assert len(r.json()["posts"]) <= 20


async def test_S10_odd_documents_do_not_break_stats(client, db, rb):
    me, mh = await make_user(db)
    vid = await make_video(db, me, created_at=ago(hours=1), isReadyToStream=True, title=None, urls=None)
    await db.video_reach.insert_one({"_id": ObjectId(vid), "views": "lots", "portion_sum": None, "finished": -4})
    await db.creator_daily.insert_one({"_id": f"{me}:junk", "creator_id": ObjectId(me), "day": "yesterday",
                                       "views": "x"})
    r = await client.get(STATS, headers=mh)
    assert r.status_code == 200, r.text
