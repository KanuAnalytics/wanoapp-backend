"""Fair challenge scoring (challenge_fair). Attacks: invented signed-out
viewers, the same viewer every 6 hours, the creator's own comments and likes,
brand-new accounts, one post entering every challenge, 50 hashtags, boosting
after the end, staff winning. Old challenges keep their old score."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

OLD = datetime.now(timezone.utc) - timedelta(days=30)
CH = "/api/v1/challenges"


def _naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _switch(db, **on):
    await db.config.insert_one({"features": {"challenges": True, **on}})


async def _challenge(client, ah, tag="wanofirstbite", hours=48):
    now = datetime.now(timezone.utc)
    r = await client.post(CH, json={"hashtag": tag, "title": "One Bite", "description": "Film a bite",
                                    "starts_at": (now - timedelta(minutes=5)).isoformat(),
                                    "ends_at": (now + timedelta(hours=hours)).isoformat()}, headers=ah)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _enter(client, h, caption, **extra):
    from app.services.video_posts import create_video_post
    return await create_video_post(extra.pop("uid"), remote_url="https://videodelivery.net/abc/manifest/video.m3u8",
                                   remote_url_cf="https://videodelivery.net/abc/manifest/video.m3u8",
                                   description=caption, is_ready_to_stream=True, **extra)


async def _watch(client, vid, portion=1.0, h=None, device=None):
    headers = dict(h or {})
    if device:
        headers["X-Device-Id"] = device
    params = {} if h else {"authenticated": "false"}
    r = await client.post(f"/api/v1/videos/{vid}/watch", json={"portion": portion}, headers=headers, params=params)
    assert r.status_code == 200, r.text


async def _board(client, cid, h=None):
    params = {} if h else {"authenticated": "false"}
    r = await client.get(f"{CH}/{cid}/leaderboard", headers=h or {}, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _setup(client, db):
    await _switch(db, challenge_fair=True)
    admin, ah = await make_user(db, user_type="admin")
    cid = await _challenge(client, ah)
    return admin, ah, cid


async def test_CF1_created_with_switch_on_gets_fair_rules_old_ones_dont(client, db):
    await _switch(db, challenge_fair=False)
    _, ah = await make_user(db, user_type="admin")
    old = await _challenge(client, ah, tag="oldone")
    await db.config.update_one({}, {"$set": {"features.challenge_fair": True}})
    new = await _challenge(client, ah, tag="newone")
    assert (await db.challenges.find_one({"_id": ObjectId(old)})).get("rules") is None
    assert (await db.challenges.find_one({"_id": ObjectId(new)}))["rules"] == 2


async def test_CF2_invented_signed_out_viewers_are_capped(client, db):
    _, _, cid = await _setup(client, db)
    cheat, ch = await make_user(db)
    honest, hh = await make_user(db)
    vc = await _enter(client, ch, "bite #WanoFirstBite", uid=cheat)
    vh = await _enter(client, hh, "my bite #wanofirstbite", uid=honest)
    for i in range(45):                                       # a script inventing devices (IP cap is 50/h)
        await _watch(client, vc, device=f"bot{i}")
    for _ in range(6):                                        # six real established viewers
        _, vh_h = await make_user(db, created_at=OLD)
        await _watch(client, vh, h=vh_h)
    rows = (await _board(client, cid))["entries"]
    by = {r["video_id"]: r["score"] for r in rows}
    assert by[vc] <= 5                                        # 45 fake views = 4.5 points
    assert by[vh] == 18                                       # 6 x (1 + 2 finished)
    assert rows[0]["video_id"] == vh


async def test_CF3_new_accounts_and_creator_dont_count(client, db):
    _, _, cid = await _setup(client, db)
    cheat, ch = await make_user(db)
    v = await _enter(client, ch, "#wanofirstbite", uid=cheat)
    await client.post(f"/api/v1/videos/{v}/like", headers=ch)
    for i in range(5):
        await client.post("/api/v1/", json={"video_id": v, "content": f"so good {i}"}, headers=ch)
    for _ in range(5):                                        # fresh accounts made today
        _, nh = await make_user(db)
        await _watch(client, v, h=nh)
        await client.post(f"/api/v1/videos/{v}/like", headers=nh)
        await client.post("/api/v1/", json={"video_id": v, "content": "wow"}, headers=nh)
    rows = (await _board(client, cid))["entries"]
    assert rows[0]["score"] == 0


async def test_CF4_one_established_fan_counts_once_whatever_they_do(client, db):
    _, _, cid = await _setup(client, db)
    me, mh = await make_user(db)
    v = await _enter(client, mh, "#wanofirstbite", uid=me)
    fan, fh = await make_user(db, created_at=OLD)
    await _watch(client, v, h=fh, portion=0.5)
    await db.view_dedupe.delete_many({})                      # 6 hours later
    await _watch(client, v, h=fh, portion=1.0)
    for i in range(4):
        await client.post("/api/v1/", json={"video_id": v, "content": f"again {i}"}, headers=fh)
    await client.post(f"/api/v1/videos/{v}/like", headers=fh)
    rows = (await _board(client, cid))["entries"]
    assert rows[0]["score"] == 1 + 2 + 5 + 3                  # one view (finished), one commenter, one like


async def test_CF5_one_post_one_challenge_and_hashtag_stuffing_enters_none(client, db):
    _, ah, cid = await _setup(client, db)
    other = await _challenge(client, ah, tag="wanoin10")
    me, mh = await make_user(db)
    both = await _enter(client, mh, "#wanoin10 #wanofirstbite", uid=me)
    stuffed = await _enter(client, mh, " ".join(f"#t{i}" for i in range(11)) + " #wanofirstbite", uid=me)
    assert str((await db.videos.find_one({"_id": ObjectId(both)}))["challenge_id"]) == other
    assert "challenge_id" not in await db.videos.find_one({"_id": ObjectId(stuffed)})
    assert [r["video_id"] for r in (await _board(client, other))["entries"]] == [both]
    assert (await _board(client, cid))["entries"] == []


async def test_CF6_joined_from_app_beats_hashtag_order_private_never_enters(client, db):
    _, ah, cid = await _setup(client, db)
    other = await _challenge(client, ah, tag="wanoin10")
    me, mh = await make_user(db)
    v = await _enter(client, mh, "#wanoin10 #wanofirstbite", uid=me, challenge_id=cid)
    assert str((await db.videos.find_one({"_id": ObjectId(v)}))["challenge_id"]) == cid
    p = await _enter(client, mh, "#wanofirstbite", uid=me, privacy="private")
    assert "challenge_id" not in await db.videos.find_one({"_id": ObjectId(p)})
    assert other


async def test_CF7_board_freezes_at_the_end(client, db):
    _, _, cid = await _setup(client, db)
    me, mh = await make_user(db)
    v = await _enter(client, mh, "#wanofirstbite", uid=me)
    _, fh = await make_user(db, created_at=OLD)
    await _watch(client, v, h=fh)
    import asyncio
    await asyncio.sleep(0.05)
    await db.challenges.update_one({"_id": ObjectId(cid)}, {"$set": {"ends_at": _naive_now()}})
    first = (await _board(client, cid))["entries"][0]["score"]
    for _ in range(3):                                        # boosting after the end
        _, lh = await make_user(db, created_at=OLD)
        await client.post(f"/api/v1/videos/{v}/like", headers=lh)
    assert (await _board(client, cid))["entries"][0]["score"] == first == 3


async def test_CF8_staff_never_rank_and_you_always_see_your_entry(client, db):
    admin, ah, cid = await _setup(client, db)
    await _enter(client, ah, "#wanofirstbite", uid=admin)
    me, mh = await make_user(db)
    rows = (await _board(client, cid))["entries"]
    assert rows == []
    v = await _enter(client, mh, "#wanofirstbite", uid=me)
    out = await _board(client, cid, mh)                       # cached board predates the entry
    assert out["mine"]["video_id"] == v


async def test_CF9_held_comments_and_signed_out_finish_dont_count(client, db):
    await _switch(db, challenge_fair=True, comment_guard=True)
    _, ah = await make_user(db, user_type="admin")
    cid = await _challenge(client, ah)
    me, mh = await make_user(db)
    v = await _enter(client, mh, "#wanofirstbite", uid=me)
    _, fh = await make_user(db, created_at=OLD)
    await client.post("/api/v1/", json={"video_id": v, "content": "cheap at deals.ug"}, headers=fh)
    await _watch(client, v, device="phone1", portion=1.0)
    rows = (await _board(client, cid))["entries"]
    assert rows[0]["score"] == 0                              # 0.1 rounds down; the link comment is held


async def test_CF10_old_challenge_scoring_is_untouched(client, db):
    await _switch(db, challenge_fair=False)
    _, ah = await make_user(db, user_type="admin")
    cid = await _challenge(client, ah)
    me, mh = await make_user(db)
    v = await make_video(db, me, hashtags=["wanofirstbite"], likes_count=2)
    rows = (await _board(client, cid))["entries"]
    assert rows[0]["video_id"] == v and rows[0]["score"] == 6 and "mine" not in await _board(client, cid)


async def test_CF11_test_challenges_ended_in_their_first_hour_are_not_listed_and_admin_can_edit(client, db):
    _, ah, cid = await _setup(client, db)
    await client.post(f"/api/v1/admin/challenges/{cid}/end", headers=ah)
    listed = (await client.get(CH, params={"authenticated": "false"})).json()
    assert all(c["id"] != cid for c in listed)
    up = await _challenge(client, ah, tag="later")
    r = await client.put(f"{CH}/{up}", json={"title": "Fixed typo"}, headers=ah)
    assert r.status_code == 200 and r.json()["title"] == "Fixed typo"
    _, uh = await make_user(db)
    assert (await client.put(f"{CH}/{up}", json={"title": "Hacked"}, headers=uh)).status_code == 403
    live_tag = await client.put(f"{CH}/{up}", json={"hashtag": "other"}, headers=ah)
    assert live_tag.status_code == 409
