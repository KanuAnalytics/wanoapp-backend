"""Admin page (/admin) and its endpoints (/api/v1/admin/*). Attacks written
before the code: only admins get in, switches are written where the app reads
them without breaking the version check, every action is logged."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import get_password_hash
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

A = "/api/v1/admin"


async def _admin(db, **kw):
    return await make_user(db, user_type="admin", **kw)


async def _features(client):
    return (await client.get("/api/v1/config/features")).json()


# ---------------------------------------------------------------- who gets in

async def test_AD1_everything_needs_an_admin(client, db):
    _, uh = await make_user(db)
    vid = str(ObjectId())
    calls = [("get", f"{A}/features", None), ("put", f"{A}/features", {"key": "points", "on": True}),
             ("post", f"{A}/features/all-off", None), ("get", f"{A}/overview", None),
             ("get", f"{A}/creators/lookup?q=someone", None), ("get", f"{A}/challenges", None),
             ("post", f"{A}/challenges/{vid}/end", None), ("get", f"{A}/log", None),
             ("get", f"{A}/payouts?state=approved", None)]
    for method, url, body in calls:
        kw = {"json": body} if body is not None else {}
        assert (await client.request(method.upper(), url, **kw)).status_code == 401, url
        assert (await client.request(method.upper(), url, headers=uh, **kw)).status_code == 403, url


async def test_AD2_admin_login_only_for_admins_and_short_lived(client, db):
    await make_user(db, username="boss", user_type="admin", password_hash=get_password_hash("pw-12345678"))
    await make_user(db, username="normal", password_hash=get_password_hash("pw-12345678"))
    r = await client.post(f"{A}/login", json={"username": "normal", "password": "pw-12345678"})
    assert r.status_code == 401                         # same answer as a wrong password
    assert (await client.post(f"{A}/login", json={"username": "boss", "password": "nope"})).status_code == 401
    r = await client.post(f"{A}/login", json={"username": "boss", "password": "pw-12345678"})
    assert r.status_code == 200
    from jose import jwt
    from app.core.config import settings
    claims = jwt.decode(r.json()["access_token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert claims["exp"] - claims["iat"] <= 12 * 3600
    me = await client.get(f"{A}/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["username"] == "boss"


async def test_AD3_banned_admin_cant_log_in(client, db):
    await make_user(db, username="old", user_type="admin", is_active=False,
                    password_hash=get_password_hash("pw-12345678"))
    assert (await client.post(f"{A}/login", json={"username": "old", "password": "pw-12345678"})).status_code == 401


async def test_AD4_login_is_throttled(client, db):
    await make_user(db, username="boss2", user_type="admin", password_hash=get_password_hash("pw-12345678"))
    codes = [(await client.post(f"{A}/login", json={"username": "boss2", "password": f"x{i}"})).status_code
             for i in range(12)]
    assert 429 in codes
    r = await client.post(f"{A}/login", json={"username": "boss2", "password": "pw-12345678"})
    assert r.status_code == 429                         # still locked, even with the right password


# ---------------------------------------------------------------- switches

async def test_AS1_switch_on_and_off_goes_where_the_app_reads(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "1.7.1", "showUpdateIos": True})
    r = await client.put(f"{A}/features", headers=ah, json={"key": "leaderboard", "on": True})
    assert r.status_code == 200 and r.json()["leaderboard"] is True
    assert (await _features(client))["leaderboard"] is True
    # the version check still reads the same document and still works
    v = await client.get("/api/v1/config/version")
    assert v.status_code == 200 and v.json()["appVersionNumber"] == "1.7.1" and v.json()["showUpdateIos"] is True
    assert await db.config.count_documents({}) == 1
    await client.put(f"{A}/features", headers=ah, json={"key": "leaderboard", "on": False})
    assert (await _features(client))["leaderboard"] is False


async def test_AS2_only_known_switches_and_real_booleans(client, db):
    _, ah = await _admin(db)
    for bad in ({"key": "hack", "on": True}, {"key": "points", "on": "yes"}, {"key": "points", "on": 1},
                {"key": "points"}, {"key": "$where", "on": True}, {"key": "features.points", "on": True}):
        assert (await client.put(f"{A}/features", headers=ah, json=bad)).status_code == 422, bad
    assert not any((await _features(client)).values())


async def test_AS3_garbage_features_field_is_replaced_not_crashed(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "1.7.1", "features": "everything"})
    r = await client.put(f"{A}/features", headers=ah, json={"key": "badges", "on": True})
    assert r.status_code == 200
    f = await _features(client)
    assert f["badges"] is True and sum(f.values()) == 1
    assert (await client.get("/api/v1/config/version")).json()["appVersionNumber"] == "1.7.1"


async def test_AS4_no_config_document_yet(client, db):
    _, ah = await _admin(db)
    assert (await client.put(f"{A}/features", headers=ah, json={"key": "city", "on": True})).status_code == 200
    assert (await _features(client))["city"] is True


async def test_AS5_writes_the_document_the_app_reads_when_there_are_several(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"_id": ObjectId("000000000000000000000001"), "features": {"sounds": True}})
    await db.config.insert_one({"_id": ObjectId("000000000000000000000002"), "appVersionNumber": "1.7.2"})
    await client.put(f"{A}/features", headers=ah, json={"key": "city", "on": True})
    f = await _features(client)
    assert f["city"] is True and f["sounds"] is True     # the other switch didn't get lost
    assert (await client.get("/api/v1/config/version")).json()["appVersionNumber"] == "1.7.2"


async def test_AS6_boost_and_earnings_need_points(client, db):
    _, ah = await _admin(db)
    for k in ("boost", "earnings"):
        assert (await client.put(f"{A}/features", headers=ah, json={"key": k, "on": True})).status_code == 409
    await client.put(f"{A}/features", headers=ah, json={"key": "points", "on": True})
    await client.put(f"{A}/features", headers=ah, json={"key": "boost", "on": True})
    await client.put(f"{A}/features", headers=ah, json={"key": "earnings", "on": True})
    await client.put(f"{A}/features", headers=ah, json={"key": "points", "on": False})   # takes them with it
    f = await _features(client)
    assert not f["points"] and not f["boost"] and not f["earnings"]


async def test_AS7_tv_play_needs_tv_series(client, db):
    _, ah = await _admin(db)
    assert (await client.put(f"{A}/features", headers=ah, json={"key": "tv_play", "on": True})).status_code == 409


async def test_AS8_all_off_kill_switch(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "1.7.1",
                                "features": {"points": True, "boost": True, "city": True, "old_thing": True}})
    r = await client.post(f"{A}/features/all-off", headers=ah)
    assert r.status_code == 200 and not any((await _features(client)).values())
    assert (await client.get("/api/v1/config/version")).json()["appVersionNumber"] == "1.7.1"


async def test_AS9_parallel_toggles_dont_lose_each_other(client, db):
    _, ah = await _admin(db)
    keys = ["points", "replies", "challenges", "sounds", "city", "leaderboard", "badges", "tv_series"]
    await asyncio.gather(*[client.put(f"{A}/features", headers=ah, json={"key": k, "on": True}) for k in keys])
    f = await _features(client)
    assert all(f[k] for k in keys)
    assert await db.config.count_documents({}) == 1


# ---------------------------------------------------------------- the log

async def test_AL1_every_action_is_logged_with_who(client, db):
    aid, ah = await _admin(db, username="boss")
    await client.put(f"{A}/features", headers=ah, json={"key": "city", "on": True})
    await client.put("/api/v1/points/admin/pool/2026-10", headers=ah, json={"revenue_usd": 2000})
    cid, _ = await make_user(db)
    vid = await make_video(db, cid)
    await client.post(f"/api/v1/points/admin/videos/{vid}/not-original", headers=ah)
    log = (await client.get(f"{A}/log", headers=ah)).json()
    kinds = [x["action"] for x in log]
    assert "feature" in kinds and "pool" in kinds and "not_original" in kinds
    assert all(x["admin"]["username"] == "boss" for x in log)
    f = next(x for x in log if x["action"] == "feature")
    assert f["detail"] == {"key": "city", "on": True}


async def test_AL2_failed_actions_arent_logged_as_done(client, db):
    _, ah = await _admin(db)
    await client.put(f"{A}/features", headers=ah, json={"key": "boost", "on": True})          # 409
    await client.post(f"/api/v1/points/admin/payouts/{ObjectId()}/approve", headers=ah)        # 409
    assert (await client.get(f"{A}/log", headers=ah)).json() == []


# ---------------------------------------------------------------- creator lookup

async def test_AC1_look_up_a_creator_by_username_id_or_link(client, db):
    _, ah = await _admin(db)
    cid, _ = await make_user(db, username="amina", phone_number="+256700000009", followers_count=120)
    for q in ("amina", "@amina", "AMINA", cid):
        r = await client.get(f"{A}/creators/lookup", headers=ah, params={"q": q})
        assert r.status_code == 200, q
        body = r.json()
        assert body["user"]["id"] == cid and body["user"]["username"] == "amina"
        assert {c["key"] for c in body["creator_status"]["checks"]} >= {"followers", "phone", "age"}
        assert "cash_usd" in body["earnings"] and "balance" in body["points"]
    vid = await make_video(db, cid)
    r = await client.get(f"{A}/creators/lookup", headers=ah, params={"q": f"https://wanoafrica.com/v/{vid}"})
    assert r.status_code == 200 and r.json()["user"]["id"] == cid


async def test_AC2_lookup_doesnt_leak_secrets_or_run_regex(client, db):
    _, ah = await _admin(db)
    await make_user(db, username="zed", password_hash=get_password_hash("secret-pw-1"))
    r = await client.get(f"{A}/creators/lookup", headers=ah, params={"q": "zed"})
    assert "password" not in r.text and "secret-pw" not in r.text
    for q in (".*", "z.d", "^", "", "a" * 300):
        assert (await client.get(f"{A}/creators/lookup", headers=ah, params={"q": q})).status_code in (404, 422), q


# ---------------------------------------------------------------- challenges and payouts

async def test_ACH1_challenges_list_and_end_early(client, db):
    _, ah = await _admin(db)
    now = datetime.now(timezone.utc)
    r = await client.post("/api/v1/challenges", headers=ah, json={
        "hashtag": "#danceug", "title": "Dance UG", "starts_at": now.isoformat(),
        "ends_at": (now + timedelta(days=3)).isoformat()})
    assert r.status_code == 201
    cid = r.json()["id"]
    lst = (await client.get(f"{A}/challenges", headers=ah)).json()
    assert lst[0]["id"] == cid and lst[0]["state"] == "live"
    assert (await client.post(f"{A}/challenges/{cid}/end", headers=ah)).status_code == 200
    assert (await client.get(f"{A}/challenges", headers=ah)).json()[0]["state"] == "ended"
    assert (await client.post(f"{A}/challenges/{cid}/end", headers=ah)).status_code == 409   # already ended
    assert (await client.post(f"{A}/challenges/nope/end", headers=ah)).status_code == 400


async def test_AP1_payout_history_by_state(client, db):
    _, ah = await _admin(db)
    uid, _ = await make_user(db)
    for state in ("requested", "approved", "rejected"):
        await db.points_payouts.insert_one({"user_id": ObjectId(uid), "cents": 700, "state": state,
                                            "at": datetime.utcnow(), "payout_account": {"method": "pix"}})
    for state in ("requested", "approved", "rejected"):
        rows = (await client.get(f"{A}/payouts", headers=ah, params={"state": state})).json()
        assert len(rows) == 1 and rows[0]["state"] == state and rows[0]["usd"] == 7
    assert (await client.get(f"{A}/payouts", headers=ah, params={"state": "$ne"})).status_code == 422


async def test_AO1_overview_counts(client, db):
    _, ah = await _admin(db)
    uid, _ = await make_user(db)
    await db.points_payouts.insert_one({"user_id": ObjectId(uid), "cents": 900, "state": "requested",
                                        "at": datetime.utcnow()})
    o = (await client.get(f"{A}/overview", headers=ah)).json()
    assert o["payouts_waiting"] == 1 and o["payouts_waiting_usd"] == 9
    assert "pool" in o and o["pool"]["pool_usd"] >= 200 and "moderation_waiting" in o
    assert set(o["features"]) >= {"points", "tv_play"}


# ---------------------------------------------------------------- the page itself

async def test_AW1_page_is_served_locked_down(client, db):
    r = await client.get("/admin")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "no-store" in r.headers["cache-control"]
    assert "noindex" in r.headers.get("x-robots-tag", "")
    assert "<script" in r.text and "access_token" not in r.text
    js = await client.get("/admin/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "innerHTML" not in js.text                   # everything is put on the page as text
    assert "localStorage" not in js.text                # the session ends when the tab closes


async def test_AW2_page_and_script_have_nothing_secret(client, db):
    for path in ("/admin", "/admin/app.js", "/admin/logo.png"):
        r = await client.get(path)
        assert r.status_code == 200, path
        if path != "/admin/logo.png":
            for secret in ("SECRET_KEY", "mongodb", "Bearer ey", "password_hash", "api_key"):
                assert secret not in r.text, (path, secret)
    assert (await client.get("/admin/../app/core/config.py")).status_code == 404
    assert (await client.get("/admin/%2e%2e/main.py")).status_code == 404


# ---------------------------------------------------------------- round 2

async def test_R1_demoted_admin_is_locked_out_at_once(client, db):
    aid, ah = await _admin(db)
    assert (await client.get(f"{A}/features", headers=ah)).status_code == 200
    await db.users.update_one({"_id": ObjectId(aid)}, {"$set": {"user_type": "standard"}})
    assert (await client.get(f"{A}/features", headers=ah)).status_code == 403
    await db.users.update_one({"_id": ObjectId(aid)}, {"$set": {"user_type": "admin", "is_active": False}})
    assert (await client.get(f"{A}/features", headers=ah)).status_code in (401, 403)


async def test_R2_password_change_ends_admin_sessions(client, db):
    await make_user(db, username="boss3", user_type="admin", password_hash=get_password_hash("pw-12345678"))
    tok = (await client.post(f"{A}/login", json={"username": "boss3", "password": "pw-12345678"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get(f"{A}/me", headers=h)).status_code == 200
    await db.users.update_one({"username": "boss3"},
                              {"$set": {"password_changed_at": datetime.utcnow() + timedelta(seconds=5)}})
    assert (await client.get(f"{A}/me", headers=h)).status_code == 401


async def test_R3_failed_admin_logins_dont_lock_the_app_login(client, db):
    await make_user(db, username="boss4", user_type="admin", password_hash=get_password_hash("pw-12345678"))
    for i in range(11):
        await client.post(f"{A}/login", json={"username": "boss4", "password": f"bad{i}"})
    r = await client.post("/api/v1/auth/login/json", json={"username": "boss4", "password": "pw-12345678"})
    assert r.status_code == 200


async def test_R4_undo_not_original_clears_the_strike(client, db):
    from app.services import points
    _, ah = await _admin(db)
    cid, _ = await make_user(db)
    vid = await make_video(db, cid)
    assert (await client.post(f"/api/v1/points/admin/videos/{vid}/original-again", headers=ah)).status_code == 409
    await client.post(f"/api/v1/points/admin/videos/{vid}/not-original", headers=ah)
    st = await points.creator_status(db, ObjectId(cid), datetime.now(timezone.utc))
    assert next(c for c in st["checks"] if c["key"] == "no_copied_posts")["ok"] is False
    assert (await client.post(f"/api/v1/points/admin/videos/{vid}/original-again", headers=ah)).status_code == 200
    st = await points.creator_status(db, ObjectId(cid), datetime.now(timezone.utc))
    assert next(c for c in st["checks"] if c["key"] == "no_copied_posts")["ok"] is True
    assert (await client.post(f"/api/v1/points/admin/videos/{vid}/original-again", headers=ah)).status_code == 409
    _, uh = await make_user(db)
    assert (await client.post(f"/api/v1/points/admin/videos/{vid}/original-again", headers=uh)).status_code == 403
    assert [x["action"] for x in (await client.get(f"{A}/log", headers=ah)).json()] == ["original_again", "not_original"]


async def test_R5_ending_an_upcoming_challenge_really_ends_it(client, db):
    _, ah = await _admin(db)
    now = datetime.now(timezone.utc)
    r = await client.post("/api/v1/challenges", headers=ah, json={
        "hashtag": "later", "title": "Later", "starts_at": (now + timedelta(days=2)).isoformat(),
        "ends_at": (now + timedelta(days=4)).isoformat()})
    cid = r.json()["id"]
    assert (await client.get(f"{A}/challenges", headers=ah)).json()[0]["state"] == "upcoming"
    assert (await client.post(f"{A}/challenges/{cid}/end", headers=ah)).status_code == 200
    assert (await client.get(f"{A}/challenges", headers=ah)).json()[0]["state"] == "ended"
    pub = (await client.get("/api/v1/challenges", params={"authenticated": "false"})).json()
    assert all(c["state"] == "ended" for c in pub if c["id"] == cid)


async def test_R6_lookup_dots_are_literal(client, db):
    _, ah = await _admin(db)
    await make_user(db, username="axb")
    await make_user(db, username="a.b")
    r = await client.get(f"{A}/creators/lookup", headers=ah, params={"q": "a.b"})
    assert r.json()["user"]["username"] == "a.b"
    await db.users.delete_one({"username": "a.b"})
    assert (await client.get(f"{A}/creators/lookup", headers=ah, params={"q": "a.b"})).status_code == 404


async def test_R7_extra_fields_on_a_switch_are_refused(client, db):
    _, ah = await _admin(db)
    r = await client.put(f"{A}/features", headers=ah, json={"key": "city", "on": True, "features": {"points": True}})
    assert r.status_code == 422
    assert not any((await _features(client)).values())


async def test_R8_failed_moderation_isnt_logged(client, db):
    _, ah = await _admin(db)
    r = await client.post(f"/api/v1/reports/admin/videos/{ObjectId()}/decision", headers=ah, json={"action": "remove"})
    assert r.status_code == 404
    assert (await client.get(f"{A}/log", headers=ah)).json() == []


async def test_R9_admin_pool_preview_pays_what_distribute_pays(client, db, monkeypatch):
    """The page shows who would get paid; it must be exactly what sharing out pays."""
    from app.services import points
    from tests.pressure.test_points import _month
    state = {"now": datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(points, "_utcnow", lambda: state["now"])
    monkeypatch.setattr(points, "HOLD_DAYS", 0)
    _, ah = await _admin(db)
    good = [(await make_user(db))[0] for _ in range(3)]
    bad, _ = await make_user(db)
    for i, c in enumerate(good + [bad]):
        await _month(db, c, 20, views_per_day=10 + i)

    async def only_good(db_, shares, month, n):
        return {u: p for u, p in shares.items() if str(u) != bad}
    monkeypatch.setattr(points, "_eligible_top", only_good)
    state["now"] = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    prev = (await client.get("/api/v1/points/admin/pool/2026-10", headers=ah)).json()
    assert prev["wano_creators"] == 3 and bad not in [t["user_id"] for t in prev["top"]]
    assert all(t["username"] for t in prev["top"])
    await client.post("/api/v1/points/admin/pool/2026-10/distribute", headers=ah)
    for t in prev["top"]:
        w = await db.earnings_wallet.find_one({"_id": ObjectId(t["user_id"])})
        assert w["cents"] == round(t["usd"] * 100)
    # 3 creators and a 25% cap: $150 paid, $50 stays with Wano, and the page says so
    assert sum(t["usd"] for t in prev["top"]) == 150 and prev["kept_usd"] == 50
