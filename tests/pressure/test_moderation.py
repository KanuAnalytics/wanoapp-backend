"""Attacks on the safety check: reports pause first viewers, enough reports hold a post
for review, and nobody can use reports to bury a rival or escape a hold."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from recombee_api_client.api_requests import SetItemValues

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_new_feed import _ids

pytestmark = pytest.mark.asyncio

QUEUE = "/api/v1/reports/admin/queue"


@pytest.fixture(autouse=True)
def no_report_emails(monkeypatch):
    from app.api.v1 import reports

    async def _quiet(*a, **k):
        return None
    monkeypatch.setattr(reports, "_send_report_email", _quiet)


async def _reporter(db, days_old=30, **extra):
    return await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(days=days_old), **extra)


async def _report(client, vid, h, category="nudity"):
    return await client.post(f"/api/v1/reports/videos/{vid}/report", headers=h, json={"category": category})


def _rid(r):
    body = r.json()
    return body.get("id") or body.get("_id")


async def _pool(db):
    from app.services import creators
    return [str(d["_id"]) for d in await creators.first_viewers(db, [], None)]


async def _video(db, vid):
    return await db.videos.find_one({"_id": ObjectId(vid)})


async def _new_post(db, **extra):
    c, ch = await _reporter(db, days_old=60)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True, **extra)
    return c, ch, vid


async def _hold(client, db, vid):
    for _ in range(3):
        _, h = await _reporter(db)
        assert (await _report(client, vid, h)).status_code == 201


# ---------------------------------------------------------------- pausing first viewers

async def test_M1_one_trusted_report_pauses_first_viewers_only(client, db, rb):
    _, _, vid = await _new_post(db)
    assert vid in await _pool(db)
    _, h = await _reporter(db)
    assert (await _report(client, vid, h)).status_code == 201
    assert vid not in await _pool(db)
    ids, _ = await _ids(client, None, cursor="start")
    assert vid in ids                                   # one report isn't a verdict


async def test_M2_brand_new_accounts_cannot_pause_a_rival(client, db, rb):
    _, _, vid = await _new_post(db)
    for _ in range(10):
        _, h = await _reporter(db, days_old=0)
        assert (await _report(client, vid, h)).status_code == 201
    assert vid in await _pool(db)
    assert (await _video(db, vid))["privacy"] == "public"


async def test_M3_mass_reporter_stops_counting_after_the_daily_limit(client, db, rb):
    from app.services import moderation
    _, h = await _reporter(db)
    vids = [(await _new_post(db))[2] for _ in range(moderation.REPORTS_PER_DAY + 5)]
    for v in vids:
        assert (await _report(client, v, h)).status_code == 201
    counted = [v for v in vids if ((await _video(db, v)).get("moderation") or {}).get("flags")]
    assert len(counted) == moderation.REPORTS_PER_DAY


async def test_M4_creator_reporting_own_post_does_not_count(client, db, rb):
    _, ch, vid = await _new_post(db)
    await _report(client, vid, ch)
    assert vid in await _pool(db)


async def test_M5_withdrawing_a_report_unpauses(client, db, rb):
    _, _, vid = await _new_post(db)
    _, h = await _reporter(db)
    rid = _rid(await _report(client, vid, h))
    assert vid not in await _pool(db)
    assert (await client.delete(f"/api/v1/reports/reports/{rid}", headers=h)).status_code == 204
    assert vid in await _pool(db)
    assert (await _report(client, vid, h)).status_code == 201          # re-report counts once again
    assert (await _video(db, vid))["moderation"]["flags"] == 1


# ---------------------------------------------------------------- holding for review

async def test_M6_three_trusted_reports_hide_it_everywhere(client, db, rb):
    _, ids = await _catalog(db, n=20, creators=5)
    rb.pool = ids + []
    creator, ch, vid = await _new_post(db, title="HELDCLIP")
    rb.pool = [vid] + ids
    await _hold(client, db, vid)
    v = await _video(db, vid)
    assert v["privacy"] == "private" and v["moderation"]["state"] == "held"
    assert v["moderation"]["prev_privacy"] == "public"
    _, h = await make_user(db)
    assert vid not in [x["id"] for x in (await _feed(client, h, limit=20)).json()]
    assert vid not in (await _ids(client, h, cursor="start"))[0]
    assert vid not in [x["id"] for x in (await client.get("/api/v1/feed/trending")).json()]
    assert (await client.get(f"/v/{vid}")).status_code == 404
    assert (await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": 1})).status_code == 404
    pushed = [r for r in rb.of(SetItemValues) if r.item_id == vid]
    assert pushed and pushed[-1].values.get("privacy") == "private"
    # The creator still sees it, marked under review.
    page = (await client.get("/api/v1/feed/?cursor=start&limit=10&include_own=true", headers=ch)).json()
    mine = [x for x in page if x["id"] == vid]
    assert mine and mine[0]["status"] == "under_review"
    stats = (await client.get("/api/v1/creators/me/stats", headers=ch)).json()
    assert next(p for p in stats["posts"] if p["id"] == vid)["reach_stage"] == "under_review"


async def test_M7_creator_cannot_escape_a_hold_by_changing_privacy(client, db, rb):
    _, ch, vid = await _new_post(db)
    await _hold(client, db, vid)
    r = await client.put(f"/api/v1/videos/{vid}", headers=ch, json={"privacy": "public"})
    assert r.status_code == 409, r.text
    r = await client.post("/graphql", headers=ch, json={
        "query": f'mutation {{ updateVideo(videoId: "{vid}", input: {{privacy: PUBLIC}}) {{ id }} }}'})
    assert r.json().get("errors")
    assert (await _video(db, vid))["privacy"] == "private"
    # Other edits still work.
    assert (await client.put(f"/api/v1/videos/{vid}", headers=ch, json={"title": "new title"})).status_code == 200


async def test_M8_unlisted_post_restores_to_unlisted(client, db, rb):
    _, _, vid = await _new_post(db, privacy="unlisted")
    await _hold(client, db, vid)
    assert (await _video(db, vid))["privacy"] == "private"
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "restore"})
    assert r.status_code == 200, r.text
    assert (await _video(db, vid))["privacy"] == "unlisted"


async def test_M9_simultaneous_reports_hold_exactly_once(client, db, rb):
    _, _, vid = await _new_post(db)
    hs = [(await _reporter(db))[1] for _ in range(6)]
    rs = await asyncio.gather(*[_report(client, vid, h) for h in hs])
    assert all(r.status_code == 201 for r in rs)
    v = await _video(db, vid)
    assert v["moderation"]["flags"] == 6 and v["moderation"]["state"] == "held"
    assert v["moderation"]["prev_privacy"] == "public"


async def test_M10_withdrawals_do_not_release_a_hold(client, db, rb):
    _, _, vid = await _new_post(db)
    rids, hs = [], []
    for _ in range(3):
        _, h = await _reporter(db)
        hs.append(h)
        rids.append(_rid(await _report(client, vid, h)))
    for rid, h in zip(rids, hs):
        await client.delete(f"/api/v1/reports/reports/{rid}", headers=h)
    v = await _video(db, vid)
    assert v["moderation"]["state"] == "held" and v["privacy"] == "private"


# ---------------------------------------------------------------- admin review

async def test_M11_queue_is_admin_only_and_held_first(client, db, rb):
    _, _, flagged = await _new_post(db)
    _, h = await _reporter(db)
    await _report(client, flagged, h)
    _, _, held = await _new_post(db)
    await _hold(client, db, held)
    _, uh = await make_user(db)
    assert (await client.get(QUEUE, headers=uh)).status_code == 403
    assert (await client.get(QUEUE)).status_code == 401
    _, ah = await make_user(db, user_type="admin")
    q = (await client.get(QUEUE, headers=ah)).json()
    assert [x["id"] for x in q][:2] == [held, flagged]
    assert q[0]["state"] == "held" and q[0]["flags"] == 3 and q[0]["reports"] >= 3


async def test_M12_restore_puts_it_back_and_resets_the_count(client, db, rb):
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "restore"})
    assert r.status_code == 200
    v = await _video(db, vid)
    assert v["privacy"] == "public" and v["moderation"]["state"] == "cleared" and v["moderation"]["flags"] == 0
    assert vid in (await _ids(client, None, cursor="start"))[0]
    assert vid in await _pool(db)
    assert await db.reports.count_documents({"video_id": ObjectId(vid), "status": "dismissed"}) == 3
    _, h = await _reporter(db)                 # one more report pauses again but doesn't re-hold
    await _report(client, vid, h)
    v = await _video(db, vid)
    assert v["privacy"] == "public" and v["moderation"]["flags"] == 1


async def test_M13_remove_takes_it_down(client, db, rb):
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json={"action": "remove"})
    assert r.status_code == 200
    v = await _video(db, vid)
    assert v["is_active"] is False and v["moderation"]["state"] == "removed"
    assert await db.reports.count_documents({"video_id": ObjectId(vid), "status": "resolved"}) == 3


@pytest.mark.parametrize("vid,body", [("nope", {"action": "restore"}), (None, {"action": "explode"}),
                                      (None, {}), ("000000000000000000000000", {"action": "restore"})])
async def test_M14_bad_decisions_are_refused(client, db, rb, vid, body):
    if vid is None:
        vid = (await _new_post(db))[2]
    _, ah = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=ah, json=body)
    assert r.status_code in (400, 404, 422), (vid, body, r.status_code)


async def test_M15_non_admin_cannot_decide(client, db, rb):
    _, _, vid = await _new_post(db)
    await _hold(client, db, vid)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/reports/admin/videos/{vid}/decision", headers=h, json={"action": "restore"})
    assert r.status_code == 403
    assert (await _video(db, vid))["privacy"] == "private"


# ---------------------------------------------------------------- new accounts

async def test_M16_brand_new_creator_gets_a_small_test(client, db, rb):
    from app.services import creators
    fresh_c, _ = await make_user(db, created_at=datetime.now(timezone.utc) - timedelta(hours=2))
    vid = await make_video(db, fresh_c, created_at=ago(minutes=30), isReadyToStream=True)
    await db.video_reach.insert_one({"_id": ObjectId(vid), "impressions": 15, "fy_views": creators.NEW_ACCOUNT_TEST,
                                     "fy_portion_sum": 9.0, "fy_finished": 6})
    pool = {str(d["_id"]): d for d in await creators.first_viewers(db, [], None)}
    assert pool[vid]["stage"] == "boost"        # its test was the smaller one
    await db.video_reach.update_one({"_id": ObjectId(vid)}, {"$set": {"fy_views": creators.NEW_ACCOUNT_BOOST_CAP}})
    assert vid not in await _pool(db)
