"""More points categories, Suggest a category, false-information reports,
What we look for, and the numbers behind a Wano Pick. Attacks written before
the code: off must mean exactly as before, on must not open new ways to cheat."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_points import D, _pending, _post, _views, clock  # noqa: F401

pytestmark = pytest.mark.asyncio

P = "/api/v1/points"
A = "/api/v1/admin"
OLD = ["comedy", "music", "food_culture", "learn_news", "film", "sports"]


async def _switch(db, **on):
    doc = await db.config.find_one({"features": {"$type": "object"}})
    if doc:
        await db.config.update_one({"_id": doc["_id"]}, {"$set": {f"features.{k}": v for k, v in on.items()}})
    else:
        await db.config.insert_one({"features": dict(on)})


async def _cats(client, all=True):
    r = await client.get(f"{P}/categories", params={"authenticated": "false", **({"all": "1"} if all else {})})
    assert r.status_code == 200
    return r.json()


# ------------------------------------------------------------- categories

async def test_MC1_off_exactly_the_six_as_before(client, db):
    for all_ in (True, False):
        cats = await _cats(client, all_)
        assert [c["id"] for c in cats] == OLD
        assert cats[2]["label"] == "Food, culture & travel" and cats[3]["label"] == "Learn & local news"


async def test_MC1b_older_apps_keep_six_even_when_on(client, db):
    """Their category sheet doesn't scroll: 16 rows would run off small screens."""
    await _switch(db, more_categories=True)
    cats = await _cats(client, all=False)
    assert [c["id"] for c in cats] == OLD
    assert cats[3]["label"] == "Learn & local news"


async def test_MC2_on_sixteen_in_order_with_clearer_names(client, db):
    await _switch(db, more_categories=True)
    cats = await _cats(client)
    assert len(cats) == 16 and len({c["id"] for c in cats}) == 16
    labels = {c["id"]: c["label"] for c in cats}
    assert labels["food_culture"] == "Food & culture" and labels["learn_news"] == "Learn & education"
    for k in ("local_news", "fashion_beauty", "health", "nature_travel", "faith", "family", "farming_animals",
              "diaspora", "business_skills", "tech_gaming"):
        assert k in labels
    # Never these.
    assert not any(w in " ".join(labels.values()).lower() for w in ("politic", "other", "money", "promo"))


async def test_MC3_new_keys_only_while_on(client, db):
    c, ch = await make_user(db)
    v = await make_video(db, c)
    r = await client.put(f"{P}/videos/{v}/category", json={"category": "tech_gaming"}, headers=ch)
    assert r.status_code == 400
    assert (await client.put(f"{P}/videos/{v}/category", json={"category": "comedy"}, headers=ch)).status_code == 200
    await _switch(db, more_categories=True)
    r = await client.put(f"{P}/videos/{v}/category", json={"category": "tech_gaming"}, headers=ch)
    assert r.status_code == 200
    assert (await db.videos.find_one({"_id": ObjectId(v)}))["category"] == "tech_gaming"
    # Junk and other people's posts are still refused.
    assert (await client.put(f"{P}/videos/{v}/category", json={"category": "politics"}, headers=ch)).status_code == 422
    _, oh = await make_user(db)
    assert (await client.put(f"{P}/videos/{v}/category", json={"category": "faith"}, headers=oh)).status_code == 403


async def test_MC4_posting_with_a_new_key_while_off_saves_without_one(db):
    from app.api.v1 import videos as vapi
    assert await vapi._points_category("tech_gaming") is None
    assert await vapi._points_category("comedy") == "comedy"
    assert await vapi._points_category(None) is None
    await _switch(db, more_categories=True)
    assert await vapi._points_category("tech_gaming") == "tech_gaming"


async def test_MC5_new_categories_earn_like_the_old_ones_and_keep_earning_if_switched_off(client, db, clock):
    from app.services import points
    await _switch(db, more_categories=True)
    c, _ = await make_user(db)
    v = await _post(db, c, category="health")
    await _views(db, v, c, views=40)
    await _switch(db, more_categories=False)
    await points.settle(db)
    assert await _pending(db, c) == 40 * points.PER_VIEW


async def test_MC6a_false_information_reports_change_nothing_while_off(client, db, clock):
    from app.services import points
    c, _ = await make_user(db)
    v = await _post(db, c, category="comedy")
    await _views(db, v, c, views=100)
    for _ in range(2):
        await db.reports.insert_one({"video_id": ObjectId(v), "reporter_id": ObjectId(), "category": "false_information",
                                     "is_active": True, "created_at": datetime.utcnow()})
    await points.settle(db)
    assert await _pending(db, c) == 100


async def test_MC6b_two_people_reporting_false_information_stop_a_post_earning_when_on(client, db, clock):
    from app.services import points
    await _switch(db, more_categories=True)
    d, _ = await make_user(db)
    w = await _post(db, d, category="health")
    await _views(db, w, d, views=100)
    one = await _post(db, d, category="health", hour=10)    # a single report is not enough
    await _views(db, one, d, views=7)
    for _ in range(2):
        await db.reports.insert_one({"video_id": ObjectId(w), "reporter_id": ObjectId(), "category": "false_information",
                                     "is_active": True, "created_at": datetime.utcnow()})
    await db.reports.insert_one({"video_id": ObjectId(one), "reporter_id": ObjectId(), "category": "false_information",
                                 "is_active": True, "created_at": datetime.utcnow()})
    await points.settle(db)
    assert await _pending(db, d) == 7


async def test_MC7_same_person_reporting_twice_is_one_person(client, db, clock):
    from app.services import points
    await _switch(db, more_categories=True)
    c, _ = await make_user(db)
    v = await _post(db, c, category="health")
    await _views(db, v, c, views=50)
    rep = ObjectId()
    for _ in range(5):
        await db.reports.insert_one({"video_id": ObjectId(v), "reporter_id": rep, "category": "false_information",
                                     "is_active": True, "created_at": datetime.utcnow()})
    await points.settle(db)
    assert await _pending(db, c) == 50


# ------------------------------------------------------- suggest a category

async def _suggest(client, h, name):
    return await client.post(f"{P}/categories/suggest", json={"name": name}, headers=h)


async def test_SG1_closed_while_off_and_for_logged_out(client, db):
    _, h = await make_user(db)
    assert (await _suggest(client, h, "Cars")).status_code == 404
    await _switch(db, more_categories=True)
    assert (await client.post(f"{P}/categories/suggest", json={"name": "Cars"})).status_code == 401


async def test_SG2_counted_per_person_and_shown_to_admins(client, db):
    await _switch(db, more_categories=True)
    hs = [(await make_user(db))[1] for _ in range(3)]
    for h in hs:
        assert (await _suggest(client, h, "Cars")).json() == {"exists": False}
    assert (await _suggest(client, hs[0], "  cars ")).status_code == 200          # same person again: still 3
    await _suggest(client, hs[1], "Poetry")
    _, ah = await make_user(db, user_type="admin")
    items = (await client.get(f"{A}/category-requests", headers=ah)).json()["items"]
    assert [(i["name"], i["count"]) for i in items] == [("Cars", 3), ("Poetry", 1)]
    assert (await client.get(f"{A}/category-requests", headers=hs[0])).status_code == 403
    assert (await client.delete(f"{A}/category-requests/cars", headers=ah)).json()["removed"] is True
    assert [i["name"] for i in (await client.get(f"{A}/category-requests", headers=ah)).json()["items"]] == ["Poetry"]


async def test_SG3_existing_category_is_pointed_to_not_counted(client, db):
    await _switch(db, more_categories=True)
    _, h = await make_user(db)
    for name in ("Fashion and Beauty", "fashion & beauty", "TECH & GAMING", "Comedy & skits"):
        r = (await _suggest(client, h, name)).json()
        assert r["exists"] is True, name
    assert await db.category_requests.count_documents({}) == 0


async def test_SG4_junk_links_and_spam_refused(client, db):
    await _switch(db, more_categories=True)
    _, h = await make_user(db)
    for bad in ("", " ", "a", "x" * 41, "Win 50k now", "www.scam.com", "https://evil.com", "cars.com",
                "$where", "<script>", "🔥🔥🔥", "1234"):
        assert (await _suggest(client, h, bad)).status_code == 422, bad
    assert (await client.post(f"{P}/categories/suggest", json={"name": "x" * 200}, headers=h)).status_code == 422
    assert (await client.post(f"{P}/categories/suggest", json={"name": ["Cars"]}, headers=h)).status_code == 422
    assert await db.category_requests.count_documents({}) == 0


async def test_SG5_daily_limit(client, db):
    await _switch(db, more_categories=True)
    _, h = await make_user(db)
    names = ["Cars", "Poetry", "Gardening", "Astrology", "Interior design", "Boats", "Comics"]
    codes = [(await _suggest(client, h, n)).status_code for n in names]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert await db.category_requests.count_documents({}) == 5


# ---------------------------------------------------- What we look for

async def test_GD1_guide_off_by_default_then_complete(client, db):
    assert (await client.get("/api/v1/creator/guide", params={"authenticated": "false"})).status_code == 404
    await _switch(db, content_guide=True)
    g = (await client.get("/api/v1/creator/guide", params={"authenticated": "false"})).json()
    keys = [s["key"] for s in g["sections"]]
    assert keys == ["pick", "tv"]
    tv = g["sections"][1]
    assert any("18" in m for m in tv["must"]) and any("rights" in m for m in tv["must"])
    assert g["submit"]["url"].startswith("https://wanoafrica.com/")
    assert any("False" in n for n in g["never_earns"])


async def test_GD2_road_offers_submit_your_film_only_when_on(client, db):
    await _switch(db, milestones=True)
    _, h = await make_user(db)
    road = (await client.get("/api/v1/milestones/me", headers=h)).json()["road"]
    assert "action" not in road[-1]
    await _switch(db, content_guide=True)
    road = (await client.get("/api/v1/milestones/me", headers=h)).json()["road"]
    assert road[-1]["action"] == {"label": "Submit your film", "url": "https://wanoafrica.com/tv#submit-film"}


# ------------------------------------------------- numbers before picking

async def test_PC1_pick_check_numbers_and_usual(client, db):
    c, _ = await make_user(db, username="maker")
    v = await make_video(db, c, likes_count=12, comments_count=5, shares_count=2, category="music")
    await db.video_reach.insert_one({"_id": ObjectId(v), "views": 200, "finished": 120})
    for f in (20, 30, 40):                                  # usual: 30 / 100
        o = await make_video(db, c)
        await db.video_reach.insert_one({"_id": ObjectId(o), "views": 100, "finished": f})
    _, ah = await make_user(db, user_type="admin")
    r = await client.get(f"{A}/picks/check", params={"video": f"https://wanoafrica.com/v/{v}"}, headers=ah)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["views"] == 200 and d["watched_to_end_rate"] == 0.6 and d["creators_usual_rate"] == 0.3
    assert (d["likes"], d["comments"], d["shares"]) == (12, 5, 2)
    assert d["warnings"] == [] and d["creator"] == "maker" and d["category"] == "music"


async def test_PC2_pick_check_warns_about_what_should_stop_a_pick(client, db):
    teen, _ = await make_user(db, date_of_birth=datetime(datetime.utcnow().year - 15, 1, 1))
    v = await make_video(db, teen, originality={"state": "rejected"})
    for cat in ("intellectual_property", "false_information"):
        await db.reports.insert_one({"video_id": ObjectId(v), "reporter_id": ObjectId(), "category": cat,
                                     "is_active": True, "created_at": datetime.utcnow()})
    _, ah = await make_user(db, user_type="admin")
    w = (await client.get(f"{A}/picks/check", params={"video": v}, headers=ah)).json()["warnings"]
    assert "Creator is under 18" in w and "Marked not original" in w
    assert any(x.startswith("Reported as a copy") for x in w)
    assert any(x.startswith("Reported as false information") for x in w)
    # No views yet: no rate, no crash.
    assert (await client.get(f"{A}/picks/check", params={"video": v}, headers=ah)).json()["watched_to_end_rate"] is None


async def test_PC3_pick_check_only_for_admins_and_real_posts(client, db):
    c, ch = await make_user(db)
    v = await make_video(db, c)
    _, ah = await make_user(db, user_type="admin")
    assert (await client.get(f"{A}/picks/check", params={"video": v}, headers=ch)).status_code == 403
    assert (await client.get(f"{A}/picks/check", params={"video": v})).status_code == 401
    for bad in ("hello", "https://evil.com/x"):
        assert (await client.get(f"{A}/picks/check", params={"video": bad}, headers=ah)).status_code == 422
    assert (await client.get(f"{A}/picks/check", params={"video": str(ObjectId())}, headers=ah)).status_code == 404
