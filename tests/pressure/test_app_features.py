"""Feature switches for the app, and Wano TV "makers". Attacks first."""

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_tv_protect import _title, spaces  # noqa: F401
from tests.pressure.test_for_you import rb  # noqa: F401

pytestmark = pytest.mark.asyncio

KEYS = {"points", "boost", "earnings", "replies", "challenges", "sounds", "city", "leaderboard", "badges",
        "tv_series", "tv_play", "hashtags", "milestones", "home_ground", "picks", "missions", "creator_stats", "trial_posts", "mentions", "ai_label", "feed_fx", "messages"}


async def _features(client):
    r = await client.get("/api/v1/config/features")
    assert r.status_code == 200, r.text
    return r.json()


async def test_F1_everything_off_by_default(client, db):
    f = await _features(client)
    assert set(f) == KEYS and not any(f.values())


async def test_F2_switched_on_in_the_config_document_only_known_keys_and_only_true(client, db):
    await db.config.insert_one({"appVersionNumber": "1.7.1",
                                "features": {"points": True, "leaderboard": "yes", "hack": True, "badges": 1}})
    f = await _features(client)
    assert f["points"] is True and f["leaderboard"] is False and f["badges"] is False and "hack" not in f


async def test_F3_garbage_config_doesnt_break_it(client, db):
    await db.config.insert_one({"features": "everything"})
    assert not any((await _features(client)).values())


async def test_M1_makers_from_linked_credits(client, db, rb, spaces):
    eid, _ = await make_user(db, username="eilmer", display_name="Eilmer Bahri", profile_picture="https://x/p.jpg")
    await _title(db, "middleton", credits={"director": "Eilmer Bahri", "cast": [], "production": "p",
                                           "distributor": "w", "people": [
        {"name": "Eilmer Bahri", "role": "Filmmaker", "username": "eilmer"},
        {"name": "No Account", "role": "Cast"}]})
    t = (await client.get("/api/v1/tv/titles", params={"authenticated": "false"})).json()[0]
    assert t["makers"] == [{"user_id": eid, "username": "eilmer", "display_name": "Eilmer Bahri",
                            "profile_picture": "https://x/p.jpg", "role": "Filmmaker"}]


async def test_M2_no_people_no_makers(client, db, rb, spaces):
    await _title(db, "middleton")
    t = (await client.get("/api/v1/tv/titles", params={"authenticated": "false"})).json()[0]
    assert t["makers"] == []


async def test_M3_makers_stored_on_the_title_survive_alongside_linked_credits(client, db, rb, spaces):
    """Live titles (One Field, Middleton) store makers as {user_id, role}; linked
    credits add more. Both show, each person once, closed accounts left out."""
    team, _ = await make_user(db, username="wanoteam", display_name="Wano Team")
    smsaid, _ = await make_user(db, username="smsaid", display_name="Smsaid")
    gone, _ = await make_user(db, username="gone", is_active=False)
    await _title(db, "middleton", credits={"director": "Eilmer Bahri", "cast": [], "production": "p",
                                           "distributor": "w", "people": [
        {"name": "Smsaid", "role": "Cast", "username": "smsaid"},
        {"name": "Wano Team", "role": "Producer", "username": "wanoteam"}]})
    await db.tv_titles.update_one({}, {"$set": {"makers": [
        {"user_id": ObjectId(team), "role": "Filmmaker"},
        {"user_id": ObjectId(gone), "role": "Featured in film"}]}})
    t = (await client.get("/api/v1/tv/titles", params={"authenticated": "false"})).json()[0]
    assert [(m["username"], m["role"]) for m in t["makers"]] == [("wanoteam", "Filmmaker"), ("smsaid", "Cast")]


async def test_M4_stored_makers_with_no_credit_people(client, db, rb, spaces):
    team, _ = await make_user(db, username="wanoteam2")
    await _title(db, "onefield")
    await db.tv_titles.update_one({}, {"$set": {"makers": [{"user_id": ObjectId(team), "role": "Filmmaker"},
                                                           {"user_id": "not-an-id", "role": "x"}, "junk"]}})
    t = (await client.get("/api/v1/tv/titles", params={"authenticated": "false"})).json()[0]
    assert [m["username"] for m in t["makers"]] == ["wanoteam2"]
