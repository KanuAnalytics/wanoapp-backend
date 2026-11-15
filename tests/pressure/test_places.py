"""Places ("places" switch): search towns and neighbourhoods, pick your area,
flyers aimed at a circle. The old city list and Home Ground stay as they are;
counts never single anyone out. Attacks first."""

import json
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import flyers as flyers_svc, places
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

S = "/api/v1/places"
IMG = "https://wano.nyc3.digitaloceanspaces.com/flyers/abc-w720.webp"
ROWS = [
    ["o1", "Kampala", "city", "ug", 0.3177, 32.5814, "kampala", 1516210],
    ["o2", "Ntinda", "area", "ug", 0.3544, 32.6136, "kampala", 0],
    ["o3", "Kololo", "area", "ug", 0.3341, 32.5933, "kampala", 0],
    ["o4", "Upper Kololo", "area", "ug", 0.3292, 32.5910, "kampala", 0],
    ["o5", "Lagos", "city", "ng", 6.4550, 3.3941, "lagos", 15388000],
    ["o6", "Yaba", "area", "ng", 6.5095, 3.3711, "lagos", 0],
    ["o7", "Ntinda Road", "area", "ke", -1.2833, 36.8167, "nairobi", 0],
    ["o8", "Gulu", "city", "ug", 2.7702, 32.3044, "gulu", 152276],
]


@pytest.fixture(autouse=True)
def _places():
    places.load(ROWS)
    places.clear_cache()
    flyers_svc._cache.update(at=0.0, db=None, rows=[])
    yield
    places._loaded[0] = False
    places.clear_cache()
    flyers_svc._cache.update(at=0.0, db=None, rows=[])


async def _on(db, **more):
    await db.config.insert_one({"features": {"places": True, **more}})


async def _person(db, country="ug", area=None, dob=datetime(1995, 5, 1), **kw):
    extra = {"localization": {"country": country}, "date_of_birth": dob}
    if area:
        extra["area"] = {"id": area, "at": datetime.utcnow()}
    return await make_user(db, **extra, **kw)


# ---------------------------------------------------------------- switch, access

async def test_PL1_off_closed_and_listed_off(client, db):
    _, h = await _person(db)
    for r in (await client.get(f"{S}/search?q=ntin", headers=h), await client.get(f"{S}/me", headers=h),
              await client.put(f"{S}/me", headers=h, json={"place_id": "o2"})):
        assert r.status_code == 404
    assert (await client.get("/api/v1/config/features")).json()["places"] is False


async def test_PL2_signed_in_only(client, db):
    await _on(db)
    assert (await client.get(f"{S}/search?q=ntin")).status_code == 401
    assert (await client.put(f"{S}/me", json={"place_id": "o2"})).status_code == 401


# ---------------------------------------------------------------- search

async def test_PL3_finds_a_neighbourhood_with_its_city_and_no_coordinates(client, db):
    await _on(db)
    _, h = await _person(db)
    r = await client.get(f"{S}/search?q=ntin", headers=h)
    items = r.json()["items"]
    assert items[0] == {"id": "o2", "name": "Ntinda", "kind": "area", "country": "ug", "in": "Kampala",
                        "label": "Ntinda, Kampala, Uganda"}
    assert items[1]["id"] == "o7"                       # Kenya's after your own country
    assert "lat" not in str(items) and "32.61" not in r.text


async def test_PL4_words_in_any_order_accents_and_nicknames(client, db):
    await _on(db)
    _, h = await _person(db)
    got = lambda r: [i["id"] for i in r.json()["items"]]
    assert got(await client.get(f"{S}/search?q=kololo up", headers=h)) == ["o4"]
    assert got(await client.get(f"{S}/search?q=KOLOLO", headers=h))[:2] == ["o3", "o4"]
    assert got(await client.get(f"{S}/search?q=kla", headers=h))[0] == "o1"
    for q in ("", "n", "%", "zz", "' or 1=1"):
        r = await client.get(f"{S}/search", params={"q": q}, headers=h)
        assert r.status_code == 200 and r.json()["items"] == [], q
    assert (await client.get(f"{S}/search", params={"q": "x" * 61}, headers=h)).status_code == 422


async def test_PL5_at_most_twenty(client, db):
    await _on(db)
    places.load(ROWS + [[f"z{i}", f"Kamwokya {i}x", "area", "ug", 0.34, 32.59, "kampala", 0] for i in range(40)])
    _, h = await _person(db)
    assert len((await client.get(f"{S}/search?q=kamw", headers=h)).json()["items"]) == places.SEARCH_MAX


# ---------------------------------------------------------------- your area

async def test_PL6_pick_change_and_clear_your_area(client, db):
    await _on(db)
    uid, h = await _person(db)
    r = await client.put(f"{S}/me", headers=h, json={"place_id": "o2"})
    assert r.status_code == 200 and r.json()["area"]["label"] == "Ntinda, Kampala, Uganda"
    assert (await client.get(f"{S}/me", headers=h)).json()["area"]["id"] == "o2"
    u = await db.users.find_one({"_id": ObjectId(uid)})
    assert u["area"]["id"] == "o2" and "home_ground" not in u          # Home Ground untouched
    assert (await client.put(f"{S}/me", headers=h, json={"place_id": None})).json() == {"area": None}
    assert (await client.get(f"{S}/me", headers=h)).json() == {"area": None}


async def test_PL7_only_places_from_the_list(client, db):
    await _on(db)
    _, h = await _person(db)
    for bad in ("nowhere", "o2 ", {"$ne": 1}, 5, "kampala"):
        r = await client.put(f"{S}/me", headers=h, json={"place_id": bad})
        assert r.status_code == 422, bad
    assert (await client.put(f"{S}/me", headers=h, json={"place_id": "o2", "lat": 1})).status_code == 422


async def test_PL8_a_few_changes_a_day(client, db):
    await _on(db)
    _, h = await _person(db)
    for i in range(places.AREA_CHANGES_PER_DAY):
        assert (await client.put(f"{S}/me", headers=h, json={"place_id": "o2" if i % 2 else "o3"})).status_code == 200
    assert (await client.put(f"{S}/me", headers=h, json={"place_id": "o2"})).status_code == 429


async def test_PL9_old_city_list_unchanged(client, db):
    await db.config.insert_one({"features": {"places": True, "home_ground": True}})
    _, h = await _person(db)
    r = await client.get("/api/v1/home-ground/cities", headers=h)
    ids = [c["id"] for g in r.json() for c in g["cities"]]
    assert "kampala" in ids and "o2" not in ids and len(ids) < 1000


# ---------------------------------------------------------------- admin

async def test_PL10_admin_hide_and_rename(client, db):
    await _on(db)
    _, uh = await _person(db)
    _, ah = await make_user(db, user_type="admin")
    assert (await client.post("/api/v1/admin/places/o2", headers=uh, json={"hidden": True})).status_code == 403
    r = await client.post("/api/v1/admin/places/o3", headers=ah, json={"name": "Kololo Hill"})
    assert r.status_code == 200 and r.json()["name"] == "Kololo Hill" and r.json()["original"] == "Kololo"
    assert (await client.post("/api/v1/admin/places/o2", headers=ah, json={"hidden": True})).json()["hidden"] is True
    pub = [i["id"] for i in (await client.get(f"{S}/search?q=ntin", headers=uh)).json()["items"]]
    assert "o2" not in pub
    adm = (await client.get("/api/v1/admin/places/search?q=ntin", headers=ah)).json()["items"]
    assert any(i["id"] == "o2" and i["hidden"] for i in adm)
    assert (await client.put(f"{S}/me", headers=uh, json={"place_id": "o2"})).status_code == 422
    assert (await client.get(f"{S}/search?q=kololo hill", headers=uh)).json()["items"][0]["id"] == "o3"
    for bad in ({"name": "<b>x</b>"}, {"name": ""}, {"name": "x" * 61}):
        assert (await client.post("/api/v1/admin/places/o3", headers=ah, json=bad)).status_code == 422
    assert (await client.post("/api/v1/admin/places/nope", headers=ah, json={"hidden": True})).status_code == 404
    assert await db.admin_log.count_documents({"action": "place_edited"}) == 2


async def test_PL11_reach_is_rounded_and_adults_only(client, db):
    _, ah = await make_user(db, user_type="admin")
    for _ in range(25):
        await _person(db, area="o2")
    for _ in range(30):
        await _person(db, area="o2", dob=datetime.utcnow() - timedelta(days=365 * 15))   # under 18: not counted
    for _ in range(5):
        await _person(db, area="o8")                                                     # Gulu: far away
    r = await client.get("/api/v1/admin/places/reach", params={"place_id": "o2", "km": 1}, headers=ah)
    assert r.json()["people"] == 20 and r.json()["label"] == "about 20"
    r = await client.get("/api/v1/admin/places/reach", params={"place_id": "o8", "km": 10}, headers=ah)
    assert r.json() == {**r.json(), "people": None, "label": "fewer than 20"}
    assert (await client.get("/api/v1/admin/places/reach", params={"place_id": "o2", "km": 2},
                             headers=ah)).status_code == 422
    assert places.rounded(0)["people"] is None and places.rounded(19)["people"] is None
    assert places.rounded(24)["people"] == 20 and places.rounded(1234)["label"] == "about 1,230"


# ---------------------------------------------------------------- flyers in a circle

def _flyer(**over):
    now = datetime.utcnow()
    b = {"business": "Ntinda Chapati", "line": "", "category": "food", "countries": ["ug"], "cities": [],
         "image": IMG, "button": "whatsapp", "button_value": "+256772123456",
         "starts_at": (now - timedelta(hours=1)).isoformat(), "ends_at": (now + timedelta(days=7)).isoformat()}
    b.update(over)
    return b


async def _seen(client, h):
    flyers_svc._cache["at"] = 0.0
    r = await client.get("/api/v1/flyers", headers=h)
    assert r.status_code == 200, r.text
    return [f["business"] for f in r.json()["flyers"]]


async def test_PL12_circle_checked_when_saved(client, db):
    _, ah = await make_user(db, user_type="admin")
    A = "/api/v1/admin/flyers"
    for circle, why in (({"place_id": "nope", "km": 1}, "centre"), ({"place_id": "o2", "km": 2}, "0.5"),
                        ({"place_id": "o6", "km": 1}, "isn't in"), ({"place_id": "o2", "km": True}, "km")):
        r = await client.post(A, headers=ah, json=_flyer(circle=circle))
        assert r.status_code == 422 and why in r.text, (circle, r.text)
    r = await client.post(A, headers=ah, json=_flyer(circle={"place_id": "o2", "km": 1}))
    assert r.status_code == 200 and r.json()["circle"] == {"place_id": "o2", "km": 1.0,
                                                           "label": "Ntinda, Kampala, Uganda"}


async def test_PL13_only_people_whose_area_is_inside(client, db):
    await db.config.insert_one({"features": {"places": True, "flyers": True}})
    _, ah = await make_user(db, user_type="admin")
    await client.post("/api/v1/admin/flyers", headers=ah, json=_flyer(circle={"place_id": "o2", "km": 3}))
    await client.post("/api/v1/admin/flyers", headers=ah, json=_flyer(business="All Uganda"))
    _, inside = await _person(db, area="o2")
    _, kololo = await _person(db, area="o3")          # about 3.2 km from Ntinda
    _, none = await _person(db)
    _, gulu = await _person(db, area="o8")
    _, child = await _person(db, area="o2", dob=datetime.utcnow() - timedelta(days=365 * 15))
    _, nigeria = await _person(db, country="ng", area="o2")
    assert sorted(await _seen(client, inside)) == ["All Uganda", "Ntinda Chapati"]
    for h in (kololo, none, gulu):
        assert await _seen(client, h) == ["All Uganda"]
    assert await _seen(client, child) == [] and await _seen(client, nigeria) == []


async def test_PL14_wider_circle_reaches_kololo(client, db):
    await db.config.insert_one({"features": {"places": True, "flyers": True}})
    _, ah = await make_user(db, user_type="admin")
    await client.post("/api/v1/admin/flyers", headers=ah, json=_flyer(circle={"place_id": "o2", "km": 10}))
    _, kololo = await _person(db, area="o3")
    assert await _seen(client, kololo) == ["Ntinda Chapati"]


async def test_PL15_events_from_outside_the_circle_are_not_counted(client, db):
    await db.config.insert_one({"features": {"places": True, "flyers": True}})
    _, ah = await make_user(db, user_type="admin")
    fid = (await client.post("/api/v1/admin/flyers", headers=ah,
                             json=_flyer(circle={"place_id": "o2", "km": 1}))).json()["id"]
    _, gulu = await _person(db, area="o8")
    r = await client.post("/api/v1/flyers/event", headers=gulu, json={"flyer_id": fid, "kind": "tap"})
    assert r.json() == {"counted": False}


# ---------------------------------------------------------------- the shipped list

async def test_PL16_shipped_list_is_sound():
    with open(places.DATA_FILE, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    head = json.loads(lines[0])
    assert "OpenStreetMap" in head["source"] and "GeoNames" in head["source"]
    rows = [json.loads(x) for x in lines[1:] if x.strip()]
    assert len(rows) > 5000
    ids = [r[0] for r in rows]
    assert len(ids) == len(set(ids))
    from app.services import home_ground as hg
    for pid, name, kind, cc, lat, lng, city, pop in rows:
        assert kind in places.KINDS and cc in hg.COUNTRIES and -90 <= lat <= 90 and -180 <= lng <= 180
        assert city is None or city in hg.CITY, city
        assert 0 < len(name) <= 60 and "<" not in name
    assert places.load() == len(rows)
    p = next(p for p in places._PLACES.values() if p["name"] == "Ntinda")
    assert p["city"] == "kampala" and p["kind"] == "area"
