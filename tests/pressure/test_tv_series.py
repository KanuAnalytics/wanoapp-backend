"""Wano TV: docuseries (seasons and episodes), credits linked to Wano profiles,
and popularity from real plays. Attacks written before the code."""

import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401
from tests.pressure.test_tv_protect import CDN, MASTER, VARIANT, _title, spaces  # noqa: F401

pytestmark = pytest.mark.asyncio


def _ep(folder, s, e, **extra):
    return {"id": f"s{s}e{e}", "season": s, "number": e, "title": f"Episode {e}", "synopsis": "x",
            "duration_minutes": 20, "thumbnail": f"{CDN}/wano-tv/{folder}/s{s}e{e}.jpg",
            "video_url": f"{CDN}/wano-tv/{folder}/s{s}e{e}/master.m3u8", **extra}


async def _series(db, folder="roots", seasons=2, per=3, **extra):
    eps = {s: [_ep(folder, s, e) for e in range(per, 0, -1)] for s in range(seasons, 0, -1)}   # stored out of order
    doc = {"type": "series", "seasons": [{"number": s, "title": f"Season {s}", "episodes": eps[s]} for s in eps]}
    doc.pop("video_url", None)
    return await _title(db, folder, video_url=None, **doc, **extra)


async def _titles(client, h=None, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = await client.get(f"/api/v1/tv/titles?{q}" if q else "/api/v1/tv/titles",
                         headers=h or {}, params=None if h else {"authenticated": "false"})
    assert r.status_code == 200, r.text
    return {t["slug"]: t for t in r.json()}


# ---------------------------------------------------------------- docuseries

async def test_S1_series_lists_seasons_and_episodes_in_order_with_protected_links(client, db, rb, spaces):
    await _series(db)
    t = (await _titles(client))["roots"]
    assert t["type"] == "series"
    assert [s["number"] for s in t["seasons"]] == [1, 2]
    assert [e["id"] for e in t["seasons"][0]["episodes"]] == ["s1e1", "s1e2", "s1e3"]
    for s in t["seasons"]:
        for e in s["episodes"]:
            assert "/api/v1/tv/stream/" in e["video_url"] and "digitaloceanspaces" not in e["video_url"]
    assert t["video_url"] == t["seasons"][0]["episodes"][0]["video_url"]     # play = first episode
    assert t["episode_count"] == 6


async def test_S2_episode_link_plays_and_cannot_reach_another_title(client, db, rb, spaces):
    from app.services import tv_stream
    await _series(db, "roots")
    other = await _title(db, "middleton")
    for k in ("wano-tv/roots/s1e1/master.m3u8", "wano-tv/roots/s1e1/1080p/index.m3u8"):
        spaces.objects[k] = MASTER if k.endswith("s1e1/master.m3u8") else VARIANT
    spaces.objects["wano-tv/roots/s1e1/720p/index.m3u8"] = VARIANT
    spaces.objects["wano-tv/roots/s1e1/subs/en/index.m3u8"] = VARIANT
    t = (await _titles(client))["roots"]
    path = urlparse(t["seasons"][0]["episodes"][0]["video_url"]).path
    r = await client.get(path)
    assert r.status_code == 200 and "/api/v1/tv/stream/" in r.text
    token = path.split("/stream/")[1].split("/")[0]
    assert (await client.get(f"/api/v1/tv/stream/{token}/../middleton/master.m3u8")).status_code in (400, 403, 404)
    assert other


async def test_S3_episode_pointing_outside_its_series_is_not_signed(client, db, rb, spaces):
    tid = await _series(db, "roots", seasons=1, per=2)
    await db.tv_titles.update_one({"_id": ObjectId(tid)},
                                  {"$set": {"seasons.0.episodes.0.video_url": f"{CDN}/wano-tv/middleton/master.m3u8"}})
    t = (await _titles(client))["roots"]
    urls = [e["video_url"] for e in t["seasons"][0]["episodes"]]
    assert not any("middleton" in u and "/api/v1/tv/stream/" in u for u in urls)
    assert len(t["seasons"][0]["episodes"]) == 1          # the stray episode is dropped


async def test_S4_movies_unchanged(client, db, rb, spaces):
    await _title(db, "middleton")
    t = (await _titles(client))["middleton"]
    assert t["type"] == "movie" and t["seasons"] == [] and "/api/v1/tv/stream/" in t["video_url"]


async def test_S5_broken_episodes_are_skipped_not_fatal(client, db, rb, spaces):
    tid = await _series(db, "roots", seasons=1, per=3)
    await db.tv_titles.update_one({"_id": ObjectId(tid)}, {"$set": {
        "seasons.0.episodes.0.duration_minutes": "long", "seasons.0.episodes.1": "junk"}})   # s1e3, s1e2
    await _series(db, "empty", seasons=1, per=0)
    titles = await _titles(client)
    assert [e["id"] for e in titles["roots"]["seasons"][0]["episodes"]] == ["s1e1"]
    assert "empty" not in titles                           # a series with nothing playable isn't listed


async def test_S6_history_remembers_the_episode(client, db, rb, spaces):
    tid = await _series(db, "roots")
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/tv/history/{tid}?episode=s2e1", headers=h)
    assert r.status_code == 200 and r.json()["episode"] == "s2e1"
    hist = (await client.get("/api/v1/tv/history", headers=h)).json()
    assert hist[0]["last_episode"] == "s2e1"
    assert (await client.post(f"/api/v1/tv/history/{tid}?episode=s9e9", headers=h)).status_code == 404
    assert (await client.post(f"/api/v1/tv/history/{tid}?episode=../x", headers=h)).status_code in (404, 422)


# ---------------------------------------------------------------- credits -> profiles

async def test_C1_credits_link_to_wano_profiles(client, db, rb, spaces):
    eid, _ = await make_user(db, username="eilmer", display_name="Eilmer Bahri",
                             profile_picture="https://x/p.jpg", email="secret@x.com", phone_number="+256700000001")
    gone, _ = await make_user(db, username="gone", is_active=False)
    await _title(db, "middleton", credits={
        "director": "Eilmer Bahri", "cast": ["Paul Miller"], "production": "Ondore Media", "distributor": "Wano TV",
        "people": [{"name": "Eilmer Bahri", "role": "Director", "username": "eilmer"},
                   {"name": "Paul Miller", "role": "Cast"},
                   {"name": "Old Account", "role": "Editor", "username": "gone"},
                   {"name": "By id", "role": "Sound", "user_id": eid},
                   {"name": "Typo", "role": "Gaffer", "username": "nobody_here"}]})
    r = await client.get("/api/v1/tv/titles", params={"authenticated": "false"})
    t = {x["slug"]: x for x in r.json()}["middleton"]
    people = {p["name"]: p for p in t["credits"]["people"]}
    assert people["Eilmer Bahri"]["profile"] == {"id": eid, "username": "eilmer", "display_name": "Eilmer Bahri",
                                                  "profile_picture": "https://x/p.jpg"}
    assert people["By id"]["profile"]["id"] == eid
    assert people["Paul Miller"]["profile"] is None and people["Old Account"]["profile"] is None
    assert people["Typo"]["profile"] is None
    assert "secret@x.com" not in r.text and "+256700000001" not in r.text
    assert t["credits"]["director"] == "Eilmer Bahri"                 # old fields still there


async def test_C2_credit_lookups_are_batched(client, db, rb, spaces, monkeypatch):
    names = []
    for i in range(40):
        u, _ = await make_user(db, username=f"crew{i}")
        names.append(f"crew{i}")
    for i in range(40):
        await _title(db, f"film-{i}", credits={"director": "d", "cast": [], "production": "p", "distributor": "w",
                                               "people": [{"name": n, "role": "Crew", "username": n} for n in names]})
    finds = []
    real = type(db.users).find

    def counting(self, *a, **k):
        if self.name == "users":                     # the class is shared by every collection
            finds.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(type(db.users), "find", counting)
    await _titles(client)
    assert len(finds) <= 2, f"{len(finds)} user lookups for 40 titles"


# ---------------------------------------------------------------- popularity from plays

async def _play(client, tid, h=None, device=None, ip="41.1.1.1"):
    headers = {**(h or {}), "X-Forwarded-For": ip}
    if device:
        headers["X-Device-Id"] = device
    params = {} if h else {"authenticated": "false"}
    return await client.post(f"/api/v1/tv/titles/{tid}/play", headers=headers, params=params)


async def test_P1_a_play_counts_once_per_viewer_per_day(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    _, h = await make_user(db)
    for _ in range(20):
        assert (await _play(client, tid, h)).status_code == 200
        await _play(client, tid, device="phone-1")
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) == 2


async def test_P1_history_call_counts_as_a_play(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    _, h = await make_user(db)
    await client.post(f"/api/v1/tv/history/{tid}", headers=h)
    await _play(client, tid, h)
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) == 1


async def test_P2_catalogue_ranks_by_recent_plays_and_old_plays_fade(client, db, rb, spaces):
    from app.services import tv_popularity
    a = await _title(db, "aaa", popularity=0)
    b = await _title(db, "bbb", popularity=0)
    c = await _title(db, "ccc", popularity=0)
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    await db.tv_title_daily.insert_many([
        {"_id": f"{a}:old", "title_id": ObjectId(a), "day": today - timedelta(days=12), "plays": 300},
        {"_id": f"{b}:now", "title_id": ObjectId(b), "day": today, "plays": 40},
        {"_id": f"{c}:now", "title_id": ObjectId(c), "day": today - timedelta(days=1), "plays": 25},
        {"_id": f"{a}:ancient", "title_id": ObjectId(a), "day": today - timedelta(days=40), "plays": 99999},
    ])
    tv_popularity.clear_cache()
    order = list((await _titles(client)).keys())
    assert order[:3] == ["aaa", "bbb", "ccc"] or order[:3] == ["bbb", "aaa", "ccc"]
    # 300 plays 12 days ago (~1/3 weight) vs 40 today: today's wins once the old ones age a bit more.
    await db.tv_title_daily.update_one({"_id": f"{a}:old"}, {"$set": {"day": today - timedelta(days=14)}})
    tv_popularity.clear_cache()
    assert list((await _titles(client)).keys())[0] == "bbb"


async def test_P3_editorial_boost_still_works(client, db, rb, spaces):
    from app.services import tv_popularity
    await _title(db, "plain", popularity=0)
    await _title(db, "pushed", popularity=500)
    tv_popularity.clear_cache()
    assert list((await _titles(client)).keys())[0] == "pushed"


async def test_P4_bad_or_missing_titles(client, db, rb, spaces):
    tid = await _title(db, "middleton")
    assert (await _play(client, "notanid", device="d")).status_code == 400
    assert (await _play(client, str(ObjectId()), device="d")).status_code == 404
    await db.tv_titles.update_one({"_id": ObjectId(tid)}, {"$set": {"is_active": False}})
    assert (await _play(client, tid, device="d")).status_code == 404


async def test_P5_bot_devices_from_one_network_are_capped(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    for i in range(300):
        await _play(client, tid, device=f"bot-{i}", ip="9.9.9.9")
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) <= tv_popularity.ANON_PER_IP_PER_DAY
    _, h = await make_user(db)                               # logged-in people on that network still count
    await _play(client, tid, h, ip="9.9.9.9")
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) <= tv_popularity.ANON_PER_IP_PER_DAY + 1


async def test_P6_logged_out_without_device_is_ignored_politely(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    r = await _play(client, tid)
    assert r.status_code == 200 and r.json() == {"counted": False}
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) == 0


async def test_P7_catalogue_with_many_titles_and_days_is_fast(client, db, rb, spaces):
    from app.services import tv_popularity
    ids = [ObjectId(await _title(db, f"t{i}")) for i in range(60)]
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    await db.tv_title_daily.insert_many([{"_id": f"{t}:{d}", "title_id": t, "day": today - timedelta(days=d),
                                          "plays": d + 1} for t in ids for d in range(30)])
    tv_popularity.clear_cache()
    t0 = time.perf_counter()
    assert len(await _titles(client)) == 60
    assert time.perf_counter() - t0 < 3


# ---------------------------------------------------------------- round 2

async def test_R1_old_style_series_with_one_film_still_listed(client, db, rb, spaces):
    await _title(db, "middleton", type="series")              # no seasons yet, just a film
    t = (await _titles(client))["middleton"]
    assert t["type"] == "series" and t["seasons"] == [] and "/api/v1/tv/stream/" in t["video_url"]


async def test_R2_duplicate_episode_ids_are_shown_once(client, db, rb, spaces):
    tid = await _series(db, "roots", seasons=1, per=2)
    await db.tv_titles.update_one({"_id": ObjectId(tid)}, {"$push": {"seasons.0.episodes": _ep("roots", 1, 1)}})
    t = (await _titles(client))["roots"]
    assert [e["id"] for e in t["seasons"][0]["episodes"]] == ["s1e1", "s1e2"] and t["episode_count"] == 2


async def test_R3_history_forgets_an_episode_that_was_removed(client, db, rb, spaces):
    tid = await _series(db, "roots", seasons=2, per=1)
    _, h = await make_user(db)
    await client.post(f"/api/v1/tv/history/{tid}?episode=s2e1", headers=h)
    await db.tv_titles.update_one({"_id": ObjectId(tid)}, {"$pull": {"seasons": {"number": 2}}})
    assert (await client.get("/api/v1/tv/history", headers=h)).json()[0]["last_episode"] is None


async def test_R4_credit_username_case_and_huge_lists(client, db, rb, spaces):
    uid, _ = await make_user(db, username="Eilmer.B")
    people = [{"name": "Eilmer", "role": "Director", "username": "Eilmer.B"},
              {"name": "Lower", "role": "Producer", "username": "eilmer.b"},
              {"name": "Regex", "role": "x", "username": ".*"}]
    people += [{"name": f"extra {i}", "role": "Crew", "username": f"x{i}"} for i in range(5000)]
    await _title(db, "middleton", credits={"director": "d", "cast": [], "production": "p", "distributor": "w",
                                           "people": people})
    t = (await _titles(client))["middleton"]
    got = {p["name"]: p["profile"] for p in t["credits"]["people"]}
    assert got["Eilmer"]["id"] == uid
    assert got["Regex"] is None
    assert len(t["credits"]["people"]) <= 200                 # capped


async def test_R5_play_on_a_movie_while_logged_in_matches_history(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    users = [await make_user(db) for _ in range(5)]
    for _, h in users:
        await client.post(f"/api/v1/tv/history/{tid}", headers=h)
        await _play(client, tid, h)
        await _play(client, tid, h, device="same-phone")      # logged in: the account counts, not the device
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) == 5


async def test_R6_concurrent_plays_count_once(client, db, rb, spaces):
    import asyncio
    from app.services import tv_popularity
    tid = await _title(db, "middleton")
    rs = await asyncio.gather(*[_play(client, tid, device="dup") for _ in range(25)])
    assert all(r.status_code == 200 for r in rs)
    assert sum(r.json()["counted"] for r in rs) == 1
    assert await tv_popularity.plays(db, ObjectId(tid), days=1) == 1


async def test_R7_popularity_field_reflects_plays(client, db, rb, spaces):
    from app.services import tv_popularity
    tid = await _title(db, "middleton", popularity=3)
    for i in range(4):
        await _play(client, tid, device=f"d{i}", ip=f"41.0.0.{i}")
    tv_popularity.clear_cache()
    assert (await _titles(client))["middleton"]["popularity"] == 7


async def test_R8_weird_editorial_popularity_does_not_break_the_list(client, db, rb, spaces):
    await _title(db, "a", popularity="lots")
    await _title(db, "b", popularity=None)
    await _title(db, "c", popularity=True)
    titles = await _titles(client)
    assert {"a", "b", "c"} >= set(titles) and len(titles) >= 2
