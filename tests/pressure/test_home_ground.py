"""Home Ground: every user represents a city (picked from a fixed list) and a
country; weekly battles between countries and cities; local creator boards;
a weekly crown per city and country. Behind the "home_ground" switch.
Attacks written before the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
HG = "/api/v1/home-ground"


async def _on(db, on=True):
    await db.config.insert_one({"features": {"home_ground": on, "leaderboard": True}})


def _week_start():
    from app.services import leaderboard
    return leaderboard.week_start(datetime.now(timezone.utc))


async def _scored(db, username, country="ug", city=None, score_views=100, dob=datetime(1990, 1, 1), **extra):
    """A creator with a post this week and some score."""
    doc = {"localization": {"country": country}, "date_of_birth": dob, **extra}
    if city:
        doc["home_ground"] = {"city": city, "set_at": datetime.utcnow() - timedelta(days=60)}
    uid, h = await make_user(db, username=username, **doc)
    start = _week_start()
    await make_video(db, uid, created_at=start + timedelta(hours=2))
    await db.creator_daily.insert_one({"creator_id": ObjectId(uid), "day": start, "views": score_views,
                                       "finished": 0, "likes": 0, "comments": 0, "shares": 0})
    return uid, h


async def _clear():
    from app.services import leaderboard, home_ground
    leaderboard.clear_cache()
    home_ground.clear_cache()


async def test_HG1_switch_off_everything_closed(client, db):
    _, h = await make_user(db)
    for path in ["/cities", "/me", "/battle", "/board?country=ug"]:
        assert (await client.get(HG + path, headers=h)).status_code == 404, path
    assert (await client.get("/api/v1/config/features")).json()["home_ground"] is False


async def test_HG2_city_list_is_fixed_and_grouped_by_country(client, db):
    await _on(db)
    _, h = await make_user(db)
    r = (await client.get(f"{HG}/cities", headers=h)).json()
    ug = next(c for c in r if c["country"] == "ug")
    ids = [x["id"] for x in ug["cities"]]
    assert "kampala" in ids and "gulu" in ids
    cd = next(c for c in r if c["country"] == "cd")
    assert "kinshasa" in [x["id"] for x in cd["cities"]]


async def test_HG3_pick_home_ground_and_suggestion_from_old_city_setting(client, db):
    await _on(db)
    _, h = await make_user(db, localization={"country": "ug", "city": "Kampala", "city_key": "kampala"})
    me = (await client.get(f"{HG}/me", headers=h)).json()
    assert me["city"] is None and me["suggested"]["id"] == "kampala"
    r = await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)
    assert r.status_code == 200, r.text
    me = (await client.get(f"{HG}/me", headers=h)).json()
    assert me["city"] == {"id": "kampala", "name": "Kampala", "country": "ug"}


async def test_HG4_unknown_or_hostile_city_refused(client, db):
    await _on(db)
    _, h = await make_user(db)
    for bad in ["atlantis", "", {"$ne": 1}, "kampala; drop", "__proto__", None]:
        assert (await client.put(f"{HG}/me", json={"city_id": bad}, headers=h)).status_code in (400, 422), bad


async def test_HG5_city_can_change_once_a_month_but_typos_fixed_within_an_hour(client, db):
    await _on(db)
    uid, h = await make_user(db)
    assert (await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)).status_code == 200
    assert (await client.put(f"{HG}/me", json={"city_id": "gulu"}, headers=h)).status_code == 200   # within an hour
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"home_ground.set_at": datetime.utcnow() - timedelta(days=2)}})
    r = await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)
    assert r.status_code == 429 and "30 days" in r.json()["detail"]
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"home_ground.set_at": datetime.utcnow() - timedelta(days=31)}})
    assert (await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)).status_code == 200
    # picking the same city again never locks anything
    assert (await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)).status_code == 200


async def test_HG6_country_battle_works_from_day_one(client, db):
    await _on(db)
    await _scored(db, "ug1", "ug", score_views=300)
    await _scored(db, "ug2", "ug", score_views=200)
    await _scored(db, "ke1", "ke", score_views=400)
    await _scored(db, "cd1", "cd", score_views=50)
    await _clear()
    _, h = await make_user(db)
    b = (await client.get(f"{HG}/battle", params={"level": "country"}, headers=h)).json()
    rows = b["items"]
    assert [r["id"] for r in rows] == ["ug", "ke", "cd"]
    assert rows[0]["score"] == 500 and rows[0]["creators"] == 2 and rows[0]["leader"]["username"] == "ug1"
    assert rows[0]["name"] == "Uganda"


async def test_HG7_city_battle_needs_enough_creators(client, db):
    await _on(db)
    for i in range(3):
        await _scored(db, f"kla{i}", "ug", city="kampala", score_views=100)
    await _scored(db, "gulu1", "ug", city="gulu", score_views=900)     # one creator: not enough for a city row
    await _clear()
    _, h = await make_user(db)
    rows = (await client.get(f"{HG}/battle", params={"level": "city"}, headers=h)).json()["items"]
    assert [r["id"] for r in rows] == ["kampala"]
    assert rows[0]["score"] == 300


async def test_HG8_local_board_for_a_city_and_a_country(client, db):
    await _on(db)
    a, _ = await _scored(db, "kla_a", "ug", city="kampala", score_views=500)
    b, _ = await _scored(db, "kla_b", "ug", city="kampala", score_views=100)
    await _scored(db, "gulu_c", "ug", city="gulu", score_views=900)
    await _scored(db, "ke_d", "ke", score_views=999)
    await _clear()
    _, h = await make_user(db)
    city = (await client.get(f"{HG}/board", params={"city": "kampala"}, headers=h)).json()
    assert [e["user"]["username"] for e in city["entries"]] == ["kla_a", "kla_b"]
    assert [e["rank"] for e in city["entries"]] == [1, 2]
    ug = (await client.get(f"{HG}/board", params={"country": "ug"}, headers=h)).json()
    assert [e["user"]["username"] for e in ug["entries"]] == ["gulu_c", "kla_a", "kla_b"]
    assert (await client.get(f"{HG}/board", params={"city": "atlantis"}, headers=h)).status_code == 404
    assert (await client.get(f"{HG}/board", headers=h)).status_code == 400


async def test_HG9_me_shows_where_i_and_my_city_stand(client, db):
    await _on(db)
    await _scored(db, "kla_a", "ug", city="kampala", score_views=500)
    me_id, mh = await _scored(db, "kla_me", "ug", city="kampala", score_views=100)
    await _scored(db, "kla_c", "ug", city="kampala", score_views=50)
    for i in range(3):
        await _scored(db, f"nbo{i}", "ke", city="nairobi", score_views=400)
    await _clear()
    me = (await client.get(f"{HG}/me", headers=mh)).json()
    assert me["my_rank"]["city"] == 2 and me["my_rank"]["country"] == 2
    assert me["city_standing"] == {"rank": 2, "behind": 550, "ahead_of": None, "of": 2}


async def test_HG10_under_18_count_for_their_city_but_arent_shown_on_city_boards(client, db):
    await _on(db)
    teen, _ = await _scored(db, "teen", "ug", city="kampala", score_views=900,
                            dob=datetime.now() - timedelta(days=365 * 15))
    await _scored(db, "a1", "ug", city="kampala", score_views=10)
    await _scored(db, "a2", "ug", city="kampala", score_views=10)
    await _clear()
    _, h = await make_user(db)
    city = (await client.get(f"{HG}/board", params={"city": "kampala"}, headers=h)).json()
    assert "teen" not in [e["user"]["username"] for e in city["entries"]]
    rows = (await client.get(f"{HG}/battle", params={"level": "city"}, headers=h)).json()["items"]
    assert rows[0]["score"] == 920                      # their work still counts for Kampala
    assert rows[0]["leader"]["username"] != "teen"
    ug = (await client.get(f"{HG}/board", params={"country": "ug"}, headers=h)).json()
    assert ug["entries"][0]["user"]["username"] == "teen"        # country board shows them


async def test_HG11_banned_creators_dont_count(client, db):
    await _on(db)
    bad, _ = await _scored(db, "bad", "ke", score_views=5000)
    await _scored(db, "ug1", "ug", score_views=10)
    await db.users.update_one({"_id": ObjectId(bad)}, {"$set": {"is_active": False}})
    await _clear()
    _, h = await make_user(db)
    rows = (await client.get(f"{HG}/battle", params={"level": "country"}, headers=h)).json()["items"]
    assert [r["id"] for r in rows] == ["ug"]


async def test_HG12_profile_says_representing_city_adults_only(client, db):
    await _on(db)
    a, _ = await make_user(db, username="adult", localization={"country": "ug"},
                           home_ground={"city": "kampala", "set_at": datetime.utcnow()})
    t, _ = await make_user(db, username="teen2", localization={"country": "ug"},
                           home_ground={"city": "kampala", "set_at": datetime.utcnow()},
                           date_of_birth=datetime.now() - timedelta(days=365 * 15))
    _, vh = await make_user(db, username="viewer")
    pa = (await client.get(f"/api/v1/users/{a}/complete", headers=vh)).json()
    pt = (await client.get(f"/api/v1/users/{t}/complete", headers=vh)).json()
    assert pa["home_ground"] == {"label": "Kampala", "country": "ug"}
    assert pt["home_ground"] == {"label": "Uganda", "country": "ug"}


async def test_HG13_weekly_crowns_once_per_place(client, db, calls):
    await _on(db)
    from app.services import home_ground, leaderboard
    for i in range(3):
        await _scored(db, f"kla{i}", "ug", city="kampala", score_views=100 * (i + 1))
    await _clear()
    last = _week_start()
    now = last + timedelta(days=7, hours=2)
    out = await home_ground.award_crowns(db, now=now)
    out2 = await home_ground.award_crowns(db, now=now)
    kinds = sorted([b["kind"] async for b in db.user_badges.find({})])
    assert kinds == ["crown_city", "crown_country"]
    b = await db.user_badges.find_one({"kind": "crown_city"})
    assert b["place"] == "kampala" and leaderboard.label(b) == "Top creator in Kampala, week of " + \
        f"{last.day} {leaderboard.MONTHS[last.month - 1]} {last.year}"


async def test_HG14_logged_out_can_see_battles_but_not_me(client, db):
    await _on(db)
    assert (await client.get(f"{HG}/battle", params={"authenticated": "false"})).status_code == 200
    assert (await client.get(f"{HG}/me")).status_code == 401


# ---------------------------------------------------------------- round 2

async def test_R2_1_typo_window_cant_be_used_to_reset_the_lock(client, db):
    await _on(db)
    uid, h = await make_user(db)
    await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)
    first = (await db.users.find_one({"_id": ObjectId(uid)}))["home_ground"]["set_at"]
    await client.put(f"{HG}/me", json={"city_id": "gulu"}, headers=h)
    await client.put(f"{HG}/me", json={"city_id": "jinja"}, headers=h)
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["home_ground"]["set_at"] == first
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"home_ground.set_at": first - timedelta(hours=2)}})
    assert (await client.put(f"{HG}/me", json={"city_id": "kampala"}, headers=h)).status_code == 429


async def test_R2_2_junk_stored_values_dont_break_anything(client, db):
    await _on(db)
    await _scored(db, "j1", "ug", home_ground="kampala")
    await _scored(db, "j2", "ug", home_ground={"city": {"$ne": 1}})
    await _scored(db, "j3", "UG", localization={"country": "UG"})
    await _scored(db, "j4", "ug", localization="junk")
    await _scored(db, "j5", "ug", date_of_birth="2015-01-01")
    await _clear()
    _, h = await make_user(db)
    assert (await client.get(f"{HG}/battle", headers=h)).status_code == 200
    assert (await client.get(f"{HG}/battle", params={"level": "city"}, headers=h)).status_code == 200
    ug = (await client.get(f"{HG}/board", params={"country": "UG"}, headers=h)).json()
    assert "j3" in [e["user"]["username"] for e in ug["entries"]]


async def test_R2_3_bad_params(client, db):
    await _on(db)
    _, h = await make_user(db)
    assert (await client.get(f"{HG}/board", params={"country": "u1"}, headers=h)).status_code == 400
    assert (await client.get(f"{HG}/board", params={"country": "ugx"}, headers=h)).status_code == 422
    assert (await client.get(f"{HG}/battle", params={"level": "planet"}, headers=h)).status_code == 422
    assert (await client.get(f"{HG}/battle", params={"week": "next"}, headers=h)).status_code == 422
    assert (await client.get(f"{HG}/board", params={"city": "$where"}, headers=h)).status_code == 404


async def test_R2_4_last_week_board(client, db):
    await _on(db)
    uid, _ = await make_user(db, username="old", localization={"country": "ug"})
    last = _week_start() - timedelta(days=7)
    await make_video(db, uid, created_at=last + timedelta(hours=3))
    await db.creator_daily.insert_one({"creator_id": ObjectId(uid), "day": last, "views": 70,
                                       "finished": 0, "likes": 0, "comments": 0, "shares": 0})
    await _clear()
    _, h = await make_user(db)
    rows = (await client.get(f"{HG}/battle", params={"week": "last"}, headers=h)).json()["items"]
    assert rows and rows[0]["id"] == "ug" and rows[0]["score"] == 70
    now_rows = (await client.get(f"{HG}/battle", headers=h)).json()["items"]
    assert now_rows == []


async def test_R2_5_crowns_wait_for_the_week_to_settle_and_respect_the_switch(client, db):
    from app.services import home_ground
    await _on(db)
    for i in range(3):
        await _scored(db, f"k{i}", "ug", city="kampala")
    await _clear()
    early = _week_start() + timedelta(days=7, minutes=10)       # week just ended, not settled
    assert (await home_ground.award_crowns(db, now=early))["crowns"] == 0
    await db.config.update_many({}, {"$set": {"features.home_ground": False}})
    late = _week_start() + timedelta(days=7, hours=3)
    assert (await home_ground.award_crowns(db, now=late))["crowns"] == 0
    assert await db.user_badges.count_documents({}) == 0


async def test_R2_6_suggestion_from_old_free_text_spellings(client, db):
    from app.services import home_ground as hg
    assert hg.city_from_text("  KLA ") == "kampala"
    assert hg.city_from_text("Dar es salaam") == "dar_es_salaam"
    assert hg.city_from_text("Yaoundé") == "yaounde"
    assert hg.city_from_text("Mbuji-Mayi") == "mbuji_mayi"
    assert hg.city_from_text("Atlantis") is None and hg.city_from_text(None) is None
