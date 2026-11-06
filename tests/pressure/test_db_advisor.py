"""Atlas Performance Advisor findings on the live cluster (Nov 2026), turned into attacks.

Live: ~10k videos, ~9k users. Slow queries each read the whole collection:
videos by creator + remoteUrl_CF / remoteUrl / stream_uid (146 ms), users by
expo_push_tokens (247 ms), videos by isReadyToStream (+ created_at). Five
indexes are redundant (prefixes of compound ones) and our startup code keeps
creating them. The plan checks need a real MongoDB: WANO_REAL_MONGO."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import REAL_MONGO, make_user, make_video

pytestmark = pytest.mark.asyncio
real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")

UID = "0123456789abcdef0123456789abcdef"


def _plan(explain) -> str:
    return str(explain.get("queryPlanner", explain))


async def _examined(db, coll, q, sort=None) -> int:
    """Documents the server actually reads for this query (an index scan over
    the whole range is a collection scan in disguise)."""
    cmd = {"find": coll, "filter": q}
    if sort:
        cmd["sort"] = dict(sort)
    out = await db.command("explain", cmd, verbosity="executionStats")
    return out["executionStats"]["totalDocsExamined"]


async def _legacy_world(db, n=3000):
    """Lots of videos, most with stream_uid, a few old ones without, some photos."""
    now = datetime.utcnow()
    docs = []
    for i in range(n):
        uid = f"{i:032x}"
        d = {"creator_id": ObjectId(), "is_active": True, "privacy": "public", "created_at": now - timedelta(minutes=i),
             "remoteUrl_CF": f"https://videodelivery.net/{uid}/manifest/video.m3u8", "isReadyToStream": True}
        if i % 10:
            d["stream_uid"] = uid
        if i % 50 == 0:
            d.update({"media_type": "photo", "stream_uid": None, "remoteUrl_CF": None, "images": ["https://x/p.jpg"]})
        docs.append(d)
    await db.videos.insert_many(docs)


@real_only
async def test_A1_stream_lookups_use_an_index(client, db):
    from app.services.video_posts import stream_uid_exact_query, stream_uid_reference_query
    await _legacy_world(db)
    from app.services.video_posts import backfill_stream_uids
    await backfill_stream_uids(db, apply=True)          # the one-off step after deploy
    for q in (stream_uid_exact_query(UID),
              {**stream_uid_reference_query(UID), "creator_id": {"$ne": ObjectId()}},
              {**stream_uid_exact_query(UID), "isReadyToStream": {"$ne": True}}):
        n = await _examined(db, "videos", q)
        assert n <= 100, f"read {n} of 3000 videos"


@real_only
async def test_A2_push_token_lookup_uses_an_index(client, db):
    await db.users.insert_many([{"username": f"t{i}", "expo_push_tokens": [f"tok{i}"]} for i in range(2000)])
    n = await _examined(db, "users", {"_id": {"$ne": ObjectId()}, "expo_push_tokens": "tok5"})
    assert n <= 2, f"read {n} of 2000 users"


@real_only
async def test_A3_stuck_upload_scan_uses_an_index(client, db):
    await _legacy_world(db)
    now = datetime.utcnow()
    q = {"isReadyToStream": False, "media_type": {"$ne": "photo"},
         "created_at": {"$lte": now - timedelta(minutes=10), "$gte": now - timedelta(days=7)}}
    n = await _examined(db, "videos", q, [("created_at", -1)])
    assert n <= 5, f"read {n} of 3000 videos"


@real_only
async def test_A4_redundant_indexes_are_not_created(client, db):
    names = {i["name"] async for i in db.videos.list_indexes()}
    comments = {i["name"] async for i in db.comments.list_indexes()}
    assert not {"created_at_-1", "creator_id_1", "is_active_1_privacy_1"} & names, names
    assert not {"user_id_1", "video_id_1"} & comments, comments
    # ...while the compound indexes that cover them are there.
    assert {"created_at_-1__id_-1", "creator_id_1_created_at_-1", "is_active_1_privacy_1_created_at_-1"} <= names
    assert {"user_id_1_is_active_1_created_at_-1", "video_id_1_parent_id_1_is_active_1_created_at_-1"} <= comments


async def test_B1_legacy_post_without_stream_uid_still_found(client, db):
    from app.services.video_posts import stream_uid_exact_query, stream_uid_reference_query
    c, _ = await make_user(db)
    vid = await make_video(db, c, remoteUrl_CF=f"https://videodelivery.net/{UID}/manifest/video.m3u8")
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$unset": {"stream_uid": ""}})
    assert await db.videos.find_one(stream_uid_exact_query(UID))
    assert await db.videos.find_one(stream_uid_reference_query(UID))
    # Old docs that stored stream_uid: null explicitly too.
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"stream_uid": None}})
    assert await db.videos.find_one(stream_uid_exact_query(UID))
    # And the modern path.
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$set": {"stream_uid": UID, "remoteUrl_CF": None}})
    assert await db.videos.find_one(stream_uid_exact_query(UID))


async def test_B2_exact_match_does_not_match_a_longer_uid_or_another_host_path(client, db):
    from app.services.video_posts import stream_uid_exact_query
    c, _ = await make_user(db)
    await make_video(db, c, remoteUrl_CF=f"https://videodelivery.net/x/{UID}/manifest/video.m3u8", stream_uid=None)
    await make_video(db, c, remoteUrl_CF=f"https://videodelivery.net/{UID}ff/manifest/video.m3u8", stream_uid=None)
    assert not await db.videos.find_one(stream_uid_exact_query(UID))


async def test_C1_backfill_fills_stream_uid_on_old_posts(client, db):
    from app.services.video_posts import backfill_stream_uids
    c, _ = await make_user(db)
    old = await make_video(db, c, remoteUrl_CF=f"https://videodelivery.net/{UID}/manifest/video.m3u8")
    await db.videos.update_one({"_id": ObjectId(old)}, {"$unset": {"stream_uid": ""}})
    photo = await make_video(db, c, media_type="photo", remoteUrl_CF=None, images=["https://x/p.jpg"])
    await db.videos.update_one({"_id": ObjectId(photo)}, {"$unset": {"stream_uid": ""}})
    junk = await make_video(db, c, remoteUrl_CF="https://evil.example/not-a-stream", remoteUrl=None)
    await db.videos.update_one({"_id": ObjectId(junk)}, {"$unset": {"stream_uid": ""}})
    dry = await backfill_stream_uids(db)
    assert dry == {"would_fix": 1, "fixed": 0, "applied": False}
    assert "stream_uid" not in await db.videos.find_one({"_id": ObjectId(old)})
    done = await backfill_stream_uids(db, apply=True)
    assert done["fixed"] == 1
    assert (await db.videos.find_one({"_id": ObjectId(old)}))["stream_uid"] == UID
    assert (await db.videos.find_one({"_id": ObjectId(photo)})).get("stream_uid") is None
    assert (await db.videos.find_one({"_id": ObjectId(junk)})).get("stream_uid") is None
    assert (await backfill_stream_uids(db, apply=True))["fixed"] == 0


async def test_C2_backfill_endpoint_is_admin_only(client, db):
    _, h = await make_user(db)
    _, ah = await make_user(db, user_type="admin")
    assert (await client.post("/api/v1/recombee/backfill-stream-uids", headers=h)).status_code == 403
    r = await client.post("/api/v1/recombee/backfill-stream-uids", headers=ah)
    assert r.status_code == 200 and r.json()["applied"] is False


async def test_C3_webhook_still_finds_a_legacy_post(client, db):
    from app.routes import upload_video
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=False,
                           remoteUrl_CF=f"https://videodelivery.net/{UID}/manifest/video.m3u8")
    await db.videos.update_one({"_id": ObjectId(vid)}, {"$unset": {"stream_uid": ""}})
    assert await upload_video.mark_stream_ready(UID)
    assert (await db.videos.find_one({"_id": ObjectId(vid)}))["isReadyToStream"] is True


@real_only
async def test_D1_drop_script_only_drops_covered_indexes(client, db):
    from scripts.drop_redundant_indexes import run
    await db.videos.create_index([("created_at", -1)])          # an old production index
    await db.videos.create_index([("creator_id", 1)])
    await db.comments.create_index([("user_id", 1)])
    await db.videos.drop_index("creator_id_1_created_at_-1")     # pretend its cover is missing
    dry = await run(db, apply=False)
    assert ("videos", "created_at_-1", "would drop") in dry
    assert ("videos", "creator_id_1", "kept: covering index creator_id_1_created_at_-1 missing") in dry
    assert "created_at_-1" in {i["name"] async for i in db.videos.list_indexes()}
    done = await run(db, apply=True)
    assert ("videos", "created_at_-1", "dropped") in done and ("comments", "user_id_1", "dropped") in done
    names = {i["name"] async for i in db.videos.list_indexes()}
    assert "created_at_-1" not in names and "creator_id_1" in names and "created_at_-1__id_-1" in names
    assert all(w in ("not there", "kept: covering index creator_id_1_created_at_-1 missing")
               for _, _, w in await run(db, apply=True))
