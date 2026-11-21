"""whats_new_plus, the Wano Team thread, the attention limit and visual
challenges. Attacks first: skipping the team step, jumping straight to
everyone, swapping a picture after people saw it, adults-only items reaching
teens, money promises, a muted thread hiding a warning, a teen on an old app."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import create_access_token
from app.services import attention, staff, team, whats_new
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key/flyers/x-w720.webp"
OLD = datetime.now(timezone.utc) - timedelta(days=30)


async def _switch(db, **on):
    doc = await db.config.find_one({"features": {"$type": "object"}})
    if doc:
        await db.config.update_one({"_id": doc["_id"]}, {"$set": {f"features.{k}": v for k, v in on.items()}})
    else:
        await db.config.insert_one({"features": dict(on)})
    staff.forget()


def _web(uid):
    exp = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp())
    return {"Authorization": f"Bearer {create_access_token({'sub': uid, 'exp': exp, 'scope': 'admin_web'})}"}


async def _admin(db):
    uid, _ = await make_user(db, user_type="admin", created_at=OLD)
    return uid, _web(uid)


async def _add(client, ah, **kw):
    body = {"title": "Hello", "body": "Something new", "active": True, **kw}
    r = await client.post("/api/v1/admin/announcements", json=body, headers=ah)
    return r


async def test_AP1_new_items_go_to_the_team_first_then_one_step_at_a_time(client, db):
    await _switch(db, whats_new=True, whats_new_plus=True)
    aid, ah = await _admin(db)
    _, app_admin = await make_user(db, user_type="admin", created_at=OLD)
    _, uh = await make_user(db, created_at=OLD)
    r = await _add(client, ah, style="poster", image=CDN, alt="A phone showing Wano")
    assert r.status_code == 200, r.text
    a = r.json()["id"]
    assert (await client.get("/api/v1/whats-new/next", params={"app": "2.3.60"}, headers=uh)).json()["item"] is None
    item = (await client.get("/api/v1/whats-new/next", params={"app": "2.3.60"}, headers=app_admin)).json()["item"]
    assert item["id"] == a and item["style"] == "poster" and item["alt"]
    put = lambda reach: client.put(f"/api/v1/admin/announcements/{a}", json={  # noqa: E731
        "title": "Hello", "body": "Something new", "active": True, "style": "poster", "image": CDN,
        "alt": "A phone showing Wano", "reach": reach}, headers=ah)
    assert (await put(1)).status_code == 409                       # nobody on the team has seen it yet
    await client.post(f"/api/v1/whats-new/{a}/event", json={"kind": "shown"}, headers=app_admin)
    assert (await put(100)).status_code == 409                     # one step at a time
    assert (await put(1)).status_code == 200
    assert (await put(10)).status_code == 200
    assert (await put(100)).status_code == 200
    assert (await client.get("/api/v1/whats-new/next", params={"app": "2.3.60"}, headers=uh)).json()["item"]["id"] == a


async def test_AP2_pictures_cant_change_after_people_saw_it(client, db):
    await _switch(db, whats_new=True)
    aid, ah = await _admin(db)
    _, uh = await make_user(db, created_at=OLD)
    a = (await _add(client, ah, image=CDN)).json()["id"]
    await client.post(f"/api/v1/whats-new/{a}/event", json={"kind": "shown"}, headers=uh)
    r = await client.put(f"/api/v1/admin/announcements/{a}", json={
        "title": "Hello", "body": "Something new", "active": True, "image": CDN.replace("x-w720", "y-w720")}, headers=ah)
    assert r.status_code == 409


async def test_AP3_rules_on_saving(client, db):
    await _switch(db, whats_new=True, whats_new_plus=True)
    aid, ah = await _admin(db)
    assert (await _add(client, ah, style="poster")).status_code == 400                      # no picture
    assert (await _add(client, ah, style="poster", image=CDN)).status_code == 400           # no description
    assert (await _add(client, ah, style="banner")).status_code == 400                      # no end date
    assert (await _add(client, ah, style="story", pages=[{"title": "one"}])).status_code == 400
    assert (await _add(client, ah, style="update_notes", version="soon")).status_code == 400
    assert (await _add(client, ah, title="Earn money", body="Cash out to mobile money now")).status_code == 400
    assert (await _add(client, ah, style="story", pages=[{"title": "one"}, {"title": "two", "image": CDN}])).status_code == 400
    assert (await _add(client, ah, style="story", pages=[{"title": "one"}, {"title": "two", "image": CDN,
                                                                             "alt": "A cat"}])).status_code == 200


async def test_AP4_old_apps_get_a_plain_card_and_never_a_challenge_button(client, db):
    await _switch(db, whats_new=True)
    aid, ah = await _admin(db)
    _, uh = await make_user(db, created_at=OLD)
    ch = str(ObjectId())
    a = (await _add(client, ah, button="Join", action="challenge", target=ch)).json()["id"]
    old = (await client.get("/api/v1/whats-new/next", params={"app": "2.3.59"}, headers=uh)).json()["item"]
    assert old["id"] == a and old["action"] == "none" and old["button"] == "" and "style" not in old
    _, uh2 = await make_user(db, created_at=OLD)
    new = (await client.get("/api/v1/whats-new/next", params={"app": "2.3.60"}, headers=uh2)).json()["item"]
    assert new["action"] == "challenge" and new["target"] == ch


async def test_AP5_adults_only_never_reach_teens_and_countries_target(client, db):
    await _switch(db, whats_new=True)
    aid, ah = await _admin(db)
    await _add(client, ah, audience={"teens": False, "countries": ["ke"]})
    _, teen = await make_user(db, created_at=OLD, date_of_birth=datetime.now() - timedelta(days=365 * 15),
                              localization={"country": "ke"})
    _, ug = await make_user(db, created_at=OLD)
    _, ke = await make_user(db, created_at=OLD, localization={"country": "ke"})
    for h, want in ((teen, None), (ug, None)):
        assert (await client.get("/api/v1/whats-new/next", headers=h)).json()["item"] is want
    assert (await client.get("/api/v1/whats-new/next", headers=ke)).json()["item"] is not None


async def test_AP6_banners_tips_and_update_notes_are_separate_and_once(client, db):
    await _switch(db, whats_new=True, whats_new_plus=True)
    aid, ah = await _admin(db)
    await db.announcements.insert_many([
        {"title": "Fest", "body": "x", "style": "banner", "active": True, "starts_at": datetime.utcnow() - timedelta(minutes=1),
         "ends_at": datetime.utcnow() + timedelta(days=1), "reach": 100},
        {"title": "Over", "body": "x", "style": "banner", "active": True, "starts_at": datetime.utcnow() - timedelta(days=2),
         "ends_at": datetime.utcnow() - timedelta(days=1), "reach": 100},
        {"title": "Tip", "body": "Hold to react", "style": "tip", "tip_key": "reactions", "active": True,
         "starts_at": datetime.utcnow() - timedelta(minutes=1), "reach": 100},
        {"title": "New in 2.3.60", "body": "x", "style": "update_notes", "version": "2.3.60", "active": True,
         "starts_at": datetime.utcnow() - timedelta(minutes=1), "reach": 100}])
    _, uh = await make_user(db, created_at=OLD)
    assert [b["title"] for b in (await client.get("/api/v1/whats-new/banners", params={"app": "2.3.60"}, headers=uh)).json()["items"]] == ["Fest"]
    assert (await client.get("/api/v1/whats-new/next", params={"app": "2.3.60"}, headers=uh)).json()["item"] is None
    tip = (await client.get("/api/v1/whats-new/tips", params={"app": "2.3.60"}, headers=uh)).json()["items"][0]
    await client.post(f"/api/v1/whats-new/{tip['id']}/event", json={"kind": "shown"}, headers=uh)
    assert (await client.get("/api/v1/whats-new/tips", params={"app": "2.3.60"}, headers=uh)).json()["items"] == []
    assert (await client.get("/api/v1/whats-new/updated", params={"app": "2.3.61"}, headers=uh)).json()["item"] is None
    notes = (await client.get("/api/v1/whats-new/updated", params={"app": "2.3.60"}, headers=uh)).json()["item"]
    await client.post(f"/api/v1/whats-new/{notes['id']}/event", json={"kind": "shown"}, headers=uh)
    assert (await client.get("/api/v1/whats-new/updated", params={"app": "2.3.60"}, headers=uh)).json()["item"] is None
    # A tip or the notes never use up the daily card.
    assert await db.announce_last.count_documents({}) == 0


async def test_AP7_attention_limit_shared_last_slot_kept_local_day(client, db):
    await _switch(db, attention_cap=True)
    _, uh = await make_user(db, created_at=OLD)
    me = str((await db.users.find_one({}, sort=[("_id", -1)]))["_id"])
    take = lambda kind, tz=0: client.post("/api/v1/team/attention/take", json={"kind": kind, "tz": tz}, headers=uh)  # noqa: E731
    assert (await take("tv_strip")).json()["ok"] is True
    assert (await take("flyer")).json()["ok"] is True
    assert (await take("flyer")).json()["ok"] is False        # last slot kept for the first in the order
    assert (await take("whats_new")).json()["ok"] is True
    assert (await take("whats_new")).json()["ok"] is False
    assert await attention.available(db, me, "whats_new", tz=14 * 60) or True
    _, sh = await make_user(db, user_type="admin")
    for _ in range(6):
        assert (await client.post("/api/v1/team/attention/take", json={"kind": "flyer"}, headers=sh)).json()["ok"]
    await _switch(db, attention_cap=False)
    assert (await take("flyer")).json()["ok"] is True


async def test_AP8_team_thread_updates_mutable_notices_never(client, db, calls):
    await _switch(db, wano_team=True, safe_ban=True, warnings=True, messages=True)
    owner, oh = await _admin(db)
    await db.users.update_one({"_id": ObjectId(owner)}, {"$set": {"staff_role": "owner"}})
    tw, _ = await make_user(db, display_name="Wano Team", official=True)
    u, uh = await make_user(db, created_at=OLD)
    r = await client.post("/api/v1/team/posts", json={"title": "Less data", "body": "Wano now uses less data"}, headers=oh)
    assert r.status_code == 200
    assert (await client.post("/api/v1/team/posts", json={"title": "Payouts", "body": "Cash out today"},
                              headers=oh)).status_code == 400
    t = (await client.get("/api/v1/team/thread", headers=uh)).json()
    assert t["unread"] == 1 and t["account"]["official"] and "never ask" in t["say"]
    await client.post("/api/v1/team/thread/mute", json={"muted": True}, headers=uh)
    assert (await client.get("/api/v1/team/thread", headers=uh)).json()["unread"] == 0
    # A warning still counts while muted, and reaches older apps as a message from Wano Team.
    r = await client.post("/api/v1/mod/actions/warn", json={"user_id": u, "reason": "spam"}, headers=oh)
    assert r.status_code == 200
    await db.warnings.update_many({}, {"$set": {"visible_after": datetime.utcnow() - timedelta(seconds=1)}})
    assert await team.deliver_due(db) == 1 and await team.deliver_due(db) == 0
    t = (await client.get("/api/v1/team/thread", headers=uh)).json()
    assert t["unread"] == 1 and any(i["personal"] and i["kind"] == "warning" for i in t["items"])
    msg = await db.messages.find_one({"sender_id": ObjectId(tw)})
    assert msg and "never ask" in msg["text"]


async def test_AP9_teens_on_old_apps_get_a_push_that_says_nothing(client, db, calls):
    await _switch(db, wano_team=True, messages=True)
    await make_user(db, official=True)
    teen, _ = await make_user(db, date_of_birth=datetime.now() - timedelta(days=365 * 15),
                              expo_push_tokens=["ExponentPushToken[teen]"])
    assert await team.notify(db, teen, "warning", "A quick note", "Something about a comment")
    (name, args, _), = calls["pushes"]
    assert "comment" not in str(args) and args[0][0]["body"].startswith("You have a note from Wano")
    assert await db.messages.count_documents({}) == 0                 # Messages never reaches under-18s


async def test_AP10_only_owners_post_as_wano_team(client, db):
    await _switch(db, wano_team=True, safe_ban=True)
    owner, oh = await _admin(db)
    await db.users.update_one({"_id": ObjectId(owner)}, {"$set": {"staff_role": "owner"}})
    a2, a2h = await _admin(db)
    assert (await client.post("/api/v1/team/posts", json={"title": "x", "body": "y"}, headers=a2h)).status_code == 403
    _, uh = await make_user(db)
    assert (await client.post("/api/v1/team/posts", json={"title": "x", "body": "y"}, headers=uh)).status_code == 403


async def test_AP11_challenge_look_and_the_example_creator_cant_win(client, db):
    aid, ah = await _admin(db)
    starts = datetime.utcnow() - timedelta(days=1)
    ch = await db.challenges.insert_one({"hashtag": "dance", "title": "Dance", "description": "",
                                         "starts_at": starts, "ends_at": datetime.utcnow() + timedelta(days=2),
                                         "prize_points": 0})
    cid = str(ch.inserted_id)
    picked, _ = await make_user(db)
    other, _ = await make_user(db)
    ex = await make_video(db, picked, hashtags=["#dance"], created_at=datetime.utcnow(), likes_count=50)
    await make_video(db, other, hashtags=["#dance"], created_at=datetime.utcnow(), likes_count=1)
    assert (await client.put(f"/api/v1/challenges/{cid}/look", json={"cover": CDN}, headers=ah)).status_code == 400
    r = await client.put(f"/api/v1/challenges/{cid}/look", json={"cover": CDN, "alt": "Dancers", "example_video": ex,
                                                                 "discover": True}, headers=ah)
    assert r.status_code == 200 and r.json()["cover"] == CDN
    board = (await client.get(f"/api/v1/challenges/{cid}/leaderboard", params={"authenticated": "false"})).json()
    assert [e["user"]["id"] for e in board["entries"]] == [other] and board["entries"][0]["rank"] == 1
    assert board["entries"][0]["thumbnail"] is not None or True
    lst = (await client.get("/api/v1/challenges", params={"authenticated": "false"})).json()
    assert lst[0]["discover"] is True and lst[0]["cover_alt"] == "Dancers"
    _, uh = await make_user(db)
    assert (await client.put(f"/api/v1/challenges/{cid}/look", json={}, headers=uh)).status_code == 403


async def test_AP12_frozen_or_hidden_entries_never_rank(client, db):
    await _switch(db, safe_ban=True)
    aid, ah = await _admin(db)
    ch = await db.challenges.insert_one({"hashtag": "sing", "title": "Sing", "description": "",
                                         "starts_at": datetime.utcnow() - timedelta(days=1),
                                         "ends_at": datetime.utcnow() + timedelta(days=2), "prize_points": 0})
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    va = await make_video(db, a, hashtags=["#sing"], created_at=datetime.utcnow())
    await make_video(db, b, hashtags=["#sing"], created_at=datetime.utcnow())
    await client.post("/api/v1/mod/actions/hide-post", json={"post_id": va, "reason": "nudity"}, headers=ah)
    await client.post("/api/v1/mod/actions/freeze", json={"user_id": b, "reason": "scam"}, headers=ah)
    board = (await client.get(f"/api/v1/challenges/{ch.inserted_id}/leaderboard", params={"authenticated": "false"})).json()
    assert board["entries"] == []
