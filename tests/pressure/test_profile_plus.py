"""'Followed by' on profiles (profile_plus switch). Attacks first."""

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
URL = "/api/v1/users/{}/followed-by"


async def _on(db, on=True):
    await db.config.insert_one({"features": {"profile_plus": on}})


async def _follow(db, a, b):
    await db.users.update_one({"_id": ObjectId(a)}, {"$addToSet": {"following": ObjectId(b)}})
    await db.users.update_one({"_id": ObjectId(b)}, {"$addToSet": {"followers": ObjectId(a)}})


async def test_PP1_off_by_default_404(client, db):
    me, h = await make_user(db)
    t, _ = await make_user(db)
    assert (await client.get(URL.format(t), headers=h)).status_code == 404


async def test_PP2_needs_sign_in(client, db):
    await _on(db)
    t, _ = await make_user(db)
    assert (await client.get(URL.format(t))).status_code in (401, 403)


async def test_PP3_bad_id_400(client, db):
    await _on(db)
    _, h = await make_user(db)
    assert (await client.get(URL.format("nope"), headers=h)).status_code == 400
    assert (await client.get(URL.format("$where"), headers=h)).status_code == 400


async def test_PP4_mutuals_counted_faces_with_pictures_first_newest_first(client, db):
    await _on(db)
    me, h = await make_user(db)
    t, _ = await make_user(db)
    a, _ = await make_user(db, username="a", profile_picture=None)
    b, _ = await make_user(db, username="b", profile_picture="https://x/b.jpg")
    c, _ = await make_user(db, username="c", profile_picture="https://x/c.jpg")
    d, _ = await make_user(db, username="d", profile_picture="https://x/d.jpg")
    other, _ = await make_user(db, username="stranger", profile_picture="https://x/s.jpg")
    for x in (a, b, c, d):
        await _follow(db, me, x)
        await _follow(db, x, t)
    await _follow(db, other, t)  # follows them, but I don't follow this one
    r = await client.get(URL.format(t), headers=h)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 4
    assert [u["username"] for u in j["users"]] == ["d", "c", "b"]
    assert set(j["users"][0]) == {"id", "username", "display_name", "profile_picture"}


async def test_PP5_own_profile_and_nobody_in_common_are_empty(client, db):
    await _on(db)
    me, h = await make_user(db)
    t, _ = await make_user(db)
    assert (await client.get(URL.format(me), headers=h)).json() == {"count": 0, "users": []}
    assert (await client.get(URL.format(t), headers=h)).json() == {"count": 0, "users": []}


async def test_PP6_closed_blocked_and_missing_accounts_left_out(client, db):
    await _on(db)
    me, h = await make_user(db)
    t, _ = await make_user(db)
    gone, _ = await make_user(db, is_active=False)
    blocked, _ = await make_user(db)
    ok, _ = await make_user(db, username="ok")
    for x in (gone, blocked, ok):
        await _follow(db, me, x)
        await _follow(db, x, t)
    await db.users.update_one({"_id": ObjectId(me)}, {"$push": {"blocked_users": ObjectId(blocked)}})
    j = (await client.get(URL.format(t), headers=h)).json()
    assert [u["username"] for u in j["users"]] == ["ok"]
    # A closed or unknown target shows nothing.
    assert (await client.get(URL.format(str(ObjectId())), headers=h)).json()["count"] == 0
    await db.users.update_one({"_id": ObjectId(t)}, {"$set": {"is_active": False}})
    assert (await client.get(URL.format(t), headers=h)).json()["count"] == 0


async def test_PP7_blocked_target_shows_nothing_and_junk_arrays_dont_break_it(client, db):
    await _on(db)
    me, h = await make_user(db)
    t, _ = await make_user(db)
    x, _ = await make_user(db)
    await _follow(db, me, x)
    await _follow(db, x, t)
    await db.users.update_one({"_id": ObjectId(me)}, {"$push": {"following": {"$each": ["junk", 5, None]}}})
    await db.users.update_one({"_id": ObjectId(t)}, {"$push": {"followers": {"$each": ["junk", None]}}})
    assert (await client.get(URL.format(t), headers=h)).json()["count"] == 1
    await db.users.update_one({"_id": ObjectId(me)}, {"$push": {"blocked_by": ObjectId(t)}})
    assert (await client.get(URL.format(t), headers=h)).json() == {"count": 0, "users": []}


async def test_PP8_heavy_follower_7605_follows_is_fast(client, db):
    import time
    await _on(db)
    me, h = await make_user(db)
    t, _ = await make_user(db)
    ids = [ObjectId() for _ in range(7605)]
    fans = [ObjectId() for _ in range(20000)]
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"following": ids}})
    await db.users.update_one({"_id": ObjectId(t)}, {"$set": {"followers": fans + ids[:50]}})
    t0 = time.perf_counter()
    j = (await client.get(URL.format(t), headers=h)).json()
    assert j["count"] == 50 and j["users"] == []  # those ids have no accounts
    assert time.perf_counter() - t0 < 2.0
