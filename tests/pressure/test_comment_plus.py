"""comment_plus on the server: the order (pinned, hearted, most liked, a spot
for a small account), each person's own total (a held comment never shows up
as a missing number), and creator heart / pin (only the creator; not on held
comments; one pin per post). Switch off: the old order."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
C = "/api/v1/"
OLD = datetime.now(timezone.utc) - timedelta(days=30)


async def _on(db, plus=True):
    await db.config.insert_one({"features": {"comment_guard": True, "comment_plus": plus}})


async def _post(client, h, vid, text):
    r = await client.post(C, json={"video_id": vid, "content": text}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["_id"]


async def _texts(client, vid, h):
    r = await client.get(f"{C}video/{vid}", headers=h)
    return [c["content"] for c in r.json()], r


async def test_CPL1_order_pinned_hearted_liked_and_a_small_account_third(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    vid = await make_video(db, owner)
    big = []
    for i in range(4):
        _, bh = await make_user(db, followers_count=5000, created_at=OLD)
        big.append(await _post(client, bh, vid, f"big{i}"))
    _, sh = await make_user(db, followers_count=12, created_at=OLD)
    small = await _post(client, sh, vid, "small")
    _, nh = await make_user(db, followers_count=3)                     # brand-new account: no spot
    await _post(client, nh, vid, "newbie")
    for i, cid in enumerate(big):
        await db.comments.update_one({"_id": ObjectId(cid)}, {"$set": {"likes_count": 10 + i}})
    order, _ = await _texts(client, vid, oh)
    assert order[:4] == ["big3", "big2", "small", "big1"]
    assert await client.post(f"{C}{big[0]}/pin", json={"on": True}, headers=oh)
    await client.post(f"{C}{big[1]}/heart", json={"on": True}, headers=oh)
    order, _ = await _texts(client, vid, oh)
    assert order[:3] == ["big0", "big1", "small"]


async def test_CPL2_my_total_includes_my_held_comment_others_dont_see_it(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    await _post(client, fh, vid, "lovely")
    await _post(client, fh, vid, "deals at scam.ug")              # held
    texts, r = await _texts(client, vid, fh)
    assert len(texts) == 2 and r.headers["X-Comments-Total"] == "2"
    texts, r = await _texts(client, vid, oh)
    assert len(texts) == 1 and r.headers["X-Comments-Total"] == "1"


async def test_CPL3_only_the_creator_hearts_or_pins_never_held_one_pin_per_post(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    a = await _post(client, fh, vid, "first")
    b = await _post(client, fh, vid, "second")
    held = await _post(client, fh, vid, "promo deals.ug")
    assert (await client.post(f"{C}{a}/heart", json={"on": True}, headers=fh)).status_code == 403
    assert (await client.post(f"{C}{held}/pin", json={"on": True}, headers=oh)).status_code == 404
    await client.post(f"{C}{a}/pin", json={"on": True}, headers=oh)
    await client.post(f"{C}{b}/pin", json={"on": True}, headers=oh)
    pinned = await db.comments.count_documents({"video_id": ObjectId(vid), "is_pinned": True})
    assert pinned == 1
    reply = (await client.post(C, json={"video_id": vid, "content": "re", "parent_id": a}, headers=oh)).json()["_id"]
    assert (await client.post(f"{C}{reply}/pin", json={"on": True}, headers=oh)).status_code == 400


async def test_CPL4_switch_off_old_order_no_routes(client, db):
    await _on(db, plus=False)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    a = await _post(client, fh, vid, "older")
    await _post(client, fh, vid, "newer")
    await db.comments.update_one({"_id": ObjectId(a)}, {"$set": {"likes_count": 50}})
    order, r = await _texts(client, vid, oh)
    assert order == ["newer", "older"] and "X-Comments-Total" not in r.headers
    assert (await client.post(f"{C}{a}/heart", json={"on": True}, headers=oh)).status_code == 404


async def test_CPL5_focus_from_a_notification_puts_that_comment_first_only_if_visible(client, db):
    await _on(db)
    owner, oh = await make_user(db)
    fan, fh = await make_user(db)
    vid = await make_video(db, owner)
    first = await _post(client, fh, vid, "first")
    await _post(client, fh, vid, "second")
    held = await _post(client, fh, vid, "promo deals.ug")
    r = await client.get(f"{C}video/{vid}", params={"focus": first}, headers=oh)
    assert r.json()[0]["content"] == "first"
    r = await client.get(f"{C}video/{vid}", params={"focus": held}, headers=oh)
    assert all(c["content"] != "promo deals.ug" for c in r.json())
