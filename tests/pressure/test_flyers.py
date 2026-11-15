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
    assert set(f) == {"id", "business", "line", "image", "image_small", "button", "label"}
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
