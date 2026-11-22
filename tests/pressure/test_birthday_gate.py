"""Birthday for accounts without one ("birthday_needed") and unknown age
treated as 16-17 for comments ("birthday_unknown_teen"). Attacks: tapping
"Not now" many times in a day, an old app that never asks, changing a birthday
through PATCH/PUT /users/me to dodge the teen rules, adding a birthday twice
at once, a 12-year-old's date, and staff accounts with no birthday."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import birthday_gate
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

A = "/api/v1/age"
OLD = datetime(2025, 1, 1)          # an account past its first days
C = "/api/v1/"


def _born(years):
    now = datetime.utcnow()
    return datetime(now.year - years, 1, 1) if (now.month, now.day) > (1, 1) else datetime(now.year - years - 1, 1, 1)


async def _on(db, **more):
    """The prompt and the requirement (most tests); _friendly() is the prompt alone."""
    await db.config.insert_one({"features": {"birthday_needed": True, "birthday_required": True, **more}})


async def _friendly(db, **more):
    await db.config.insert_one({"features": {"birthday_needed": True, **more}})


async def _skip_days(db, uid, n):
    now = datetime.utcnow()
    for i in range(n):
        await birthday_gate.later(db, uid, now=now + timedelta(days=i))


async def _comment(client, h, vid, text="nice"):
    return await client.post(C, json={"video_id": vid, "content": text}, headers=h)


async def test_BG1_switch_off_nothing_changes(client, db):
    uid, h = await make_user(db, date_of_birth=None, created_at=OLD)
    assert (await client.get(f"{A}/needed", headers=h)).status_code == 404
    assert (await client.post(f"{A}/later", headers=h)).status_code == 404
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"birthday_skips": 9}})
    assert (await _comment(client, h, vid)).status_code == 200
    # PATCH /users/me works as before with the switch off.
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "2001-05-05T00:00:00"}, headers=h)
    assert r.status_code == 200, r.text
    _, h2 = await make_user(db)
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "2010-05-05T00:00:00"}, headers=h2)
    assert r.status_code == 200


async def test_BG2_state_and_not_now_counts_once_a_day(client, db):
    await _on(db)
    uid, h = await make_user(db, date_of_birth=None, created_at=OLD)
    assert (await client.get(f"{A}/needed", headers=h)).json() == {"needed": True, "ask": True, "skips_left": 3,
                                                                   "required": False}
    for _ in range(5):                                   # tapping it again and again the same day
        r = await client.post(f"{A}/later", headers=h)
    assert r.json()["skips_left"] == 2
    _, ah = await make_user(db)                          # a usable birthday: nothing needed
    assert (await client.get(f"{A}/needed", headers=ah)).json() == {"needed": False, "ask": False, "skips_left": 0,
                                                                    "required": False}
    _, bh = await make_user(db, date_of_birth=datetime.utcnow() - timedelta(days=200))   # impossible date
    assert (await client.get(f"{A}/needed", headers=bh)).json()["needed"] is True


async def test_BG3_after_three_days_post_comment_message_need_a_birthday(client, db):
    await _on(db, messages=True, quick_posts=True)
    uid, h = await make_user(db, date_of_birth=None, created_at=OLD)
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    await _skip_days(db, uid, 2)
    assert (await _comment(client, h, vid)).status_code == 200
    await _skip_days(db, uid, 1)                          # same day as the first: still 2
    assert (await client.get(f"{A}/needed", headers=h)).json()["skips_left"] == 1
    await birthday_gate.later(db, uid, now=datetime.utcnow() + timedelta(days=5))
    st = (await client.get(f"{A}/needed", headers=h)).json()
    assert st == {"needed": True, "ask": True, "skips_left": 0, "required": True}
    r = await _comment(client, h, vid)
    assert r.status_code == 403 and r.json()["detail"] == birthday_gate.NEEDED_TEXT
    r = await client.post("/api/v1/videos/post", headers=h, json={
        "media_type": "photo", "images": ["https://wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg"],
        "description": "x"})
    assert r.status_code == 403
    r = await client.post("/api/v1/quick/card", json={"text": "hi"}, headers=h)
    assert r.status_code == 403
    for a, b in ((uid, creator), (creator, uid)):
        await db.users.update_one({"_id": ObjectId(a)}, {"$addToSet": {"following": ObjectId(b)}})
    r = await client.post("/api/v1/messages/conversations", json={"user_id": creator}, headers=h)
    assert r.status_code in (200, 201), r.text
    r = await client.post(f"/api/v1/messages/conversations/{r.json()['id']}/messages", json={"text": "hi"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"] == birthday_gate.NEEDED_TEXT
    # Liking still works.
    assert (await client.post(f"/api/v1/videos/{vid}/like", headers=h)).status_code == 200
    # Adding it lifts the block, and resets the count.
    r = await client.post(f"{A}/add", json={"birthday": "1995-03-04"}, headers=h)
    assert r.status_code == 200 and r.json() == {"needed": False, "ask": False, "skips_left": 0, "required": False}
    assert (await _comment(client, h, vid)).status_code == 200
    u = await db.users.find_one({"_id": ObjectId(uid)})
    assert u["date_of_birth"] == datetime(1995, 3, 4) and "birthday_skips" not in u
    assert u["dob_history"][-1]["via"] == "added"


async def test_BG4_old_apps_never_blocked(client, db):
    await _on(db)
    uid, h = await make_user(db, date_of_birth=None, created_at=OLD)       # never sends "Not now"
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    for _ in range(3):
        assert (await _comment(client, h, vid)).status_code == 200


async def test_BG5_add_once_13_to_110_only(client, db):
    await _on(db)
    _, h = await make_user(db, date_of_birth=None)
    twelve = (datetime.utcnow() - timedelta(days=365 * 12 + 30)).date().isoformat()
    r = await client.post(f"{A}/add", json={"birthday": twelve}, headers=h)
    assert r.status_code == 400 and "13" in r.json()["detail"]
    assert (await client.post(f"{A}/add", json={"birthday": "1800-01-01"}, headers=h)).status_code == 400
    assert (await client.post(f"{A}/add", json={"birthday": "nope"}, headers=h)).status_code == 400
    a, b = await asyncio.gather(client.post(f"{A}/add", json={"birthday": "2000-01-01"}, headers=h),
                                client.post(f"{A}/add", json={"birthday": "1980-01-01"}, headers=h))
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    r = await client.post(f"{A}/add", json={"birthday": "1970-01-01"}, headers=h)
    assert r.status_code == 409 and "support" in r.json()["detail"]
    _, ah = await make_user(db)
    assert (await client.post(f"{A}/add", json={"birthday": "1970-01-01"}, headers=ah)).status_code == 409


async def test_BG6_profile_cant_change_a_birthday_once_set(client, db):
    await _on(db)
    teen, th = await make_user(db, date_of_birth=_born(15), bio="x")
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "1990-01-01T00:00:00", "bio": "new"}, headers=th)
    assert r.status_code == 200
    u = await db.users.find_one({"_id": ObjectId(teen)})
    assert u["date_of_birth"] == _born(15) and u["bio"] == "new"
    r = await client.put("/api/v1/users/me", json={"date_of_birth": "1990-01-01T00:00:00", "bio": "newer"}, headers=th)
    assert r.status_code == 200, r.text
    assert (await db.users.find_one({"_id": ObjectId(teen)}))["date_of_birth"] == _born(15)
    # No birthday yet: the profile may add one, checked like the app's.
    nob, nh = await make_user(db, date_of_birth=None)
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "2020-01-01T00:00:00", "bio": "hi"}, headers=nh)
    assert r.status_code == 200                       # the impossible date is ignored, the bio saves
    u = await db.users.find_one({"_id": ObjectId(nob)})
    assert u.get("date_of_birth") is None and u["bio"] == "hi"
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "1999-09-09T00:00:00"}, headers=nh)
    assert r.status_code == 200
    assert (await db.users.find_one({"_id": ObjectId(nob)}))["date_of_birth"] == datetime(1999, 9, 9)


async def test_BG7_support_changes_it_admin_only_and_kept(client, db):
    await _on(db)
    uid, h = await make_user(db, date_of_birth=_born(15))
    body = {"birthday": "1990-02-02", "reason": "Passport checked by email"}
    assert (await client.post(f"/api/v1/admin/users/{uid}/birthday", json=body, headers=h)).status_code in (401, 403)
    admin, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/admin/users/{uid}/birthday", json=body, headers=ah)
    assert r.status_code == 200 and r.json()["birthday"] == "1990-02-02"
    u = await db.users.find_one({"_id": ObjectId(uid)})
    h0 = u["dob_history"][-1]
    assert h0["via"] == "support" and h0["by"] == admin and h0["old"] == _born(15)
    assert await db.admin_log.count_documents({"action": "birthday"}) == 1


async def test_BG8_unknown_age_gets_16_17_comment_rule(client, db):
    await _on(db, teen_comments=True, birthday_unknown_teen=True)
    owner, oh = await make_user(db, date_of_birth=None)
    vid = await make_video(db, owner)
    stranger, sh = await make_user(db)
    c = (await _comment(client, sh, vid, "hey")).json()
    assert (await db.comments.find_one({"_id": ObjectId(c["_id"])})).get("held") == "teen_following_only"
    # They add a birthday: an adult's post again.
    await client.post(f"{A}/add", json={"birthday": "1990-01-01"}, headers=oh)
    c = (await _comment(client, sh, vid, "hey")).json()
    assert (await db.comments.find_one({"_id": ObjectId(c["_id"])})).get("held") is None
    # Staff accounts with no birthday aren't affected.
    staff, _ = await make_user(db, date_of_birth=None, user_type="admin")
    svid = await make_video(db, staff)
    c = (await _comment(client, sh, svid, "hey")).json()
    assert (await db.comments.find_one({"_id": ObjectId(c["_id"])})).get("held") is None


async def test_BG9_unknown_teen_switch_off_unknown_is_adult(client, db):
    await _on(db, teen_comments=True)
    owner, _ = await make_user(db, date_of_birth=None)
    vid = await make_video(db, owner)
    _, sh = await make_user(db)
    c = (await _comment(client, sh, vid, "hey")).json()
    assert (await db.comments.find_one({"_id": ObjectId(c["_id"])})).get("held") is None


async def test_BG10_friendly_mode_never_blocks_or_counts(client, db):
    await _friendly(db, messages=True)
    uid, h = await make_user(db, date_of_birth=None, created_at=OLD)
    assert (await client.get(f"{A}/needed", headers=h)).json() == {"needed": True, "ask": True, "skips_left": 0,
                                                                   "required": False}
    await _skip_days(db, uid, 5)
    assert "birthday_skips" not in await db.users.find_one({"_id": ObjectId(uid)})
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    assert (await _comment(client, h, vid)).status_code == 200
    # Already skipped 9 times under the requirement, then it was turned off: not blocked.
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"birthday_skips": 9}})
    assert (await _comment(client, h, vid)).status_code == 200
    # Adding a birthday and the one-way lock still work.
    assert (await client.post(f"{A}/add", json={"birthday": "1992-02-02"}, headers=h)).status_code == 200
    r = await client.patch("/api/v1/users/me", json={"date_of_birth": "2010-01-01T00:00:00", "bio": "x"}, headers=h)
    assert r.status_code == 200
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["date_of_birth"] == datetime(1992, 2, 2)


async def test_BG11_new_accounts_not_asked_in_their_first_days(client, db):
    await _on(db)
    _, h = await make_user(db, date_of_birth=None, created_at=datetime.utcnow() - timedelta(hours=5))
    st = (await client.get(f"{A}/needed", headers=h)).json()
    assert st["needed"] is True and st["ask"] is False
    _, h2 = await make_user(db, date_of_birth=None, created_at=datetime.utcnow() - timedelta(days=4))
    assert (await client.get(f"{A}/needed", headers=h2)).json()["ask"] is True
