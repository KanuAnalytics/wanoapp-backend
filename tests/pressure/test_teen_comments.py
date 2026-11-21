"""Teen comment safety (teen_comments) and the safety record. Attacks: an
adult stranger on a 14-year-old's post, a fake "friend" the teen never
followed back, a 16-year-old's setting, spelled-out swear words, a teen
trying to open comments to everyone before 16, and the record of it all."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

C = "/api/v1/"


def _born(years):
    now = datetime.utcnow()
    return datetime(now.year - years, 1, 1) if now.month > 1 or now.day > 1 else datetime(now.year - years - 1, 1, 1)


async def _on(db, **extra):
    await db.config.insert_one({"features": {"teen_comments": True, **extra}})


async def _follow(db, a, b):
    await db.users.update_one({"_id": ObjectId(a)}, {"$addToSet": {"following": ObjectId(b)}})


async def _post(client, h, vid, text):
    r = await client.post(C, json={"video_id": vid, "content": text}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _held(db, cid):
    return (await db.comments.find_one({"_id": ObjectId(cid)})).get("held")


async def test_TC1_under_16_friends_only(client, db):
    await _on(db)
    teen, th = await make_user(db, date_of_birth=_born(14))
    vid = await make_video(db, teen)
    stranger, sh = await make_user(db)
    fan, fh = await make_user(db)
    friend, frh = await make_user(db)
    await _follow(db, fan, teen)                       # fan follows, teen never followed back
    await _follow(db, friend, teen)
    await _follow(db, teen, friend)
    assert await _held(db, (await _post(client, sh, vid, "hi"))["_id"]) == "teen_friends_only"
    assert await _held(db, (await _post(client, fh, vid, "hi"))["_id"]) == "teen_friends_only"
    assert await _held(db, (await _post(client, frh, vid, "nice one"))["_id"]) is None
    assert await _held(db, (await _post(client, th, vid, "thanks"))["_id"]) is None
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["comments_count"] == 2
    c = await db.comments.find_one({"held": "teen_friends_only"})
    assert c["held_rule"] == "teen_comments:v1" and c["held_at"]


async def test_TC2_16_17_people_they_follow_unless_they_choose_everyone(client, db):
    await _on(db)
    teen, th = await make_user(db, date_of_birth=_born(16))
    vid = await make_video(db, teen)
    stranger, sh = await make_user(db)
    assert await _held(db, (await _post(client, sh, vid, "hey"))["_id"]) == "teen_following_only"
    await _follow(db, teen, stranger)
    assert await _held(db, (await _post(client, sh, vid, "hey"))["_id"]) is None
    other, oh = await make_user(db)
    r = await client.put(f"{C}me/teen-comments", json={"who": "everyone"}, headers=th)
    assert r.status_code == 200
    assert await _held(db, (await _post(client, oh, vid, "great"))["_id"]) is None


async def test_TC3_under_16_cant_open_it_adults_cant_use_it(client, db):
    await _on(db)
    _, th = await make_user(db, date_of_birth=_born(14))
    assert (await client.put(f"{C}me/teen-comments", json={"who": "everyone"}, headers=th)).status_code == 403
    _, ah = await make_user(db)
    assert (await client.put(f"{C}me/teen-comments", json={"who": "everyone"}, headers=ah)).status_code == 400


async def test_TC4_offensive_words_held_even_from_friends_spelled_out_too(client, db):
    await _on(db)
    teen, th = await make_user(db, date_of_birth=_born(15))
    vid = await make_video(db, teen)
    friend, fh = await make_user(db)
    await _follow(db, friend, teen)
    await _follow(db, teen, friend)
    for bad in ("you're a b1tch", "f u c k this", "send nudes", "Kill yourself"):
        assert await _held(db, (await _post(client, fh, vid, bad))["_id"]) == "teen_word_filter", bad
    for ok in ("Himalayan salt on this dish", "class is fun", "assessment tomorrow"):
        assert await _held(db, (await _post(client, fh, vid, ok))["_id"]) is None, ok


async def test_TC5_adults_posts_and_switch_off_unchanged(client, db):
    await _on(db)
    adult, _ = await make_user(db)
    vid = await make_video(db, adult)
    _, sh = await make_user(db)
    assert await _held(db, (await _post(client, sh, vid, "hello"))["_id"]) is None
    await db.config.update_one({}, {"$set": {"features.teen_comments": False}})
    teen, _ = await make_user(db, date_of_birth=_born(14))
    tvid = await make_video(db, teen)
    assert await _held(db, (await _post(client, sh, tvid, "hello"))["_id"]) is None


async def test_TC6_unknown_or_impossible_birthday_is_treated_as_adult(client, db):
    await _on(db)
    nodob, _ = await make_user(db, date_of_birth=None)
    baby, _ = await make_user(db, date_of_birth=_born(3))     # the old sign-up's default date
    _, sh = await make_user(db)
    for owner in (nodob, baby):
        vid = await make_video(db, owner)
        assert await _held(db, (await _post(client, sh, vid, "hi"))["_id"]) is None


async def test_TC7_safety_record_has_rules_switch_history_and_counts(client, db):
    _, ah = await make_user(db, user_type="admin")
    await db.config.insert_one({"features": {}})
    r = await client.put("/api/v1/admin/features", json={"key": "teen_comments", "on": True}, headers=ah)
    assert r.status_code == 200, r.text
    teen, _ = await make_user(db, date_of_birth=_born(14))
    vid = await make_video(db, teen)
    _, sh = await make_user(db)
    await _post(client, sh, vid, "hi")
    rec = (await client.get(f"{C}admin/safety-record", headers=ah)).json()
    ids = {p["id"] for p in rec["policies"]}
    assert {"teen_comments:v1", "comment_guard:v1", "teen_messages:v1"} <= ids
    assert rec["switch_history"][0]["switch"] == "teen_comments" and rec["switch_history"][0]["on"] is True
    assert rec["held_comments"] == {"teen_friends_only": 1}
    _, uh = await make_user(db)
    assert (await client.get(f"{C}admin/safety-record", headers=uh)).status_code == 403
    # rules are never rewritten: an edited text in the code doesn't change the stored version
    await db.safety_policies.update_one({"_id": "teen_comments:v1"}, {"$set": {"text": "original"}})
    from app.services import safety_record
    await safety_record.ensure(db)
    assert (await db.safety_policies.find_one({"_id": "teen_comments:v1"}))["text"] == "original"


async def test_TC8_reading_my_setting(client, db):
    _, ah = await make_user(db)
    _, yh = await make_user(db, date_of_birth=_born(14))
    _, oh = await make_user(db, date_of_birth=_born(17))
    assert (await client.get(f"{C}me/teen-comments", headers=ah)).json() == {"teen": False, "who": "everyone", "locked": False}
    assert (await client.get(f"{C}me/teen-comments", headers=yh)).json() == {"teen": True, "who": "friends", "locked": True}
    assert (await client.get(f"{C}me/teen-comments", headers=oh)).json()["who"] == "following"
    await client.put(f"{C}me/teen-comments", json={"who": "everyone"}, headers=oh)
    assert (await client.get(f"{C}me/teen-comments", headers=oh)).json()["who"] == "everyone"


async def test_TC9_editing_a_swear_into_a_friends_comment_on_a_teen_post_hides_it(client, db):
    await _on(db)
    teen, th = await make_user(db, date_of_birth=_born(15))
    vid = await make_video(db, teen)
    friend, fh = await make_user(db)
    await _follow(db, friend, teen)
    await _follow(db, teen, friend)
    cid = (await _post(client, fh, vid, "nice dance"))["_id"]
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["comments_count"] == 1
    r = await client.put(f"{C}{cid}", json={"content": "nice dance b1tch"}, headers=fh)
    assert r.status_code == 200
    assert await _held(db, cid) == "teen_word_filter"
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["comments_count"] == 0
