"""Shares are counted (Nov 2026): once per person per post, never the
creator's own, from the share button and from a post sent in a chat."""

import pytest
from bson import ObjectId
from recombee_api_client.api_requests import AddPurchase

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import FakeRecombee, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_SH1_counted_once_per_person(client, db, rb):
    creator, ch = await make_user(db)
    vid = await make_video(db, creator)
    a, ha = await make_user(db)
    b, hb = await make_user(db)
    for _ in range(3):
        r = await client.post(f"/api/v1/videos/{vid}/share", headers=ha)
        assert r.status_code == 200
    await client.post(f"/api/v1/videos/{vid}/share", headers=hb)
    await client.post(f"/api/v1/videos/{vid}/share", headers=ch)          # the creator's own
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["shares_count"] == 2
    assert len(rb.of(AddPurchase)) == 5                                   # Recombee hears every one
    hours = [d async for d in db.video_stats_hourly.find({"video_id": ObjectId(vid)})]
    assert sum(h.get("shares", 0) for h in hours) == 2


async def test_SH2_needs_an_account(client, db, rb):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    r = await client.post(f"/api/v1/videos/{vid}/share")
    assert r.status_code in (401, 403)


async def test_SH3_a_post_sent_in_a_chat_counts(db, rb):
    from app.services.shares import count_share
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    a, _ = await make_user(db)
    assert await count_share(db, vid, a) is True
    assert await count_share(db, vid, a) is False
    assert await count_share(db, vid, creator) is False
    assert await count_share(db, "not-an-id", a) is False
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["shares_count"] == 1
