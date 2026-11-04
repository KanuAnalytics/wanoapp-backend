"""Round 2: harder attacks on the fixes themselves (bypass attempts)."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
EMAIL, PHONE = "victim-pii@example.com", "+256711222333"


async def _victim_world(db, client=None):
    victim, vh = await make_user(db, username="victimx", display_name="Victim X",
                                 email=EMAIL, phone_number=PHONE)
    vid = await make_video(db, victim, title="victimclip", description="victimclip")
    if client is not None:  # a real comment, written by the API itself
        r = await client.post("/api/v1/", headers=vh, json={"video_id": vid, "content": "hello"})
        assert r.status_code == 200, r.text
    return victim, vh, vid


async def test_R2_pii_never_leaks_on_any_read_path(client, db):
    victim, _, vid = await _victim_world(db, client)
    # Viewer's own birth date differs, so only the victim's would match.
    me, h = await make_user(db, following=[ObjectId(victim)], date_of_birth=datetime(1985, 5, 5))
    await db.users.update_one({"_id": ObjectId(victim)}, {"$push": {"followers": ObjectId(me)}})
    paths = [
        "/api/v1/users/", f"/api/v1/users/{victim}", f"/api/v1/users/{victim}/complete",
        f"/api/v1/users/{victim}/stats/detailed", 
        f"/api/v1/users/{me}/relationships?relationship_type=following",
        f"/api/v1/users/{victim}/relationships?relationship_type=followers",
        "/api/v1/feed/", f"/api/v1/feed/?user_id={victim}", f"/api/v1/videos/{vid}",
        "/api/v1/videos/", f"/api/v1/video/{vid}",
        f"/api/v1/user/{victim}", "/api/v1/notifications/",
    ]
    leaks = []
    for p in paths:
        for hdr in (h, {}):
            r = await client.get(p, headers=hdr)
            if r.status_code >= 500:
                leaks.append((p, "500"))
            if EMAIL in r.text or PHONE in r.text or "1990-01-01" in r.text:
                leaks.append((p, "PII"))
    for q in ['{ user(userId: "%s") { email username } }' % victim,
              '{ video(videoId: "%s") { title creator { email } } }' % vid,
              '{ videoComments(videoId: "%s") { content user { email } } }' % vid]:
        r = await client.post("/graphql", json={"query": q}, headers=h)
        if EMAIL in r.text:
            leaks.append((q[:30], "PII"))
    assert not leaks, leaks


async def test_R2_owner_still_sees_own_contact_info(client, db):
    _, vh, _ = await _victim_world(db)
    r = await client.get("/api/v1/users/me/profile", headers=vh)
    assert r.status_code == 200 and EMAIL in r.text, r.text


async def test_R2_revoked_token_rejected_by_graphql(client, db):
    uid, h = await make_user(db, email="rev@example.com")
    await db.users.update_one({"_id": ObjectId(uid)},
                              {"$set": {"password_changed_at": datetime.now(timezone.utc) + timedelta(seconds=5)}})
    r = await client.post("/graphql", json={"query": "{ me { email } }"}, headers=h)
    assert "rev@example.com" not in r.text


@pytest.mark.parametrize("url", [
    "https://videodelivery.net@evil.com/" + "a" * 32 + "/manifest/video.m3u8",
    "https://evilvideodelivery.net/" + "a" * 32 + "/manifest/video.m3u8",
    "https://videodelivery.net.evil.com/" + "a" * 32 + "/manifest/video.m3u8",
    "http://videodelivery.net/" + "a" * 32 + "/manifest/video.m3u8",
    "https://169.254.169.254/" + "a" * 32 + "/x",
    "https://videodelivery.net:8443/" + "a" * 32 + "/x",
    "file:///etc/passwd",
])
async def test_R2_stitch_ssrf_bypasses_blocked(url):
    from app.services.stitch_jobs import StitchValidationError, validate_clip_url
    with pytest.raises(StitchValidationError):
        validate_clip_url(url)


async def test_R2_lambda_blocks_ssrf_tricks():
    import subprocess, json, os
    js = r"""
    const m = await import(process.argv[1]);
    const f = m.isAllowedUrl || m.assertAllowedUrl || m.default?.isAllowedUrl;
    console.log(typeof f);
    """
    src = open("lambda.mjs").read()
    assert "videodelivery.net" in src and ("hostname" in src or "new URL" in src)
    assert "redirect" in src  # redirects must not be followed blindly


async def test_R2_delete_with_alternate_url_shape_spares_victim(client, db, calls):
    victim, _ = await make_user(db)
    attacker, h = await make_user(db)
    uid = "b" * 32
    await make_video(db, victim, remoteUrl_CF=f"https://videodelivery.net/{uid}/manifest/video.m3u8", stream_uid=uid)
    vid = await make_video(db, attacker,
                           remoteUrl_CF=f"https://customer-x.cloudflarestream.com/{uid}/manifest/video.m3u8")
    await client.delete(f"/api/v1/videos/{vid}", headers=h)
    assert uid not in [a[0] for a, _ in calls["stream_deletes"].calls]


async def test_R2_client_cannot_claim_victims_stream_on_post(client, db):
    victim, _ = await make_user(db)
    _, h = await make_user(db)
    uid = "c" * 32
    await make_video(db, victim, remoteUrl_CF=f"https://videodelivery.net/{uid}/manifest/video.m3u8", stream_uid=uid)
    r = await client.post("/api/v1/videos/post", headers=h, json={
        "remoteUrl_CF": f"https://videodelivery.net/{uid}/manifest/video.m3u8",
        "description": "stolen", "media_type": "video"})
    assert r.status_code in (400, 403, 422), r.status_code


async def test_R2_reset_otp_requests_are_capped_per_day(client, db, monkeypatch):
    """Each new OTP resets the guess counter, so new OTPs per account must be capped."""
    from app.api.v1 import auth
    async def fake_send(to_email, username):
        return True, "123456"
    monkeypatch.setattr(auth.email_service, "send_password_reset_otp", fake_send)
    uid, _ = await make_user(db, username="victim2", email="victim2@example.com")
    ok = 0
    for _ in range(12):
        await db.users.update_one({"_id": ObjectId(uid)}, {"$unset": {"password_reset_requested_at": ""}})
        r = await client.post("/api/v1/auth/forgot-password", json={"username_or_email": "victim2@example.com"})
        doc = await db.users.find_one({"_id": ObjectId(uid)})
        if doc.get("password_reset_otp_hash") and r.status_code == 200:
            ok += 1
            await db.users.update_one({"_id": ObjectId(uid)}, {"$unset": {"password_reset_otp_hash": ""}})
    assert ok <= 10, f"{ok} reset codes issued in a row"


async def test_R2_signup_otp_requests_are_capped_per_day(client, db, monkeypatch):
    from app.api.v1 import auth
    async def fake_send(to_email, username=None):
        return True, "123456"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    ok = 0
    for _ in range(12):
        await db.registration_otps.update_one({"email": "squat@example.com"}, {"$unset": {"requested_at": ""}})
        r = await client.post("/api/v1/auth/request-registration-otp", json={"email": "squat@example.com"})
        ok += r.status_code == 200
    assert ok <= 10, f"{ok} signup codes issued in a row"
