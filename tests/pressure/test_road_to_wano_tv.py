"""Road to Wano TV: Rising creator -> Crowned in your Home Ground -> Wano Pick
-> On Wano TV (given by an admin). Attacks: what a creator, a mistaken admin
or a journalist could use against us."""

from datetime import datetime

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

A = "/api/v1/admin"


async def _on(db, **sw):
    await db.config.insert_one({"features": {k: True for k in sw}})


async def _road(client, h):
    r = await client.get("/api/v1/milestones/me", headers=h)
    assert r.status_code == 200, r.text
    return {s["key"]: s["done"] for s in r.json()["road"]}


async def _give(client, ah, who):
    return await client.post(f"{A}/wano-tv", json={"creator": who}, headers=ah)


async def test_RT1_new_creator_sees_four_steps_none_done(client, db):
    await _on(db, milestones=True)
    _, h = await make_user(db, username="fresh")
    r = (await client.get("/api/v1/milestones/me", headers=h)).json()["road"]
    assert [s["key"] for s in r] == ["rising", "crowned", "picked", "wano_tv"]
    assert not any(s["done"] for s in r)
    assert all(isinstance(s["hint"], str) and s["hint"] for s in r)


async def test_RT2_creators_cant_award_themselves(client, db):
    await _on(db, milestones=True)
    me, h = await make_user(db, username="sneaky")
    for url, body in ((f"{A}/wano-tv", {"creator": "@sneaky"}), (f"{A}/wano-tv", {"creator": me})):
        assert (await client.post(url, json=body, headers=h)).status_code == 403
    assert (await client.post(f"{A}/wano-tv", json={"creator": "@sneaky"})).status_code == 401
    assert (await _road(client, h))["wano_tv"] is False


async def test_RT3_banned_account_is_refused(client, db):
    await _on(db, milestones=True)
    await make_user(db, username="banned1", is_active=False)
    _, ah = await make_user(db, user_type="admin")
    r = await _give(client, ah, "@banned1")
    assert r.status_code in (404, 409), r.text
    assert await db.user_badges.count_documents({"kind": "wano_tv"}) == 0


async def test_RT4_under_18_is_refused(client, db):
    """Wano TV needs a signed licence agreement: not something a minor can sign,
    and 'You're going to Wano TV' to a 15-year-old is a headline."""
    await _on(db, milestones=True)
    await make_user(db, username="teen", date_of_birth=datetime(datetime.utcnow().year - 15, 1, 1))
    _, ah = await make_user(db, user_type="admin")
    r = await _give(client, ah, "@teen")
    assert r.status_code == 409, r.text
    assert "18" in r.json()["detail"]
    assert await db.user_badges.count_documents({"kind": "wano_tv"}) == 0


async def test_RT5_any_capitals_and_the_at_sign_work(client, db):
    await _on(db, milestones=True)
    _, ch = await make_user(db, username="WanoStar")
    _, ah = await make_user(db, user_type="admin")
    for who in ("@wanostar", "wanostar", "WANOSTAR"):
        assert (await _give(client, ah, who)).status_code == 200, who
    assert (await _road(client, ch))["wano_tv"] is True
    assert await db.user_badges.count_documents({"kind": "wano_tv"}) == 1   # once, not three times


async def test_RT6_a_mistake_can_be_undone(client, db):
    await _on(db, milestones=True)
    c, ch = await make_user(db, username="oops")
    _, ah = await make_user(db, user_type="admin")
    assert (await _give(client, ah, "@oops")).status_code == 200
    r = await client.post(f"{A}/wano-tv/undo", json={"creator": "@oops"}, headers=ah)
    assert r.status_code == 200, r.text
    assert (await _road(client, ch))["wano_tv"] is False
    assert await db.admin_log.count_documents({"action": "wano_tv_removed"}) == 1
    # Only admins can undo.
    assert (await client.post(f"{A}/wano-tv/undo", json={"creator": "@oops"}, headers=ch)).status_code == 403


async def test_RT7_credited_on_a_wano_tv_title_counts_by_itself(client, db):
    """A creator credited on a live Wano TV title is on Wano TV: the last step
    shouldn't wait for someone to remember to press a button."""
    await _on(db, milestones=True)
    a, ah_ = await make_user(db, username="director")
    b, bh = await make_user(db, username="ActorOne")
    c, ch = await make_user(db, username="maker")
    await db.tv_titles.insert_one({"title": "Yaoundé Police Criminelle", "is_active": True,
                                   "credits": {"people": [{"name": "D", "role": "Director", "user_id": a},
                                                          {"name": "A", "role": "Actor", "username": "actorone"}]},
                                   "makers": [{"user_id": ObjectId(c), "role": "Producer"}]})
    for h in (ah_, bh, ch):
        assert (await _road(client, h))["wano_tv"] is True
    # A pulled title doesn't count; nor does merely having a similar name.
    d, dh = await make_user(db, username="pulled")
    await db.tv_titles.insert_one({"title": "Old", "is_active": False,
                                   "credits": {"people": [{"name": "x", "role": "Actor", "user_id": d}]}})
    _, eh = await make_user(db, username="director2")
    assert (await _road(client, dh))["wano_tv"] is False
    assert (await _road(client, eh))["wano_tv"] is False


async def test_RT8_road_never_breaks_the_achievements_page(client, db):
    await _on(db, milestones=True)
    c, ch = await make_user(db, username="odd", tier=["not", "a", "string"])
    await db.user_badges.insert_one({"_id": f"{c}:junk", "user_id": ObjectId(c), "kind": None})
    await db.tv_titles.insert_one({"title": "Broken", "is_active": True, "credits": "garbage"})
    await db.tv_titles.insert_one({"title": "Broken2", "is_active": True, "credits": [None, 5, {"user_id": "x"}]})
    r = await client.get("/api/v1/milestones/me", headers=ch)
    assert r.status_code == 200
    assert len(r.json()["road"]) == 4


async def test_RT9_undo_is_safe(client, db):
    await _on(db, milestones=True)
    await make_user(db, username="never")
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"{A}/wano-tv/undo", json={"creator": "@never"}, headers=ah)
    assert r.status_code == 200 and r.json()["removed"] is False
    assert (await client.post(f"{A}/wano-tv/undo", json={"creator": "@nobody"}, headers=ah)).status_code == 404
    # Undo only touches the Wano TV badge, never other badges.
    u, uh = await make_user(db, username="keep")
    await db.user_badges.insert_one({"_id": f"{u}:wano_pick", "user_id": ObjectId(u), "kind": "wano_pick"})
    await _give(client, ah, "@keep")
    await client.post(f"{A}/wano-tv/undo", json={"creator": "@keep"}, headers=ah)
    assert await db.user_badges.count_documents({"user_id": ObjectId(u)}) == 1
    assert (await _road(client, uh))["picked"] is True


async def test_RT10_home_ground_reset_takes_any_capitals_and_the_at_sign(client, db):
    _, ah = await make_user(db, user_type="admin")
    u, _ = await make_user(db, username="WanoTeam", home_ground={"city": "Accra", "country": "gh"})
    for who in ("@wanoteam", "wanoteam", "WanoTeam"):
        await db.users.update_one({"_id": ObjectId(u)}, {"$set": {"home_ground": {"city": "Accra", "country": "gh"}}})
        r = await client.post(f"{A}/home-ground/reset", json={"username": who}, headers=ah)
        assert r.status_code == 200, (who, r.text)
        assert "home_ground" not in await db.users.find_one({"_id": ObjectId(u)})
    assert (await client.post(f"{A}/home-ground/reset", json={"username": "@nobody"}, headers=ah)).status_code == 404
