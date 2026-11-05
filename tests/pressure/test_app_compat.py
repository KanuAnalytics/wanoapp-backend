"""
Regressions the security-fix merge introduced for the builds already in the
stores and for existing accounts. Each test fails without its fix.
"""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import get_password_hash
from app.services import for_you, stitch_jobs
from app.services.recommendations import LEGACY_ANON_USER
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


# --- Accounts whose stored email kept its capitals -----------------------
# Registration used to save the address as typed; the merge compares against
# a lowercased one, which never equals it.

async def test_login_works_when_the_stored_email_has_capitals(client, db):
    await make_user(db, email="John.Doe@Example.com",
                    password_hash=get_password_hash("hunter2"))

    r = await client.post("/api/v1/auth/login/json",
                          json={"username": "john.doe@example.com", "password": "hunter2"})
    assert r.status_code == 200, r.text


async def test_forgot_password_finds_an_email_with_capitals(client, db, monkeypatch):
    from app.api.v1 import auth

    async def fake_send(to_email, username):
        return True, "123456"

    monkeypatch.setattr(auth.email_service, "send_password_reset_otp", fake_send)
    uid, _ = await make_user(db, email="Jane.Roe@Example.com")

    r = await client.post("/api/v1/auth/forgot-password",
                          json={"username_or_email": "jane.roe@example.com"})
    assert r.status_code == 200, r.text
    # The reply is the same either way, so check a code really was issued.
    user = await db.users.find_one({"_id": ObjectId(uid)})
    assert user.get("password_reset_otp_hash")


async def test_the_same_address_cannot_be_registered_twice_in_another_case(client, db):
    await make_user(db, email="Taken@Example.com")

    r = await client.post("/api/v1/auth/request-registration-otp",
                          json={"email": "taken@example.com", "channel": "email"})
    assert r.status_code == 400, r.text
    assert "already registered" in r.json()["detail"].lower()


# --- Views from builds that never call POST /watch ----------------------

async def test_opening_a_video_counts_a_view(client, db):
    creator, _ = await make_user(db)
    viewer, headers = await make_user(db)
    vid = await make_video(db, creator)

    r = await client.get(f"/api/v1/videos/{vid}", headers=headers)
    assert r.status_code == 200, r.text

    from app.services.metrics_service import metrics_buffer
    assert (await metrics_buffer.get_buffered_counts(vid))["views"] == 1


async def test_opening_a_video_twice_counts_one_view(client, db):
    creator, _ = await make_user(db)
    viewer, headers = await make_user(db)
    vid = await make_video(db, creator)

    await client.get(f"/api/v1/videos/{vid}", headers=headers)
    await client.get(f"/api/v1/videos/{vid}", headers=headers)
    # ...and the newer build's /watch for the same video must not add a second.
    await client.post(f"/api/v1/videos/{vid}/watch", headers=headers,
                      json={"portion": 0.9})

    from app.services.metrics_service import metrics_buffer
    assert (await metrics_buffer.get_buffered_counts(vid))["views"] == 1


# --- For You for builds that send no device id --------------------------
# They all arrive as one viewer, so one person's impressions must not
# narrow anyone else's feed.

async def test_legacy_viewers_do_not_share_an_exclusion_list(db, monkeypatch):
    async def no_recombee(*a, **kw):
        return None

    monkeypatch.setattr(for_you, "recombee_send", no_recombee)

    creator, _ = await make_user(db)
    seen = await make_video(db, creator)
    # Somebody else on the same build was already shown it.
    await db.feed_impressions.insert_one({
        "viewer": LEGACY_ANON_USER, "video_id": ObjectId(seen),
        "at": datetime.now(timezone.utc),
    })

    page = await for_you.build_page(db, current_user=None, viewer=LEGACY_ANON_USER,
                                    scenario="for_you", limit=5, next_recomm_id=None,
                                    featured=None)
    assert seen in [item["id"] for item in page.items]


async def test_nothing_is_stored_under_the_shared_legacy_viewer(db):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)

    await for_you.record_impressions(db, viewer=LEGACY_ANON_USER, current_user=None,
                                     variant="mix", recomm_id=None, items=[{"id": vid}])
    assert await db.feed_impressions.count_documents({}) == 0


# --- Stitch jobs whose clips are still uploading ------------------------

async def test_a_job_waiting_for_a_slow_upload_is_not_failed(db):
    old = datetime.utcnow() - timedelta(hours=2)
    job = await db.pending_videos.insert_one({
        "status": stitch_jobs.STATUS_WAITING, "created_at": old, "updated_at": old,
        "clips": [{"stream_uid": "a" * 32, "ready": False}],
    })

    await stitch_jobs.fail_stale_jobs(db)

    doc = await db.pending_videos.find_one({"_id": job.inserted_id})
    assert doc["status"] == stitch_jobs.STATUS_WAITING


async def test_stitching_that_stopped_responding_is_still_failed(db):
    old = datetime.utcnow() - timedelta(hours=2)
    job = await db.pending_videos.insert_one({
        "status": stitch_jobs.STATUS_PROCESSING, "created_at": old, "updated_at": old,
        "clips": [{"stream_uid": "b" * 32, "ready": True}],
    })

    await stitch_jobs.fail_stale_jobs(db)

    doc = await db.pending_videos.find_one({"_id": job.inserted_id})
    assert doc["status"] == stitch_jobs.STATUS_FAILED


async def test_a_job_abandoned_for_days_is_eventually_failed(db):
    old = datetime.utcnow() - timedelta(days=3)
    job = await db.pending_videos.insert_one({
        "status": stitch_jobs.STATUS_WAITING, "created_at": old, "updated_at": old,
        "clips": [{"stream_uid": "c" * 32, "ready": False}],
    })

    await stitch_jobs.fail_stale_jobs(db)

    doc = await db.pending_videos.find_one({"_id": job.inserted_id})
    assert doc["status"] == stitch_jobs.STATUS_FAILED


# --- Searching a followers list ----------------------------------------
# The list is capped at 100 per request, so filtering in the app would only
# ever see the first page.

async def _with_followers(db, names):
    owner, headers = await make_user(db)
    ids = []
    for username, display in names:
        uid, _ = await make_user(db, username=username, display_name=display)
        ids.append(ObjectId(uid))
    await db.users.update_one({"_id": ObjectId(owner)}, {"$set": {"followers": ids}})
    return owner, headers


async def test_followers_can_be_searched_by_username(client, db):
    owner, headers = await _with_followers(
        db, [("amina", "Amina K"), ("bolade", "Bolade"), ("chidi", "Chidi")])

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "q": "bola"})
    assert r.status_code == 200, r.text
    assert [u["username"] for u in r.json()] == ["bolade"]


async def test_followers_search_matches_the_display_name_too(client, db):
    owner, headers = await _with_followers(
        db, [("user1", "Amina Kone"), ("user2", "Bolade")])

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "q": "kone"})
    assert [u["username"] for u in r.json()] == ["user1"]


async def test_followers_search_ignores_capitals(client, db):
    owner, headers = await _with_followers(db, [("zainab", "Zainab")])

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "q": "ZAIN"})
    assert len(r.json()) == 1


async def test_a_regex_in_the_search_term_is_not_run_as_one(client, db):
    owner, headers = await _with_followers(db, [("amina", "Amina")])

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "q": ".*"})
    assert r.status_code == 200, r.text
    # Taken literally, ".*" matches nobody; run as a pattern it matches all.
    assert r.json() == []


async def test_searching_followers_reaches_past_the_first_page(client, db):
    # 120 followers, so the match sits beyond any single page.
    names = [(f"user{i:03d}", f"Person {i}") for i in range(120)]
    names[117] = ("needle", "Needle")
    owner, headers = await _with_followers(db, names)

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "q": "needle"})
    assert [u["username"] for u in r.json()] == ["needle"]


async def test_an_unsearched_page_keeps_follow_order(client, db):
    names = [("first", "First"), ("second", "Second"), ("third", "Third")]
    owner, headers = await _with_followers(db, names)

    r = await client.get(f"/api/v1/users/{owner}/relationships",
                         headers=headers,
                         params={"relationship_type": "followers", "limit": 3})
    assert [u["username"] for u in r.json()] == ["first", "second", "third"]


async def test_search_results_are_paged(client, db):
    names = [(f"match{i:02d}", f"Match {i}") for i in range(10)]
    owner, headers = await _with_followers(db, names)

    first = await client.get(f"/api/v1/users/{owner}/relationships",
                             headers=headers,
                             params={"relationship_type": "followers",
                                     "q": "match", "limit": 4})
    second = await client.get(f"/api/v1/users/{owner}/relationships",
                              headers=headers,
                              params={"relationship_type": "followers",
                                      "q": "match", "limit": 4, "skip": 4})
    assert len(first.json()) == 4 and len(second.json()) == 4
    assert not {u["_id"] for u in first.json()} & {u["_id"] for u in second.json()}
