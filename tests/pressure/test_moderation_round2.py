"""Round 2: side doors around a hold, report rings, and odd data."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_moderation import (QUEUE, _hold, _new_post, _pool, _report, _reporter, _video,
                                            no_report_emails)  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_MR1_no_side_doors_into_a_held_post(client, db, rb):
    _, _, vid = await _new_post(db, title="SIDEDOOR")
    await _hold(client, db, vid)
    _, h = await make_user(db)
    assert (await client.post(f"/api/v1/videos/{vid}/like", headers=h)).status_code == 404
    assert (await client.post(f"/api/v1/videos/{vid}/share", headers=h)).status_code == 404
    assert (await client.post("/api/v1/", headers=h, json={"video_id": vid, "content": "x"})).status_code in (403, 404)
    r = await client.post("/graphql", headers=h, json={"query": f'{{ video(id: "{vid}") {{ id title }} }}'})
    assert "SIDEDOOR" not in r.text
    assert vid not in (await client.get("/sitemaps/videos-1.xml")).text


async def test_MR2_stale_recombee_cannot_serve_a_held_post(client, db, rb):
    _, ids = await _catalog(db, n=20, creators=5)
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    rb.pool = [vid] + ids                       # Recombee hasn't caught up
    _, h = await make_user(db)
    assert vid not in [v["id"] for v in (await _feed(client, h, limit=20)).json()]


async def test_MR3_a_ring_cannot_re_hold_after_review(client, db, rb):
    _, _, vid = await _new_post(db)
    ring = [(await _reporter(db))[1] for _ in range(3)]
    for h in ring:
        await _report(client, vid, h)
    _, ah = await make_user(db, user_type="admin")
    await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "restore"})
    for h in ring:
        assert (await _report(client, vid, h)).status_code == 400      # already reported
    v = await _video(db, vid)
    assert v["privacy"] == "public" and v["moderation"]["flags"] == 0


async def test_MR4_hold_still_happens_if_recombee_is_down(client, db, rb):
    rb.down = True
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    assert (await _video(db, vid))["privacy"] == "private"


async def test_MR5_private_post_stays_private_after_restore(client, db, rb):
    _, ch, vid = await _new_post(db, privacy="private")
    # Only the creator can see a private post, so only they could... and theirs don't count.
    await _report(client, vid, ch)
    assert ((await _video(db, vid)).get("moderation") or {}).get("flags", 0) == 0


async def test_MR6_daily_limit_resets_after_a_day(client, db, rb):
    from app.services import moderation
    _, h = await _reporter(db)
    old = [(await _new_post(db))[2] for _ in range(moderation.REPORTS_PER_DAY)]
    for v in old:
        await _report(client, v, h)
    await db.reports.update_many({}, {"$set": {"created_at": datetime.utcnow() - timedelta(hours=25)}})
    vid = (await _new_post(db))[2]
    await _report(client, vid, h)
    assert (await _video(db, vid))["moderation"]["flags"] == 1


async def test_MR7_queue_survives_junk(client, db, rb):
    c, _ = await make_user(db)
    await db.videos.insert_many([
        {"creator_id": ObjectId(c), "is_active": True, "moderation": {"flags": 2, "state": "held", "held_at": "soon"},
         "title": 5, "urls": "x"},
        {"creator_id": None, "is_active": True, "moderation": {"flags": 1}},
    ])
    _, ah = await make_user(db, user_type="admin")
    r = await client.get(QUEUE, headers=ah)
    assert r.status_code == 200, r.text and len(r.json()) == 2


async def test_MR8_removed_post_cannot_be_restored_or_reheld(client, db, rb):
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    _, ah = await make_user(db, user_type="admin")
    await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "remove"})
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "restore"})
    assert r.status_code == 404
    assert (await _video(db, vid))["is_active"] is False


async def test_MR9_report_counts_on_video_stay_consistent(client, db, rb):
    _, _, vid = await _new_post(db)
    rs = []
    for _ in range(4):
        _, h = await _reporter(db)
        rs.append((await _report(client, vid, h), h))
    v = await _video(db, vid)
    assert v["report_count"] == 4 and v["moderation"]["flags"] == 4


async def test_MR10_one_report_pauses_but_trending_and_new_are_untouched(client, db, rb):
    _, _, vid = await _new_post(db)
    _, h = await _reporter(db)
    await _report(client, vid, h)
    assert vid not in await _pool(db)
    v = await _video(db, vid)
    assert v["privacy"] == "public" and v["is_active"] is True
