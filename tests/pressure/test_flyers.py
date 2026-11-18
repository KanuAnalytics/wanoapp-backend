"""Local flyers ("flyers" switch). Who sees what is the point: never across
countries, cities only to that city, alcohol only to known adults, nothing to
under-18s or logged-out people. Attacks first."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import flyers as svc
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

F = "/api/v1/flyers"
A = "/api/v1/admin/flyers"
IMG = "https://wano.nyc3.digitaloceanspaces.com/flyers/abc-w720.webp"


@pytest.fixture(autouse=True)
def _fresh():
    svc._cache.update(at=0.0, db=None, rows=[])
    yield
    svc._cache.update(at=0.0, db=None, rows=[])


async def _on(db, on=True):
    await db.config.insert_one({"features": {"flyers": on}})


def _body(**over):
    now = datetime.utcnow()
    b = {"business": "Mama Rose Kitchen", "line": "Lunch from 10k UGX", "category": "food", "countries": ["ug"],
         "cities": [], "image": IMG, "button": "whatsapp", "button_value": "+256 772 123456",
         "starts_at": (now - timedelta(hours=1)).isoformat(), "ends_at": (now + timedelta(days=7)).isoformat()}
    b.update(over)
    return b


async def _admin(db):
    return await make_user(db, user_type="admin")


async def _flyer(client, db, **over):
    _, ah = await _admin(db)
    r = await client.post(A, json=_body(**over), headers=ah)
    assert r.status_code == 200, r.text
    svc._cache["at"] = 0.0
    return r.json()


async def _viewer(db, country="ug", city=None, dob=datetime(1995, 5, 1), **kw):
    extra = {"localization": {"country": country} if country else {}}
    if city:
        extra["home_ground"] = {"city": city}
    if dob is not None:
        extra["date_of_birth"] = dob
    uid, h = await make_user(db, **extra, **kw)
    if dob is None:
        await db.users.update_one({"_id": ObjectId(uid)}, {"$unset": {"date_of_birth": ""}})
    return uid, h


async def _seen(client, h):
    r = await client.get(F, headers=h or {})
    assert r.status_code == 200, r.text
    return [f["business"] for f in r.json()["flyers"]]


# ---------------------------------------------------------------- switch, access

async def test_FY1_off_closed_and_listed_off(client, db):
    _, h = await _viewer(db)
    assert (await client.get(F, headers=h)).status_code == 404
    assert (await client.get("/api/v1/config/features")).json()["flyers"] is False


async def test_FY2_only_admins_make_flyers(client, db):
    _, uh = await make_user(db)
    assert (await client.post(A, json=_body(), headers=uh)).status_code == 403
    assert (await client.post(A, json=_body())).status_code == 401
    assert (await client.get(A, headers=uh)).status_code == 403


async def test_FY3_logged_out_gets_nothing(client, db):
    await _on(db)
    await _flyer(client, db)
    r = await client.get(F, params={"authenticated": "false"})
    assert r.status_code == 200 and r.json()["flyers"] == []


# ---------------------------------------------------------------- targeting

async def test_FY4_never_across_countries(client, db):
    await _on(db)
    await _flyer(client, db, business="Kampala Eats", countries=["ug"])
    await _flyer(client, db, business="Lagos Jollof", countries=["ng"])
    _, ug = await _viewer(db, "ug")
    _, ng = await _viewer(db, "ng")
    _, ke = await _viewer(db, "ke")
    _, none = await _viewer(db, None)
    assert await _seen(client, ug) == ["Kampala Eats"]
    assert await _seen(client, ng) == ["Lagos Jollof"]
    assert await _seen(client, ke) == []
    assert await _seen(client, none) == []


async def test_FY5_city_flyers_only_to_that_city(client, db):
    await _on(db)
    await _flyer(client, db, business="Fort Portal Shop", cities=["fort_portal"])
    await _flyer(client, db, business="All Uganda")
    _, fp = await _viewer(db, "ug", city="fort_portal")
    _, kla = await _viewer(db, "ug", city="kampala")
    _, no_city = await _viewer(db, "ug")
    assert sorted(await _seen(client, fp)) == ["All Uganda", "Fort Portal Shop"]
    assert await _seen(client, kla) == ["All Uganda"]
    assert await _seen(client, no_city) == ["All Uganda"]


async def test_FY6_alcohol_only_to_known_adults(client, db):
    await _on(db)
    await _flyer(client, db, business="Uganda Breweries", category="drinks")
    _, adult = await _viewer(db, dob=datetime(1990, 1, 1))
    _, unknown = await _viewer(db, dob=None)
    _, nonsense = await _viewer(db, dob=datetime(2025, 1, 1))     # the old sign-up's default date
    assert await _seen(client, adult) == ["Uganda Breweries"]
    assert await _seen(client, unknown) == []
    assert await _seen(client, nonsense) == []


async def test_FY7_no_flyers_at_all_for_under_18s(client, db):
    await _on(db)
    await _flyer(client, db)
    _, kid = await _viewer(db, dob=datetime.utcnow() - timedelta(days=365 * 15))
    assert await _seen(client, kid) == []


async def test_FY8_only_while_running_and_switched_on(client, db):
    await _on(db)
    now = datetime.utcnow()
    await _flyer(client, db, business="Later", starts_at=(now + timedelta(days=1)).isoformat(),
                 ends_at=(now + timedelta(days=3)).isoformat())
    await _flyer(client, db, business="Over", starts_at=(now - timedelta(days=3)).isoformat(),
                 ends_at=(now - timedelta(days=1)).isoformat())
    off = await _flyer(client, db, business="Paused")
    _, ah = await _admin(db)
    await client.post(f"{A}/{off['id']}/active", json={"active": False}, headers=ah)
    _, h = await _viewer(db)
    assert await _seen(client, h) == []


async def test_FY9_daily_cap(client, db):
    await _on(db)
    f = await _flyer(client, db, daily_cap=2)
    viewers = [await _viewer(db) for _ in range(3)]
    for uid, h in viewers[:2]:
        assert await _seen(client, h) == ["Mama Rose Kitchen"]
        assert (await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "shown"}, headers=h)).json()["counted"]
    assert await _seen(client, viewers[2][1]) == []


async def test_FY10_at_most_three_at_once_and_safe_fields_only(client, db):
    await _on(db)
    for i in range(5):
        await _flyer(client, db, business=f"Shop {i}")
    _, h = await _viewer(db)
    r = (await client.get(F, headers=h)).json()
    assert len(r["flyers"]) == svc.MAX_PER_VIEWER
    f = r["flyers"][0]
    assert set(f) == {"id", "business", "line", "image", "image_small", "button", "label", "artwork_id"}   # no "test" for normal viewers
    assert f["label"] == "Sponsored"
    assert f["button"] == {"kind": "whatsapp", "label": "WhatsApp",
                           "url": "https://wa.me/256772123456?text=Hi%2C%20I%20saw%20your%20flyer%20on%20Wano"}
    assert "rules" in r and r["rules"]["min_posts"] >= 3


# ---------------------------------------------------------------- admin input

async def test_FY11_banned_categories_and_bad_input_refused(client, db):
    _, ah = await _admin(db)
    for cat in ("betting", "loans", "crypto", "adult", "$ne"):
        assert (await client.post(A, json=_body(category=cat), headers=ah)).status_code == 422, cat
    bad = [{"countries": []}, {"countries": ["xx"]}, {"cities": ["nairobi"]},            # Nairobi isn't in Uganda
           {"cities": ["atlantis"]}, {"image": "http://insecure/x.webp"}, {"image": "javascript:alert(1)"},
           {"button": "call", "button_value": "0772"}, {"button": "link", "button_value": "http://x.com"},
           {"button": "link", "button_value": "javascript:alert(1)"}, {"business": "   "},
           {"ends_at": "2020-01-01T00:00:00"}, {"starts_at": "soon"}, {"daily_cap": 0}, {"daily_cap": "10"},
           {"button": "sms", "button_value": "+256772123456"}]
    for over in bad:
        r = await client.post(A, json=_body(**over), headers=ah)
        assert r.status_code == 422, (over, r.text)
    assert (await client.post(A, json={**_body(), "owner": "x"}, headers=ah)).status_code == 422
    assert await db.flyers.count_documents({}) == 0


async def test_FY12_text_is_cleaned_and_capped(client, db):
    f = await _flyer(client, db, business="Rose‮​Kitchen" + "x" * 100, line="Hi\u0000 there")
    assert "‮" not in f["business"] and len(f["business"]) <= 40 and "\u0000" not in f["line"]


async def test_FY13_edit_and_logged(client, db):
    admin, ah = await _admin(db)
    f = await _flyer(client, db)
    r = await client.put(f"{A}/{f['id']}", json=_body(business="Mama Rose Kitchen (new menu)"), headers=ah)
    assert r.status_code == 200 and r.json()["business"] == "Mama Rose Kitchen (new menu)"
    assert await db.admin_log.count_documents({"action": {"$in": ["flyer_create", "flyer_update"]}}) == 2


# ---------------------------------------------------------------- events and numbers

async def test_FY14_events_counted_per_city_with_reach(client, db):
    await _on(db)
    f = await _flyer(client, db)
    a, ah = await _viewer(db, city="kampala")
    b, bh = await _viewer(db, city="gulu")
    for h in (ah, ah, bh):
        await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "shown"}, headers=h)
    await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "tap"}, headers=ah)
    _, adm = await _admin(db)
    rep = (await client.get(f"{A}/{f['id']}/report", headers=adm)).json()
    places = {p["place"]: p for p in rep["places"]}
    assert places["kampala"]["shown"] == 2 and places["kampala"]["reach"] == 1 and places["kampala"]["tap"] == 1
    assert places["gulu"]["reach"] == 1
    lst = (await client.get(A, headers=adm)).json()["items"][0]["stats"]
    assert lst["shown"] == 3 and lst["reach"] == 2 and lst["tap"] == 1


async def test_FY15_events_cant_be_faked(client, db):
    await _on(db)
    f = await _flyer(client, db, countries=["ug"])
    _, ng = await _viewer(db, "ng")
    r = await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "tap"}, headers=ng)
    assert r.json()["counted"] is False                                  # not theirs to see
    _, ug = await _viewer(db)
    for body in ({"flyer_id": f["id"], "kind": "buy"}, {"flyer_id": {"$ne": 1}, "kind": "tap"},
                 {"flyer_id": f["id"], "kind": "tap", "x": 1}):
        assert (await client.post(f"{F}/event", json=body, headers=ug)).status_code == 422
    assert (await client.post(f"{F}/event", json={"flyer_id": "nope", "kind": "tap"}, headers=ug)).json()["counted"] is False
    assert (await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "tap"})).status_code == 401
    counted = 0
    for _ in range(svc.EVENTS_PER_DAY + 10):
        counted += (await client.post(f"{F}/event", json={"flyer_id": f["id"], "kind": "tap"}, headers=ug)).json()["counted"]
    assert counted == svc.EVENTS_PER_DAY


async def test_FY16_image_upload_remade_as_webp(client, db, monkeypatch):
    import io
    from PIL import Image
    saved = {}
    monkeypatch.setattr(svc, "_put", lambda k, d: saved.__setitem__(k, d))
    _, ah = await _admin(db)
    buf = io.BytesIO()
    Image.new("RGB", (1200, 800), (200, 30, 30)).save(buf, "JPEG")
    r = await client.post(f"{A}/image", files={"file": ("f.jpg", buf.getvalue(), "image/jpeg")}, headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["image"].endswith("-w720.webp") and r.json()["image_small"].endswith("-w320.webp")
    assert all(k.startswith("flyers/") for k in saved) and len(saved) == 2
    bad = await client.post(f"{A}/image", files={"file": ("f.jpg", b"<script>alert(1)</script>", "image/jpeg")}, headers=ah)
    assert bad.status_code == 400
    _, uh = await make_user(db)
    assert (await client.post(f"{A}/image", files={"file": ("f.jpg", buf.getvalue(), "image/jpeg")},
                              headers=uh)).status_code == 403


async def test_FY17_busy_feed_stays_fast(client, db):
    import time
    await _on(db)
    _, ah = await _admin(db)
    now = datetime.utcnow()
    await db.flyers.insert_many([{**svc_doc(i, now)} for i in range(400)])
    _, h = await _viewer(db, "ug", city="kampala")
    t = time.perf_counter()
    for _ in range(20):
        await client.get(F, headers=h)
    assert time.perf_counter() - t < 6


def svc_doc(i, now):
    return {"business": f"B{i}", "line": "", "category": "food", "countries": ["ug" if i % 2 else "ng"],
            "cities": ["kampala"] if i % 3 == 0 else [], "image": IMG, "image_small": None, "button": "call",
            "button_value": "+256772123456", "starts_at": now - timedelta(hours=1), "ends_at": now + timedelta(days=1),
            "daily_cap": None, "active": True}


@pytest.mark.parametrize(("username", "entered"), [
    ("eilmer", "@Eilmer "),
    ("WanoTeam", "WanoTeam"),
    ("WanoTeam", "@wanoteam "),
])
async def test_FY18_testers_see_it_anywhere_first_and_nobody_else_changes(client, db, username, entered):
    await _on(db)
    _, eh = await _viewer(db, "us", username=username)
    await _flyer(client, db, business="Kampala Only", countries=["ug"], cities=["kampala"], testers=[entered])
    await _flyer(client, db, business="Lagos Only", countries=["ng"])
    r = (await client.get(F, headers=eh)).json()["flyers"]
    assert [f["business"] for f in r] == ["Kampala Only"] and r[0]["test"] is True
    _, us = await _viewer(db, "us")
    assert await _seen(client, us) == []
    _, kla = await _viewer(db, "ug", city="kampala")
    out = (await client.get(F, headers=kla)).json()["flyers"]
    assert [f["business"] for f in out] == ["Kampala Only"] and "test" not in out[0]
    # a tester's own views and taps still count, under their own country
    f_id = r[0]["id"]
    assert (await client.post(f"{F}/event", json={"flyer_id": f_id, "kind": "shown"}, headers=eh)).json()["counted"]


async def test_FY19_testers_must_exist_and_are_capped(client, db):
    _, ah = await _admin(db)
    assert (await client.post(A, json=_body(testers=["nobody_here"]), headers=ah)).status_code == 422
    names = []
    for i in range(11):
        await make_user(db, username=f"t{i}")
        names.append(f"t{i}")
    assert (await client.post(A, json=_body(testers=names), headers=ah)).status_code == 422
    assert (await client.post(A, json=_body(testers=[{"$ne": 1}]), headers=ah)).status_code == 422


async def test_FY20_testers_only_reaches_nobody_else(client, db):
    await _on(db)
    _, eh = await _viewer(db, "us", username="eilmer")
    await _flyer(client, db, business="Preview", countries=["ug"], testers=["eilmer"], testers_only=True)
    assert await _seen(client, eh) == ["Preview"]
    _, kla = await _viewer(db, "ug", city="kampala")
    _, ug = await _viewer(db, "ug")
    assert await _seen(client, kla) == [] and await _seen(client, ug) == []
    f = (await client.get(A, headers=(await _admin(db))[1])).json()["items"][0]
    assert f["testers_only"] is True
    # their taps can't be counted either
    fid = f["id"]
    assert (await client.post(f"{F}/event", json={"flyer_id": fid, "kind": "tap"}, headers=ug)).json()["counted"] is False


async def test_FY21_testers_only_needs_a_tester(client, db):
    _, ah = await _admin(db)
    r = await client.post(A, json=_body(testers_only=True), headers=ah)
    assert r.status_code == 422 and "tester" in r.json()["detail"]
    assert (await client.post(A, json=_body(testers_only="yes"), headers=ah)).status_code == 422


async def test_delivery_check_and_separate_previews(client, db):
    await _on(db)
    uid, _ = await _viewer(db, "us", username="WanoTeam")
    _, admin = await _admin(db)
    flyer = await _flyer(client, db, testers=["wanoteam"])
    endpoint = f"{A}/{flyer['id']}/delivery"
    for account in ["@WanoTeam", f"https://devbe.wanoafrica.com/u/{uid}"]:
        r = await client.get(endpoint, params={"account": account}, headers=admin)
        assert r.json()["result"] == "Selected in this check."
    assert await db.flyer_daily.count_documents({}) == 0
    assert await svc.record(db, uid, flyer["id"], "shown")
    report = await svc.report(db, flyer["id"])
    assert report["preview"]["shown"] == 1 and report["days"] == []
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": {"date_of_birth": datetime.utcnow() - timedelta(days=365*15)}})
    assert not await svc.record(db, uid, flyer["id"], "shown")
    r = await client.get(endpoint, params={"account": "@WanoTeam"}, headers=admin)
    assert "minimum age" in r.json()["result"]


async def test_concurrent_cap_and_unique_viewers(client, db):
    await _on(db)
    uid, _ = await _viewer(db)
    flyer = await _flyer(client, db, daily_cap=1)
    fid = flyer["id"]
    now = datetime.utcnow()
    accepted = await asyncio.gather(*(svc.record(db, uid, fid, "shown", now) for _ in range(4)))
    assert sum(accepted) == 1
    assert await svc.record(db, uid, fid, "shown", now + timedelta(days=1))
    assert (await svc.report(db, fid))["unique_viewers_tracked"] == 1
    await db.flyers.update_one({"_id": ObjectId(fid)}, {"$set": {"active": False}})
    assert not await svc.record(db, uid, fid, "tap", now)


async def test_alcohol_preview_requires_adult_and_offsets_are_utc(client, db):
    await _on(db)
    uid, _ = await _viewer(db, "us", dob=None, username="PreviewUser")
    flyer = await _flyer(client, db, category="drinks", testers=["PreviewUser"])
    assert not (await svc.for_viewer(db, uid))["flyers"]
    assert not await svc.record(db, uid, flyer["id"], "shown")
    doc = await svc.validate(db, _body(starts_at="2026-11-17T20:00:00-06:00",
                                        ends_at="2026-11-18T04:00:00Z"))
    assert doc["starts_at"] == datetime(2026, 11, 18, 2)


async def test_display_retry_and_frozen_attribution(client, db):
    await _on(db)
    uid, _ = await _viewer(db, "ng", city="kampala", username="PreviewUser")
    flyer = await _flyer(client, db, countries=["ng"], testers=["PreviewUser"])
    fid = flyer["id"]
    for _ in range(2):
        assert await svc.record(db, uid, fid, "shown", display_id="preview-1")
    await db.flyers.update_one({"_id": ObjectId(fid)}, {"$set": {"tester_ids": []}})
    for _ in range(2):
        assert await svc.record(db, uid, fid, "tap", display_id="preview-1")
    assert not await svc.record(db, uid, fid, "tap", display_id="unknown")
    artwork = (await svc.for_viewer(db, uid))["flyers"][0]["artwork_id"]
    assert await svc.record(db, uid, fid, "shown", display_id="public-1", artwork_id=artwork)
    report = await svc.report(db, fid)
    assert report["last_artwork_impression"] is not None
    await db.flyers.update_one({"_id": ObjectId(fid)}, {"$set": {"image": IMG + "?v=2"}})
    assert (await svc.report(db, fid))["last_artwork_impression"] is None
    assert report["preview"] == {"shown": 1, "tap": 1, "close": 0}
    assert [(p["country"], p["city"], p["shown"]) for p in report["places"]] == [("ng", "kampala", 1)]


async def test_detail_report_includes_every_location_day(client, db):
    fid = (await _flyer(client, db))["id"]
    today = svc._day(datetime.utcnow())
    await db.flyer_daily.insert_many([
        {"flyer_id": ObjectId(fid), "day": today - timedelta(days=i // 200),
         "country": "ug", "city": str(i % 200), "shown": 1}
        for i in range(5200)
    ])
    report = await svc.report(db, fid)
    listed = next(x for x in await svc.admin_list(db) if x["id"] == fid)
    assert sum(d["shown"] for d in report["days"]) == listed["stats"]["shown"] == 5200
    assert sum(p["shown"] for p in report["places"]) == 5200


async def test_real_transaction_rolls_back_and_retry_recovers(client, db, monkeypatch):
    from tests.pressure.conftest import REAL_MONGO
    if not REAL_MONGO:
        pytest.skip("requires MongoDB transactions")
    uid, _ = await _viewer(db)
    fid = (await _flyer(client, db, daily_cap=1))["id"]
    from motor.motor_asyncio import AsyncIOMotorCollection
    original = AsyncIOMotorCollection.update_one

    async def fail_audience(collection, *args, **kwargs):
        if collection.name == "flyer_audience":
            raise RuntimeError("simulated interrupted write")
        return await original(collection, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncIOMotorCollection, "update_one", fail_audience)
        with pytest.raises(RuntimeError, match="interrupted"):
            await svc.record(db, uid, fid, "shown", display_id="retry")
    assert await db.flyer_counts.count_documents({}) == 0
    assert await db.flyer_displays.count_documents({}) == 0
    results = await asyncio.gather(*(svc.record(db, uid, fid, "shown", display_id="retry") for _ in range(10)))
    assert all(results)
    assert (await svc.report(db, fid))["days"][0]["shown"] == 1


async def test_motion_upload_and_legacy_edit(client, db, monkeypatch):
    from pathlib import Path
    import subprocess
    import tempfile
    from PIL import Image
    import io
    await _on(db)
    _, admin = await _admin(db)
    _, viewer = await _viewer(db)
    stored = {}
    monkeypatch.setattr(svc, "_put", lambda key, data: stored.update({key: data}))
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "motion.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=120x40:r=12",
                        "-t", "10", "-pix_fmt", "yuv420p", str(clip)], check=True)
        response = await client.post(A + "/motion", files={"file": ("motion.mp4", clip.read_bytes(), "video/mp4")}, headers=admin)
    assert response.status_code == 200, response.text
    motion = response.json()["motion"]
    animation = Image.open(io.BytesIO(next(iter(stored.values()))))
    assert animation.size == (600, 200) and animation.n_frames == 120
    flyer = await _flyer(client, db, motion=motion)
    assert (await client.get(F, headers=viewer)).json()["flyers"][0]["motion"] == motion
    assert (await client.put(f"{A}/{flyer['id']}", json=_body(), headers=admin)).json()["motion"] == motion
    assert (await client.put(f"{A}/{flyer['id']}", json=_body(motion=None), headers=admin)).json()["motion"] is None
