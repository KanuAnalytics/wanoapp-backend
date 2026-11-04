"""Attack tests, one per review finding (ID in the test name).
Before the fixes these fail where the bug exists; after, they must all pass."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from jose import jwt

from app.core.security import create_access_token
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"


# ---------------------------------------------------------------- Critical

async def test_C1_anonymous_cannot_list_users(client, db):
    await make_user(db, email="victim@example.com", phone_number="+256700000001")
    r = await client.get("/api/v1/users/")
    assert r.status_code == 401, r.text


async def test_C1_user_list_hides_contact_fields(client, db):
    await make_user(db, email="victim@example.com", phone_number="+256700000001")
    _, h = await make_user(db)
    r = await client.get("/api/v1/users/", headers=h)
    assert r.status_code == 200, r.text
    body = r.text
    assert "victim@example.com" not in body and "+256700000001" not in body
    assert "date_of_birth" not in body or '"date_of_birth":null' in body.replace(" ", "")


async def test_C1_user_list_limit_is_capped(client, db):
    _, h = await make_user(db)
    r = await client.get("/api/v1/users/?limit=1000000", headers=h)
    assert r.status_code == 422


async def test_C1_single_user_hides_contact_fields(client, db):
    vid, _ = await make_user(db, email="victim@example.com", phone_number="+256700000001")
    r = await client.get(f"/api/v1/users/{vid}")
    assert "victim@example.com" not in r.text and "+256700000001" not in r.text


async def test_C1_graphql_users_hides_emails(client, db):
    await make_user(db, email="victim@example.com")
    r = await client.post("/graphql", json={"query": "{ users { username email } }"})
    assert "victim@example.com" not in r.text


async def test_C2_reset_otp_locks_after_failed_guesses(client, db, monkeypatch):
    from app.api.v1 import auth
    async def fake_send(to_email, username):
        return True, "123456"
    monkeypatch.setattr(auth.email_service, "send_password_reset_otp", fake_send)
    await make_user(db, username="victim", email="victim@example.com")
    r = await client.post("/api/v1/auth/forgot-password", json={"username_or_email": "victim@example.com"})
    assert r.status_code == 200, r.text
    for guess in ("000001", "000002", "000003", "000004", "000005"):
        await client.post("/api/v1/auth/reset-password", json={
            "username_or_email": "victim@example.com", "otp": guess, "new_password": "Hacked123"})
    r = await client.post("/api/v1/auth/reset-password", json={
        "username_or_email": "victim@example.com", "otp": "123456", "new_password": "Hacked123"})
    assert r.status_code in (400, 429), "correct OTP still accepted after 5 wrong guesses"


async def test_C2_registration_otp_locks_after_failed_guesses(client, db, monkeypatch):
    from app.api.v1 import auth
    async def fake_send(to_email, username=None):
        return True, "654321"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    r = await client.post("/api/v1/auth/request-registration-otp", json={"email": "new@example.com"})
    assert r.status_code == 200, r.text
    for guess in ("000001", "000002", "000003", "000004", "000005"):
        await client.post("/api/v1/auth/verify-registration-otp", json={"email": "new@example.com", "otp": guess})
    r = await client.post("/api/v1/auth/verify-registration-otp", json={"email": "new@example.com", "otp": "654321"})
    assert r.status_code in (400, 429)


@pytest.mark.parametrize("path", [
    "/api/v1/notifications/broadcast",
    "/api/v1/recombee/test-first-item",
    "/api/v1/recombee/ingest-videos",
    "/api/v1/recombee/backfill-hashtags",
])
async def test_C3_C4_admin_endpoints_reject_anonymous_and_standard(client, db, path):
    r = await client.post(path)
    assert r.status_code == 401, (path, r.status_code)
    _, h = await make_user(db)
    r = await client.post(path, headers=h)
    assert r.status_code == 403, (path, r.status_code)


@pytest.mark.parametrize("path,kwargs", [
    ("/video/upload", {"files": {"video": ("a.mp4", b"x", "video/mp4")}}),
    ("/video/presigned-upload", {"json": {"filename": "a.mp4", "fileSize": 10}}),
    ("/video/v2/presigned-upload", {"json": {"filename": "a.mp4", "fileSize": 10}}),
    ("/video/check-stream-status", {"json": {"videoId": "x", "uId": "y"}}),
])
async def test_C5_upload_routes_require_login(client, db, path, kwargs):
    r = await client.post(path, **kwargs)
    assert r.status_code == 401, (path, r.status_code, r.text[:200])


async def test_C5_svg_upload_rejected(client, db, monkeypatch):
    import app.routes.upload_video as uv
    async def fake_upload(*a, **k):
        return "https://cdn/x.svg"
    monkeypatch.setattr(uv, "upload_to_spaces", fake_upload, raising=False)
    _, h = await make_user(db)
    r = await client.post("/video/upload", headers=h,
                          files={"video": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
    assert r.status_code == 400, r.status_code


async def test_C6_deleting_post_does_not_delete_another_users_stream(client, db, calls):
    victim, _ = await make_user(db)
    attacker, h = await make_user(db)
    victim_vid = await make_video(db, victim)
    victim_doc = await db.videos.find_one({"_id": ObjectId(victim_vid)})
    stolen_url = victim_doc["remoteUrl_CF"]
    attacker_vid = await make_video(db, attacker, remoteUrl_CF=stolen_url)
    r = await client.delete(f"/api/v1/videos/{attacker_vid}", headers=h)
    assert r.status_code in (200, 204), r.text
    deleted = [a[0] for a, _ in calls["stream_deletes"].calls]
    victim_uid = stolen_url.split("/")[3]
    assert victim_uid not in deleted, "victim's Cloudflare video was deleted"


async def test_C7_deleting_post_does_not_delete_library_song(client, db, calls):
    song_url = f"{CDN}/songs/hit.mp3"
    await db.songs.insert_one({"title": "hit", "uri": song_url})
    tv_url = f"{CDN}/tv/film.m3u8"
    await db.tv_titles.insert_one({"title": "film", "trailer": tv_url, "hls": tv_url})
    attacker, h = await make_user(db)
    vid = await make_video(db, attacker, media_type="photo", images=[song_url, tv_url],
                           urls={"thumbnail": song_url})
    r = await client.delete(f"/api/v1/videos/{vid}", headers=h)
    assert r.status_code in (200, 204), r.text
    deleted = [a[0] for a, _ in calls["spaces_deletes"].calls]
    assert song_url not in deleted and tv_url not in deleted, deleted


# ---------------------------------------------------------------- High

async def test_H1_tokens_expire():
    tok = create_access_token({"sub": str(ObjectId())})
    claims = jwt.get_unverified_claims(tok)
    assert "exp" in claims and "iat" in claims


async def test_H1_password_change_revokes_old_tokens(client, db):
    uid, h = await make_user(db)
    await db.users.update_one({"_id": ObjectId(uid)},
                              {"$set": {"password_changed_at": datetime.now(timezone.utc) + timedelta(seconds=5)}})
    r = await client.get("/api/v1/users/me/profile", headers=h)
    assert r.status_code == 401, r.status_code


async def test_H2_block_user_is_saved(client, db):
    me, h = await make_user(db)
    other, _ = await make_user(db)
    r = await client.post(f"/api/v1/users/block_user/{other}", headers=h)
    assert r.status_code == 204, r.text
    doc = await db.users.find_one({"_id": ObjectId(me)})
    assert ObjectId(other) in doc.get("blocked_users", [])


async def test_H3_private_video_hidden_from_others(client, db):
    owner, _ = await make_user(db)
    _, h = await make_user(db)
    vid = await make_video(db, owner, privacy="private")
    r = await client.get(f"/api/v1/videos/{vid}", headers=h)
    assert r.status_code == 404, r.status_code
    r = await client.get(f"/api/v1/videos/{vid}?authenticated=false")
    assert r.status_code == 404, r.status_code


async def test_H3_private_video_still_visible_to_owner(client, db):
    owner, h = await make_user(db)
    vid = await make_video(db, owner, privacy="private")
    r = await client.get(f"/api/v1/videos/{vid}", headers=h)
    assert r.status_code == 200, r.text


async def test_H3_video_list_hides_private(client, db):
    owner, _ = await make_user(db)
    await make_video(db, owner, privacy="private", title="secretclip")
    r = await client.get("/api/v1/videos/?privacy=private")
    assert "secretclip" not in r.text


async def test_H3_graphql_video_hides_private(client, db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, privacy="private", title="secretclip")
    r = await client.post("/graphql", json={"query": f'{{ video(videoId: "{vid}") {{ title }} }}'})
    assert "secretclip" not in r.text


async def test_H5_register_requires_verified_otp(client, db):
    r = await client.post("/api/v1/auth/register", json={
        "username": "skipper", "email": "skip@example.com", "password": "Password1",
        "display_name": "Skip", "localization": {"country": "ug"}})
    assert r.status_code in (400, 403), r.status_code


async def test_H5_register_works_after_verified_otp(client, db, monkeypatch):
    from app.api.v1 import auth
    async def fake_send(to_email, username=None):
        return True, "111222"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    await client.post("/api/v1/auth/request-registration-otp", json={"email": "ok@example.com"})
    r = await client.post("/api/v1/auth/verify-registration-otp", json={"email": "ok@example.com", "otp": "111222"})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/auth/register", json={
        "username": "okuser", "email": "ok@example.com", "password": "Password1",
        "display_name": "Ok", "localization": {"country": "ug"}})
    assert r.status_code == 200, r.text
    # The verified OTP is single-use.
    r = await client.post("/api/v1/auth/register", json={
        "username": "okuser2", "email": "ok@example.com", "password": "Password1",
        "display_name": "Ok", "localization": {"country": "ug"}})
    assert r.status_code == 400


async def test_H6_email_index_allows_many_phone_only_users():
    import inspect
    from app.core import database
    src = inspect.getsource(database)
    assert "partialFilterExpression" in src or "sparse=True" in src


async def test_H7_complete_profile_hides_contact_fields(client, db):
    vid, _ = await make_user(db, email="victim@example.com", phone_number="+256700000001")
    _, h = await make_user(db)
    r = await client.get(f"/api/v1/users/{vid}/complete", headers=h)
    assert r.status_code == 200, r.text
    assert "+256700000001" not in r.text and "victim@example.com" not in r.text and "1990-01-01" not in r.text


async def test_H8_graphql_video_query_works(client, db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, title="publicclip")
    r = await client.post("/graphql", json={"query": f'{{ video(videoId: "{vid}") {{ id title }} }}'})
    body = r.json()
    assert not body.get("errors"), body
    assert body["data"]["video"]["title"] == "publicclip"


async def test_H9_graphql_create_comment_works(client, db):
    owner, _ = await make_user(db)
    _, h = await make_user(db)
    vid = await make_video(db, owner)
    q = 'mutation { createComment(input: {videoId: "%s", content: "nice"}) { id content } }' % vid
    r = await client.post("/graphql", json={"query": q}, headers=h)
    body = r.json()
    assert not body.get("errors"), body


async def test_H10_graphql_introspection_off_outside_debug(client, db):
    r = await client.post("/graphql", json={"query": "{ __schema { types { name } } }"})
    assert "errors" in r.json()


async def test_H10_graphql_alias_flood_rejected(client, db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner)
    q = "{ " + " ".join(f'a{i}: video(videoId: "{vid}") {{ id }}' for i in range(200)) + " }"
    r = await client.post("/graphql", json={"query": q})
    from app.services.metrics_service import metrics_buffer
    assert metrics_buffer.views_buffer.get(vid, 0) < 50


async def test_H13_put_me_updates_profile(client, db):
    uid, h = await make_user(db)
    r = await client.put("/api/v1/users/me", headers=h, json={"display_name": "New Name"})
    assert r.status_code == 200, r.text
    doc = await db.users.find_one({"_id": ObjectId(uid)})
    assert doc["display_name"] == "New Name"


# ---------------------------------------------------------------- Medium

async def test_M1_unlike_without_like_does_not_lower_count(client, db):
    owner, _ = await make_user(db)
    _, h = await make_user(db)
    vid = await make_video(db, owner)
    for _ in range(5):
        await client.delete(f"/api/v1/videos/{vid}/like", headers=h)
    from app.services.metrics_service import metrics_buffer
    assert metrics_buffer.likes_buffer.get(vid, 0) >= 0


async def test_M1_double_follow_counts_once(client, db):
    target, _ = await make_user(db)
    _, h = await make_user(db)
    await client.post(f"/api/v1/users/{target}/follow", headers=h)
    await client.post(f"/api/v1/users/{target}/follow", headers=h)
    doc = await db.users.find_one({"_id": ObjectId(target)})
    assert doc["followers_count"] == 1


async def test_M1_unfollow_without_follow_keeps_count(client, db):
    target, _ = await make_user(db)
    _, h = await make_user(db)
    await client.delete(f"/api/v1/users/{target}/follow", headers=h)
    doc = await db.users.find_one({"_id": ObjectId(target)})
    assert doc["followers_count"] == 0


async def test_M1_double_unbookmark_keeps_count(client, db):
    owner, _ = await make_user(db)
    _, h = await make_user(db)
    vid = await make_video(db, owner)
    await client.delete(f"/api/v1/videos/{vid}/bookmark", headers=h)
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert doc.get("bookmarks_count", 0) >= 0


async def test_M3_forgot_password_does_not_enumerate(client, db):
    r = await client.post("/api/v1/auth/forgot-password", json={"username_or_email": "nobody@example.com"})
    assert r.status_code == 200


async def test_M4_username_cannot_look_like_email(client, db):
    r = await client.post("/api/v1/auth/register", json={
        "username": "victim@gmail.com", "email": "x@example.com", "password": "Password1",
        "display_name": "X", "localization": {"country": "ug"}})
    assert r.status_code in (400, 422)


async def test_M5_whatsapp_otp_refuses_registered_number(client, db, monkeypatch):
    from app.api.v1 import auth
    sent = []
    async def fake_send(to, content_variables):
        sent.append(to)
        return {"sid": "x"}
    monkeypatch.setattr(auth.twilio_whatsapp_service, "send_template_message", fake_send)
    await make_user(db, phone_number="+256700000009")
    r = await client.post("/api/v1/auth/request-registration-otp", json={"phone_number": "+256700000009"})
    assert r.status_code == 400 and not sent


async def test_M6_inactive_user_cannot_log_in(client, db):
    from app.core.security import get_password_hash
    await make_user(db, username="banned1", password_hash=get_password_hash("Password1"), is_active=False)
    r = await client.post("/api/v1/auth/login/json", json={"username": "banned1", "password": "Password1"})
    assert r.status_code in (401, 403)


async def test_M7_otp_email_escapes_username(monkeypatch):
    from app.services import email_service as es
    html = es.email_service._registration_otp_html("<a href=//evil>x</a>", "123456") \
        if hasattr(es.email_service, "_registration_otp_html") else None
    if html is None:
        import inspect
        src = inspect.getsource(es)
        assert "escape(" in src
    else:
        assert "<a href=//evil>" not in html


async def test_M8_debug_tasks_not_public(client, db):
    r = await client.get("/debug/tasks")
    assert r.status_code in (401, 403, 404)


async def test_M9_debug_defaults_false():
    from app.core.config import Settings
    assert Settings.model_fields["DEBUG"].default is False


async def test_M10_theme_update_works_for_new_user(client, db):
    _, h = await make_user(db)
    r = await client.patch("/api/v1/users/me/theme?dark_mode=true", headers=h)
    assert r.status_code == 200, r.text


async def test_M15_saved_feed_is_only_your_own(client, db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, title="victimsaved")
    victim, _ = await make_user(db, bookmarked_videos=[ObjectId(vid)])
    _, h = await make_user(db)
    r = await client.get(f"/api/v1/feed/?saved=true&user_id={victim}", headers=h)
    assert "victimsaved" not in r.text


async def test_M20_comment_search_escapes_regex(client, db):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner)
    await db.comments.insert_one({"video_id": ObjectId(vid), "user_id": ObjectId(owner), "content": "abc",
                                  "is_active": True, "created_at": datetime.now(timezone.utc),
                                  "likes_count": 0, "replies_count": 0, "liked_by": []})
    _, h = await make_user(db)
    r = await client.get("/api/v1/search?q=a.c", headers=h)
    assert '"abc"' not in r.text


async def test_M25_metrics_flush_is_admin_only(client, db):
    _, h = await make_user(db)
    r = await client.post("/api/v1/metrics/flush", headers=h)
    assert r.status_code == 403


async def test_M29_graphql_metrics_status_has_no_name_error(client, db):
    _, h = await make_user(db, user_type="admin")
    r = await client.post("/graphql", headers=h, json={"query": "{ metricsStatus { totalVideosInBuffer } }"})
    assert "MetricsStatus" not in r.text


# ---------------------------------------------------------------- Low

@pytest.mark.parametrize("method,path", [
    ("put", "/api/v1/videos/notanid"),
    ("post", "/api/v1/users/notanid/follow"),
    ("post", "/api/v1/videos/notanid/like"),
    ("get", "/api/v1/users/notanid/stats/detailed"),
])
async def test_L1_bad_ids_are_400_not_500(client, db, method, path):
    _, h = await make_user(db)
    kwargs = {"json": {}} if method == "put" else {}
    r = await getattr(client, method)(path, headers=h, **kwargs)
    assert r.status_code != 500, (path, r.status_code)


@pytest.mark.parametrize("q", ["limit=0", "limit=100000", "skip=-1"])
async def test_L2_feed_paging_bounds(client, db, q):
    _, h = await make_user(db)
    r = await client.get(f"/api/v1/feed/?{q}", headers=h)
    assert r.status_code == 422, (q, r.status_code)


async def test_L3_admin_can_view_raw_user(client, db):
    target, _ = await make_user(db)
    _, h = await make_user(db, user_type="admin")
    r = await client.get(f"/api/v1/users/{target}/raw", headers=h)
    assert r.status_code == 200, r.text
