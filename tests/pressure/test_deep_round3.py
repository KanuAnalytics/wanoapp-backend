"""Round 3: deeper attacks on views, trending and For You."""

import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from recombee_api_client.api_requests import MergeUsers

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _feed, _world, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_trending import _clear_cache, _engage, _trending_ids, _users
from tests.pressure.test_trending_round2 import _stats

pytestmark = pytest.mark.asyncio


def _views(vid):
    from app.services.metrics_service import metrics_buffer
    return metrics_buffer.views_buffer.get(vid, 0)


async def _flushless_views(db, vid):
    """Counted views = buffered + flushed."""
    from app.services.metrics_service import metrics_buffer
    doc = await db.videos.find_one({"_id": ObjectId(vid)}, {"views_count": 1})
    return (doc.get("views_count") or 0) + metrics_buffer.views_buffer.get(vid, 0)


# ------------------------------------------------------------------ views

async def test_D1_spoofed_forwarded_for_cannot_dodge_ip_cap(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    for i in range(1500):
        # attacker invents the left part; the real proxy appends the true IP
        await client.post(f"/api/v1/videos/{vid}/watch?authenticated=false",
                          headers={"X-Device-Id": f"bot{i}", "X-Forwarded-For": f"7.7.{i // 250}.{i % 250}, 196.43.1.9"},
                          json={"portion": 1})
    assert await _flushless_views(db, vid) <= 50, await _flushless_views(db, vid)


async def test_D2_one_ip_cannot_flood_one_video_logged_out(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    other = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    for i in range(300):
        await client.post(f"/api/v1/videos/{vid}/watch?authenticated=false",
                          headers={"X-Device-Id": f"x{i}", "X-Forwarded-For": "41.1.1.1"}, json={"portion": 1})
    assert await _flushless_views(db, vid) <= 50
    # ...but the same IP (a carrier) still counts for other videos
    r = await client.post(f"/api/v1/videos/{other}/watch?authenticated=false",
                          headers={"X-Device-Id": "fresh", "X-Forwarded-For": "41.1.1.1"}, json={"portion": 1})
    assert r.status_code == 200 and await _flushless_views(db, other) == 1


async def test_D3_duplicate_key_race_is_handled(client, db, rb, monkeypatch):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    _, h = await make_user(db)
    coll_type = type(db.view_dedupe)
    real = coll_type.find_one_and_update

    async def racing(self, *a, **k):
        if self.name == "view_dedupe":
            raise DuplicateKeyError("E11000 duplicate key")
        return await real(self, *a, **k)
    monkeypatch.setattr(coll_type, "find_one_and_update", racing)
    r = await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": 1})
    assert r.status_code == 200, r.text
    assert _views(vid) == 0   # the other concurrent request counted it


async def test_D4_old_app_logged_out_viewers_still_counted(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=1), isReadyToStream=True)
    for i in range(20):   # no X-Device-Id: old builds; different people on different IPs
        await client.post(f"/api/v1/videos/{vid}/watch?authenticated=false",
                          headers={"X-Forwarded-For": f"102.80.0.{i}"}, json={"portion": 1})
    assert await _flushless_views(db, vid) == 20


async def test_D5_ip_tracking_memory_is_bounded():
    from app.services import trending
    trending.reset_limits()
    for i in range(150_000):
        trending._ip_allows(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert len(trending._ip_hits) <= trending.MAX_TRACKED_IPS


@pytest.mark.parametrize("portion", ["NaN", "Infinity", "-1", "1.5", "\"lots\""])
async def test_D6_bad_watch_portions_rejected(client, db, rb, portion):
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=True)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/videos/{vid}/watch", headers={**h, "Content-Type": "application/json"},
                          content=f'{{"portion": {portion}}}')
    assert r.status_code == 422, (portion, r.status_code)
    assert _views(vid) == 0


# ------------------------------------------------------------------ trending sabotage and robustness

async def test_D7_skip_sabotage_cannot_bury_a_rival(client, db, rb):
    from app.services import trending
    c, _ = await make_user(db)
    vid = await make_video(db, c, created_at=ago(hours=3), isReadyToStream=True)
    await _engage(client, vid, await _users(db, 10), like=True)
    trending.clear_cache()
    before = {v: s for v, s, _, _ in await trending._compute(db)}[vid]
    await _engage(client, vid, await _users(db, 100), portion=0.01)   # 100 accounts skip it on purpose
    trending.clear_cache()
    after = {v: s for v, s, _, _ in await trending._compute(db)}.get(vid, 0)
    assert after >= 0.5 * before, (before, after)
    assert vid in await _trending_ids(client)


async def test_D8_unlisted_video_never_trends_even_with_real_engagement(client, db, rb):
    c, _ = await make_user(db)
    vid = await make_video(db, c, privacy="unlisted", created_at=ago(hours=2), isReadyToStream=True)
    await _engage(client, vid, await _users(db, 12), like=True)   # shared by link, lots of love
    assert (await _stats(db, vid)).get("likes") == 12
    await _clear_cache()
    assert vid not in await _trending_ids(client)


async def test_D9_trending_failure_degrades_gracefully(client, db, rb, monkeypatch):
    from app.services import trending
    ids, *_ = await _world(db)
    rb.pool = ids

    async def boom(*a, **k):
        raise RuntimeError("stats collection unavailable")
    monkeypatch.setattr(trending, "_compute", boom)
    trending.clear_cache()
    r = await client.get("/api/v1/feed/trending")
    assert r.status_code == 200 and r.json() == []
    _, h = await make_user(db)
    page = await _feed(client, h, limit=10)
    assert page.status_code == 200 and len(page.json()) == 10


async def test_D10_lowercase_country_same_as_upper(client, db, rb):
    c, _ = await make_user(db)
    ug = await make_video(db, c, country="UG", created_at=ago(hours=2), isReadyToStream=True)
    ng = await make_video(db, c, country="NG", created_at=ago(hours=2), isReadyToStream=True)
    await _engage(client, ug, await _users(db, 6), like=True)
    await _engage(client, ng, await _users(db, 12), like=True)
    await _clear_cache()
    assert (await _trending_ids(client, None, country="ug"))[0] == ug


# ------------------------------------------------------------------ identity

async def test_D11_device_id_cannot_impersonate_a_user(client, db, rb):
    from app.core.security import get_password_hash
    victim, _ = await make_user(db)
    await make_user(db, username="att1", password_hash=get_password_hash("Password1"))
    await client.post("/api/v1/auth/login/json", headers={"X-Device-Id": victim},
                      json={"username": "att1", "password": "Password1"})
    merges = rb.of(MergeUsers)
    assert merges and merges[0].source_user_id == f"anon-{victim}" and merges[0].source_user_id != victim


async def test_D12_no_device_header_no_merge(client, db, rb):
    from app.core.security import get_password_hash
    await make_user(db, username="att2", password_hash=get_password_hash("Password1"))
    await client.post("/api/v1/auth/login/json", json={"username": "att2", "password": "Password1"})
    assert not rb.of(MergeUsers)


# ------------------------------------------------------------------ scale

async def test_D13_report_scales_and_is_correct(client, db, rb):
    now = datetime.now(timezone.utc)
    docs = [{"viewer": f"v{i % 900}", "video_id": ObjectId(), "variant": "mix" if i % 2 else "control",
             "source": ["personal", "fresh", "trending"][i % 3], "at": now - timedelta(minutes=i % 600),
             **({"portion": (i % 10) / 10} if i % 4 else {})} for i in range(30000)]
    await db.feed_impressions.insert_many(docs)
    _, ah = await make_user(db, user_type="admin")
    t = time.perf_counter()
    r = await client.get("/api/v1/metrics/for-you?days=1", headers=ah)
    took = time.perf_counter() - t
    assert r.status_code == 200
    v = r.json()["variants"]
    assert v["mix"]["impressions"] + v["control"]["impressions"] == 30000
    assert v["mix"]["watched"] + v["control"]["watched"] == 22500
    from app.services import for_you
    import inspect
    assert "aggregate(" in inspect.getsource(for_you.report), "report scans every impression in Python"
    assert took < 10


async def test_D14_heavy_watcher_feed_stays_fast_and_fresh(client, db, rb):
    ids, *_ = await _world(db)
    rb.pool = ids
    uid, h = await make_user(db)
    now = datetime.utcnow()
    await db.watch_history.insert_many([{"user_id": ObjectId(uid), "video_id": ObjectId(),
                                         "watched_at": now - timedelta(minutes=i)} for i in range(8000)])
    for v in ids[:5]:   # most recent watches must still be excluded
        await client.post(f"/api/v1/videos/{v}/watch", headers=h, json={"portion": 1})
    t = time.perf_counter()
    page = (await _feed(client, h, limit=20)).json()
    assert time.perf_counter() - t < 3
    assert not {v["id"] for v in page} & set(ids[:5])
    from app.services import for_you
    import inspect
    assert ".limit(" in inspect.getsource(for_you.build_page), "loads a heavy user's entire watch history"


async def test_D15_twenty_thousand_hourly_rows(client, db, rb):
    from app.services import trending
    c, _ = await make_user(db)
    vids = [ObjectId(await make_video(db, c, created_at=ago(hours=5), isReadyToStream=True)) for _ in range(200)]
    hour = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    await db.video_stats_hourly.insert_many([{"_id": f"{v}:{h}", "video_id": v, "hour": hour - timedelta(hours=h),
                                              "views": 5, "finished": 3, "likes": 2}
                                             for v in vids for h in range(0, 48)][:20000])
    trending.clear_cache()
    t = time.perf_counter()
    ranked = await trending._compute(db)
    assert time.perf_counter() - t < 5 and len(ranked) == 200
