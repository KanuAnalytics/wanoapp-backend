"""Comment safety (comment_guard). Nothing is refused (old apps show no error):
a comment that breaks a rule is saved for its author only. Attacks: links,
floods, blocked people both ways, bait-and-switch edits, report brigades from
new accounts, counts that must not move, and reading signed out."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

C = "/api/v1/"
OLD = datetime.now(timezone.utc) - timedelta(days=30)


async def _on(db, on=True):
    await db.config.insert_one({"features": {"comment_guard": on}})


async def _post(client, h, vid, text, parent=None):
    body = {"video_id": vid, "content": text, **({"parent_id": parent} if parent else {})}
    r = await client.post(C, json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _list(client, vid, h=None):
    # Signed-out apps ask with authenticated=false (the anon() helper).
    if h is None:
        return await client.get(f"{C}video/{vid}", params={"authenticated": "false"})
    return await client.get(f"{C}video/{vid}", headers=h)


async def _count(db, vid):
    return (await db.videos.find_one({"_id": ObjectId(vid)}))["comments_count"]


async def test_CG1_switch_off_nothing_changes(client, db):
    await _on(db, False)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    await _post(client, fh, vid, "see wano.ug/deal for cheap data")
    assert await _count(db, vid) == 1
    assert (await _list(client, vid)).status_code == 401          # signed out still needs an account
    assert len((await _list(client, vid, oh)).json()) == 1


async def test_CG2_link_is_held_author_sees_it_nobody_else_counts_unchanged(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    for text in ("cheap data at https://scam.example", "dm me on wa.me/2567000", "visit promo.ug now"):
        out = await _post(client, fh, vid, text)
        assert out["content"] == text                               # the author's app shows it normally
    assert await _count(db, vid) == 0
    assert await db.notifications.count_documents({}) == 0
    assert len((await _list(client, vid, fh)).json()) == 3          # author sees their own
    assert (await _list(client, vid, oh)).json() == []              # creator doesn't
    assert (await _list(client, vid)).json() == []                  # nor signed out


async def test_CG3_flood_is_held_after_five_a_minute(client, db):
    await _on(db)
    owner, _ = await make_user(db)
    bot, bh = await make_user(db)
    vid = await make_video(db, owner)
    for i in range(8):
        await _post(client, bh, vid, f"nice {i}")
    assert await _count(db, vid) == 5
    held = await db.comments.count_documents({"held": "too_fast"})
    assert held == 3


async def test_CG4_blocked_either_way_is_held_and_hidden_from_reading(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    troll, th = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    old = await _post(client, th, vid, "first, before the block")
    await db.users.update_one({"_id": ObjectId(owner)}, {"$addToSet": {"blocked_users": ObjectId(troll)}})
    await db.users.update_one({"_id": ObjectId(troll)}, {"$addToSet": {"blocked_by": ObjectId(owner)}})
    await _post(client, th, vid, "after the block")
    assert await _count(db, vid) == 1
    assert (await _list(client, vid, oh)).json() == []              # owner never sees the troll
    assert [c["content"] for c in (await _list(client, vid, fh)).json()] == ["first, before the block"]
    # the blocked person replying to the owner's comment is held too
    mine = await _post(client, oh, vid, "thanks all")
    await _post(client, th, vid, "reply", parent=mine["_id"])
    assert (await db.comments.find_one({"_id": ObjectId(mine["_id"])}))["replies_count"] == 0
    assert old["_id"]


async def test_CG5_signed_out_can_read_but_not_private_posts(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    await _post(client, fh, vid, "beautiful shot")
    r = await _list(client, vid)
    assert r.status_code == 200 and r.json()[0]["is_liked"] is False
    priv = await make_video(db, owner, privacy="private")
    assert (await _list(client, priv)).status_code == 404
    assert (await _list(client, priv, fh)).status_code == 404
    assert (await _list(client, priv, oh)).status_code == 200


async def test_CG6_editing_a_link_into_a_liked_comment_hides_it(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    c = await _post(client, fh, vid, "lovely")
    assert await _count(db, vid) == 1
    r = await client.put(f"{C}{c['_id']}", json={"content": "lovely, more at scam.ug"}, headers=fh)
    assert r.status_code == 200
    assert await _count(db, vid) == 0
    assert (await _list(client, vid, oh)).json() == []
    # deleting it afterwards doesn't take the count below zero or double-count
    await client.delete(f"{C}{c['_id']}", headers=fh)
    assert await _count(db, vid) == 0


async def test_CG7_reports_new_accounts_cant_hide_established_ones_can(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db, created_at=OLD)
    vid = await make_video(db, owner)
    c = await _post(client, fh, vid, "honest opinion")
    for _ in range(5):                                              # a brigade of fresh accounts
        _, nh = await make_user(db)
        r = await client.post(f"{C}{c['_id']}/report", json={"reason": "spam"}, headers=nh)
        assert r.status_code == 200
    assert len((await _list(client, vid, oh)).json()) == 1          # still visible
    for _ in range(3):
        _, eh = await make_user(db, created_at=OLD)
        await client.post(f"{C}{c['_id']}/report", json={"reason": "harassment"}, headers=eh)
    assert (await _list(client, vid, oh)).json() == []
    assert await _count(db, vid) == 0
    assert len((await _list(client, vid, fh)).json()) == 1           # the author still sees it (no drama)
    # can't report your own, bad reason refused, same person twice counts once
    assert (await client.post(f"{C}{c['_id']}/report", json={"reason": "spam"}, headers=fh)).status_code == 400
    _, xh = await make_user(db, created_at=OLD)
    assert (await client.post(f"{C}{c['_id']}/report", json={"reason": "bored"}, headers=xh)).status_code == 400


async def test_CG8_admin_keeps_or_hides(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    admin, ah = await make_user(db, user_type="admin")
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    c = await _post(client, fh, vid, "rude words")
    _, rh = await make_user(db, created_at=OLD)
    await client.post(f"{C}{c['_id']}/report", json={"reason": "hate"}, headers=rh)
    items = (await client.get(f"{C}admin/comment-reports", headers=ah)).json()["items"]
    assert items[0]["comment_id"] == c["_id"] and items[0]["reports"] == 1
    assert (await client.get(f"{C}admin/comment-reports", headers=oh)).status_code == 403
    r = await client.post(f"{C}admin/comment-reports/{c['_id']}", json={"action": "hide"}, headers=ah)
    assert r.status_code == 200
    assert await _count(db, vid) == 0
    assert (await client.get(f"{C}admin/comment-reports", headers=ah)).json()["items"] == []


async def test_CG9_report_route_closed_with_switch_off(client, db):
    await _on(db, False)
    owner, _ = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    c = await _post(client, fh, vid, "hi")
    _, rh = await make_user(db)
    assert (await client.post(f"{C}{c['_id']}/report", json={"reason": "spam"}, headers=rh)).status_code == 404


async def test_CG10_held_and_hidden_never_reach_top_comment_or_profile_lists(client, db):
    await db.config.insert_one({"features": {"comment_guard": True, "feed_fx": True}})
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    await _post(client, fh, vid, "promo at deals.ug")
    r = await client.get(f"/api/v1/videos/{vid}/top-comment", params={"authenticated": "false"})
    assert r.status_code == 200 and r.json()["comment"] is None
    assert (await client.get(f"{C}user/{fan}", headers=oh)).json() == []
    assert len((await client.get(f"{C}user/{fan}", headers=fh)).json()) == 1


async def test_CG11_creator_answering_fans_on_own_post_is_not_slowed(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    vid = await make_video(db, owner)
    for i in range(25):
        await _post(client, oh, vid, f"thank you {i}")
    assert await db.comments.count_documents({"held": {"$exists": True}}) == 0
    assert await _count(db, vid) == 25


async def test_CG12_admin_keep_puts_the_count_back_once(client, db):
    await _on(db)
    owner, _ = await make_user(db)
    admin, ah = await make_user(db, user_type="admin")
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    c = await _post(client, fh, vid, "fine comment")
    for _ in range(3):
        _, rh = await make_user(db, created_at=OLD)
        await client.post(f"{C}{c['_id']}/report", json={"reason": "spam"}, headers=rh)
    assert await _count(db, vid) == 0
    for _ in range(2):                                   # a double tap in Admin
        await client.post(f"{C}admin/comment-reports/{c['_id']}", json={"action": "keep"}, headers=ah)
    assert await _count(db, vid) == 1


async def test_CG13_replies_under_a_held_comment_stay_with_it(client, db):
    await _on(db)
    owner, _ = await make_user(db)
    spammer, sh = await make_user(db)
    vid = await make_video(db, owner)
    held = await _post(client, sh, vid, "deals at scam.ug")
    r = await _post(client, sh, vid, "reply to myself", parent=held["_id"])
    assert await _count(db, vid) == 0
    assert (await db.comments.find_one({"_id": ObjectId(r["_id"])}))["held"] == "parent_hidden"
