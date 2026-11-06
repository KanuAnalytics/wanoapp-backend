"""Production Query Insights (Atlas, 24 h) and a profile of every main endpoint on
production-sized data (scripts/perf_profile.py), turned into attacks.

Found: login reads users one by one (642 to find one, all 9000 for a wrong
identifier); every plain email / phone lookup does the same (sign-up checks,
forgot password, codes); sign-up code collections have no index; "not
interested" reads a user's whole watch history; user search reads every user;
For You re-reads 120 days of posts for every candidate creator's streak and
runs the unindexed popular backup on every request Recombee comes up short."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import REAL_MONGO, make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio
real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")


async def _examined(db, coll, q, **extra) -> int:
    out = await db.command("explain", {"find": coll, "filter": q, **extra}, verbosity="executionStats")
    return out["executionStats"]["totalDocsExamined"]


async def _users(db, n=9000):
    docs = []
    for i in range(n):
        d = {"username": f"user{i}", "display_name": f"Person {i}", "is_active": i % 50 != 0}
        if i % 3:
            d["email"] = f"u{i}@x.com"
        if i % 2:
            d["phone_number"] = f"+2567{i:08d}"
        docs.append(d)
    await db.users.insert_many(docs)


@real_only
async def test_L1_login_reads_one_user_not_all_of_them(client, db):
    from app.api.v1.auth import _identifier_query
    await _users(db)
    for ident in ("user4242", "u4243@x.com", "+256700004243", "nobody@x.com", "ghost"):
        n = await _examined(db, "users", _identifier_query(ident), limit=1)
        assert n <= 1, (ident, n)


@real_only
async def test_L2_every_plain_email_and_phone_lookup_is_indexed(client, db):
    await _users(db)
    for q in ({"email": "u4243@x.com"}, {"email": "missing@x.com"},
              {"phone_number": "+256700004243"}, {"phone_number": "+256799999999"}):
        assert await _examined(db, "users", q, limit=1) <= 1, q


@real_only
async def test_L3_sign_up_codes_are_indexed(client, db):
    await db.registration_otps.insert_many([{"email": f"u{i}@x.com", "otp": "x"} for i in range(3000)])
    await db.registration_whatsapp_otps.insert_many([{"phone_number": f"+2567{i:08d}"} for i in range(3000)])
    assert await _examined(db, "registration_otps", {"email": "u77@x.com"}, limit=1) <= 1
    assert await _examined(db, "registration_whatsapp_otps", {"phone_number": "+256700000077"}, limit=1) <= 1


@real_only
async def test_L4_not_interested_lookup_does_not_read_the_whole_history(client, db):
    uid = ObjectId()
    await db.watch_history.insert_many([{"user_id": uid, "video_id": ObjectId(), "watched_at": datetime.utcnow()}
                                        for _ in range(3000)])
    assert await _examined(db, "watch_history", {"user_id": uid, "not_interested": True}) <= 1


@real_only
async def test_S1_user_search_reads_only_matches(client, db):
    await _users(db)
    _, h = await make_user(db)
    await db.command("profile", 0)
    await db.system.profile.drop()
    await db.command("profile", 2)
    r = await client.get("/api/v1/users/search?q=person 12&limit=20", headers=h)
    await db.command("profile", 0)
    assert r.status_code == 200, r.text
    ops = [p async for p in db.system.profile.find({"ns": f"{db.name}.users"})]
    worst = max(p.get("docsExamined", 0) + p.get("keysExamined", 0) for p in ops)
    assert worst <= 400, f"search read {worst} users / index entries (9000 users)"


@real_only     # case-insensitive matching uses a collation; the in-memory mock has none
async def test_S2_user_search_is_still_case_insensitive_prefix(client, db):
    _, h = await make_user(db)
    for name, disp, active in (("amina_k", "Amina Kato", True), ("AMINATA", "Ata", True),
                               ("bob", "aminA from Lagos", True), ("aminz", "Gone", False), ("xamina", "x", True)):
        await make_user(db, username=name, display_name=disp, is_active=active)
    for q in ("amina", "AMINA", "Amina"):
        r = await client.get(f"/api/v1/users/search?q={q}&limit=20", headers=h)
        assert r.status_code == 200, r.text
        names = {u["username"] for u in r.json()}
        assert names == {"amina_k", "AMINATA", "bob"}, (q, names)
    r = await client.get("/api/v1/users/search?q=.*&limit=20", headers=h)     # not a regex
    assert r.status_code == 200 and r.json() == []
    r = await client.get("/api/v1/users/search?q=amina&limit=1&skip=1", headers=h)
    assert len(r.json()) == 1


async def test_F1_for_you_streaks_only_look_back_a_week(client, db, rb, monkeypatch):
    from app.services import creators
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    c, _ = await make_user(db)
    await make_video(db, c, created_at=ago(hours=2), isReadyToStream=True)
    seen = []
    real = creators.streaks

    async def spy(db_, ids_, now=None, max_days=creators.STREAK_MAX_DAYS):
        seen.append(max_days)
        return await real(db_, ids_, now=now, max_days=max_days)
    monkeypatch.setattr(creators, "streaks", spy)
    _, h = await make_user(db)
    await _feed(client, h, limit=20)
    assert seen and max(seen) <= 8, seen


async def test_F2_week_lookback_still_ranks_streaks_right(client, db, rb):
    from app.services import creators
    c, _ = await make_user(db)
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": ObjectId(c), "privacy": "public", "is_active": True,
                                  "isReadyToStream": True, "created_at": now - timedelta(days=d)} for d in range(40)])
    short = (await creators.streaks(db, [ObjectId(c)], max_days=8))[ObjectId(c)]
    creators.clear_cache()
    full = (await creators.streaks(db, [ObjectId(c)]))[ObjectId(c)]
    assert full["current"] >= 39 and short["current"] == 8
    assert creators.test_audience(short["current"]) == creators.test_audience(full["current"])


async def test_F3_popular_backup_is_shared_not_recomputed_per_request(client, db, rb, monkeypatch):
    from app.services import for_you
    _, ids = await _catalog(db, n=30, creators=6)
    rb.pool = ids
    rb.down = True
    runs = []
    real = for_you._popular_pool

    async def spy(db_):
        runs.append(1)
        return await real(db_)
    monkeypatch.setattr(for_you, "_popular_pool", spy)
    for _ in range(5):
        _, h = await make_user(db)
        r = await _feed(client, h, limit=10)
        assert r.status_code == 200 and len(r.json()) == 10
    assert len(runs) <= 1, f"popular pool rebuilt {len(runs)} times"


async def test_F4_shared_popular_pool_still_excludes_watched_own_and_inactive(client, db, rb):
    from app.services import for_you
    for_you.clear_cache()
    _, ids = await _catalog(db, n=40, creators=8)
    rb.pool = []
    rb.down = True
    me, h = await make_user(db)
    mine = await make_video(db, me, created_at=ago(days=1), isReadyToStream=True, likes_count=10 ** 6)
    await client.post(f"/api/v1/videos/{ids[0]}/watch", headers=h, json={"portion": 1})
    gone, _ = await make_user(db, is_active=False)
    dead = await make_video(db, gone, created_at=ago(days=1), isReadyToStream=True, likes_count=10 ** 6)
    page = [v["id"] for v in (await _feed(client, h, limit=20)).json()]
    assert mine not in page and dead not in page and ids[0] not in page


@real_only
async def test_S3_one_letter_search_is_bounded(client, db):
    await _users(db)
    _, h = await make_user(db)
    await db.command("profile", 0)
    await db.system.profile.drop()
    await db.command("profile", 2)
    r = await client.get("/api/v1/users/search?q=p&limit=20", headers=h)
    await db.command("profile", 0)
    assert r.status_code == 200 and len(r.json()) == 20
    ops = [p async for p in db.system.profile.find({"ns": f"{db.name}.users"})]
    assert max(p.get("docsExamined", 0) for p in ops) <= 600


@real_only
async def test_F5_popular_pool_build_walks_the_likes_index(client, db):
    from app.services import for_you
    now = datetime.now(timezone.utc)
    await db.videos.insert_many([{"creator_id": ObjectId(), "privacy": "public", "is_active": True, "isReadyToStream": True,
                                  "status": "published", "likes_count": i % 997,
                                  "created_at": now - timedelta(hours=i % 2000)} for i in range(8000)])
    q = {**for_you.showable_filter(), "created_at": {"$gte": now - timedelta(days=60)}}
    out = await db.command("explain", {"find": "videos", "filter": q, "sort": {"likes_count": -1},
                                       "limit": for_you.POPULAR_POOL_SIZE}, verbosity="executionStats")
    assert out["executionStats"]["totalDocsExamined"] <= 2000
