"""wanoafrica.com public pages: creator pages, trending, cities, mailing list,
enquiries, account-deletion requests, film status. Attacks first."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
P = "/api/v1/public"


@pytest.fixture(autouse=True)
def fresh():
    from app.services import public_site, leaderboard
    public_site.clear_cache()
    leaderboard._cache.clear()
    yield
    public_site.clear_cache()


THUMB = {"urls": {"thumbnail": "https://cdn.example/t.jpg"}}


async def test_PS1_minors_banned_and_deleted_never_get_a_page(client, db):
    await make_user(db, username="kid", date_of_birth=datetime.utcnow() - timedelta(days=365 * 15))
    await make_user(db, username="gone", is_active=False)
    await make_user(db, username="banned", is_banned=True)
    await make_user(db, username="deleted", deleted_at=datetime.utcnow())
    for name in ("kid", "gone", "banned", "deleted", "nobody", "x" * 80, "$where", "../etc"):
        assert (await client.get(f"{P}/creators/{name}")).status_code == 404, name
    assert (await client.get(f"{P}/creators/%7B%22%24ne%22%3A1%7D")).status_code == 404


async def test_PS2_creator_page_shows_public_posts_only(client, db):
    uid, _ = await make_user(db, username="Bag Juice ", display_name="Paul Miller", bio="Blue Mountains",
                             followers_count=1500, profile_picture="https://cdn.example/p.jpg")
    pub = await make_video(db, uid, title="Hello Wano", views_count=40, **THUMB)
    await make_video(db, uid, title="secret", privacy="private", **THUMB)
    await make_video(db, uid, title="held", moderation={"state": "held"}, **THUMB)
    await make_video(db, uid, title="later", status="scheduled", **THUMB)
    for q in ("Bag Juice", "bag juice", "BAG%20JUICE"):
        r = await client.get(f"{P}/creators/{q}")
        assert r.status_code == 200, q
    d = r.json()
    assert d["username"] == "Bag Juice" and d["display_name"] == "Paul Miller" and d["tier"] == "Rising"
    assert d["posts_count"] == 1 and [p["title"] for p in d["posts"]] == ["Hello Wano"]
    assert d["posts"][0]["url"] == f"https://wanoafrica.com/v/{pub}"
    assert d["url"] == "https://wanoafrica.com/@Bag%20Juice"
    assert "email" not in d and "date_of_birth" not in d and "phone_number" not in d


async def test_PS3_sitemap_lists_adult_creators_with_public_posts(client, db):
    a, _ = await make_user(db, username="ama", followers_count=50)
    b, _ = await make_user(db, username="kofi", followers_count=500)
    k, _ = await make_user(db, username="teen", date_of_birth=datetime.utcnow() - timedelta(days=365 * 16))
    await make_user(db, username="lurker")
    for u in (a, b, k):
        await make_video(db, u, **THUMB)
    items = (await client.get(f"{P}/creators")).json()["items"]
    assert [i["username"] for i in items] == ["kofi", "ama"]


async def _scored(db, uid, views):
    from app.services import leaderboard
    start = leaderboard.week_start(leaderboard._utcnow())
    await db.creator_daily.insert_one({"creator_id": ObjectId(uid), "day": start + timedelta(hours=1), "views": views,
                                       "likes": 0, "comments": 0, "shares": 0, "finished": 0})
    await make_video(db, uid, created_at=start + timedelta(hours=2), views_count=views, country="UG", **THUMB)


async def test_PS4_trending_hides_minors_and_filters_country(client, db):
    a, _ = await make_user(db, username="top")
    k, _ = await make_user(db, username="kid", date_of_birth=datetime.utcnow() - timedelta(days=365 * 15))
    g, _ = await make_user(db, username="accra1", localization={"country": "gh"})
    await _scored(db, a, 900)
    await _scored(db, k, 5000)
    await _scored(db, g, 100)
    d = (await client.get(f"{P}/trending")).json()
    assert [c["username"] for c in d["creators"]] == ["top", "accra1"]
    assert all(p["creator"]["username"] != "kid" for p in d["posts"])
    assert [c["username"] for c in (await client.get(f"{P}/trending?country=GH")).json()["creators"]] == ["accra1"]
    assert (await client.get(f"{P}/trending?country=xxx")).status_code == 422


async def test_PS5_city_pages_only_for_site_cities(client, db):
    for c in ("kampala", "lagos", "nairobi", "accra", "johannesburg", "london"):
        r = await client.get(f"{P}/cities/{c}")
        assert r.status_code == 200, c
        assert r.json()["name"]
    assert (await client.get(f"{P}/cities/gulu")).status_code == 404
    assert (await client.get(f"{P}/cities/nowhere")).status_code == 404


async def test_PS6_mailing_list(client, db):
    for bad in ({"email": "nope"}, {"email": ""}, {"email": "a@b.c" + "x" * 300}, {"email": ["a@b.co"]}):
        assert (await client.post(f"{P}/newsletter", json=bad)).status_code == 400, bad
    r = await client.post(f"{P}/newsletter", json={"email": "Ama@Example.com", "interests": ["wano_tv", "evil"], "who": "w"})
    assert r.json() == {"subscribed": True, "new": True}
    assert (await client.post(f"{P}/newsletter", json={"email": "ama@example.com"})).json()["new"] is False
    doc = await db.newsletter.find_one({"_id": "ama@example.com"})
    assert doc["interests"] == ["wano_tv"]
    assert (await client.post(f"{P}/newsletter/unsubscribe", json={"token": "x" * 20})).status_code == 404
    assert (await client.post(f"{P}/newsletter/unsubscribe", json={"token": doc["token"]})).status_code == 200
    assert (await db.newsletter.find_one({"_id": "ama@example.com"}))["unsubscribed_at"]
    for i in range(4):
        await client.post(f"{P}/newsletter", json={"email": f"x{i}@example.com", "who": "w"})
    assert (await client.post(f"{P}/newsletter", json={"email": "y@example.com", "who": "w"})).status_code == 429


async def test_PS7_enquiries_and_deletion_requests_reach_the_admin(client, db):
    base = {"kind": "advertise", "name": "Kato", "email": "kato@shop.ug", "message": "Flyers for my shop", "who": "z"}
    for bad in ({**base, "kind": "spam"}, {**base, "email": "bad"}, {**base, "message": ""}, {**base, "name": "x" * 200}):
        assert (await client.post(f"{P}/enquiries", json=bad)).status_code == 400
    assert (await client.post(f"{P}/enquiries", json={**base, "company": "=HYPERLINK(1)"})).status_code == 200
    r = await client.post(f"{P}/account-deletion", json={"account": "@ama", "email": "ama@example.com", "who": "z"})
    ref = r.json()["ref"]
    assert ref.startswith("DEL-")
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/site/enquiries", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    e = (await client.get("/api/v1/admin/site/enquiries", headers=ha)).json()
    assert e["counts"]["new"] == 1 and e["rows"][0]["company"] == "=HYPERLINK(1)" and "who" not in e["rows"][0]
    d = (await client.get("/api/v1/admin/site/deletion", headers=ha)).json()
    assert d["rows"][0]["ref"] == ref
    assert (await client.post(f"/api/v1/admin/site/deletion/{ref}/status", json={"status": "done"}, headers=ha)).status_code == 200
    assert (await client.post(f"/api/v1/admin/site/deletion/{ref}/status", json={"status": "x"}, headers=ha)).status_code == 400
    await client.post(f"{P}/newsletter", json={"email": "=cmd@evil.com"})
    csv = (await client.get("/api/v1/admin/site/newsletter.csv", headers=ha)).text
    assert "\"'=cmd@evil.com\"" in csv


async def test_PS8_film_status_needs_ref_and_email(client, db):
    from tests.pressure.test_film_submissions import form
    ref = (await client.post("/api/v1/film-submissions", json=form())).json()["ref"]
    ok = await client.post("/api/v1/film-submissions/status", json={"ref": ref.lower(), "email": " AMA@example.com "})
    assert ok.status_code == 200 and ok.json()["label"] == "Received" and ok.json()["film_title"] == "Harmattan"
    assert "email" not in ok.json() and "note" not in ok.json()
    for bad in ({"ref": ref, "email": "other@example.com"}, {"ref": "WTV-000000", "email": "ama@example.com"},
                {"ref": {"$ne": 1}, "email": "ama@example.com"}, {}):
        assert (await client.post("/api/v1/film-submissions/status", json=bad)).status_code == 404
    _, ha = await make_user(db, user_type="admin")
    await client.post(f"/api/v1/admin/film-submissions/{ref}/status", json={"status": "selected", "note": "private"}, headers=ha)
    s = (await client.post("/api/v1/film-submissions/status", json={"ref": ref, "email": "ama@example.com"})).json()
    assert s["label"] == "Selected" and s["step"] == 2 and "private" not in str(s)
    for _ in range(20):
        await client.post("/api/v1/film-submissions/status", json={"ref": "WTV-000001", "email": "a@b.co", "who": "bot"})
    assert (await client.post("/api/v1/film-submissions/status",
                              json={"ref": ref, "email": "ama@example.com", "who": "bot"})).status_code == 429


async def test_PS9_emails_and_phone_numbers_never_shown(client, db):
    e, _ = await make_user(db, username="jka@gmail.com", display_name="jka@gmail.com")
    p, _ = await make_user(db, username="Kigula", display_name="Ivan 0785476538", bio="call me +256 785 476 538")
    ok, _ = await make_user(db, username="ama", display_name="Ama")
    for u in (e, p, ok):
        await _scored(db, u, 100)
    assert (await client.get(f"{P}/creators/jka@gmail.com")).status_code == 404
    k = (await client.get(f"{P}/creators/Kigula")).json()
    assert k["display_name"] == "Kigula" and k["bio"] == ""
    d = (await client.get(f"{P}/trending")).json()
    text = str(d)
    assert "gmail" not in text and "0785476538" not in text
    row = next(c for c in d["creators"] if c["username"] is None)
    assert row["display_name"] == "Wano creator" and row["url"] == f"https://wanoafrica.com/u/{e}"
    assert "jka@gmail.com" not in str((await client.get(f"{P}/creators")).json())


async def test_PS10_topic_tag_and_sound_pages_only_when_backed_by_posts(client, db):
    a, _ = await make_user(db, username="ama")
    kid, _ = await make_user(db, username="kid", date_of_birth=datetime.utcnow() - timedelta(days=365 * 15))
    for i in range(7):
        await make_video(db, a, category="comedy", country="UG", hashtags=["#KampalaComedy", "skits"], views_count=i,
                         sound={"id": "s1", "title": "Wano Sauti Yetu", "artist": "Wano Band"}, **THUMB)
    await make_video(db, kid, category="comedy", country="UG", hashtags=["kampalacomedy"], views_count=999,
                     sound={"id": "s1", "title": "Wano Sauti Yetu"}, **THUMB)
    for i in range(3):
        await make_video(db, a, category="music", country="NG", hashtags=["call0785476538"], **THUMB)
    items = (await client.get(f"{P}/topics")).json()["items"]
    keys = {(t["topic"], t["country"]) for t in items}
    assert ("comedy", "ug") in keys and ("comedy", None) in keys and ("music", "ng") not in keys
    t = (await client.get(f"{P}/topics/comedy?country=ug")).json()
    assert t["posts_count"] == 8 and len(t["posts"]) == 7 and all(p["creator"]["username"] == "ama" for p in t["posts"])
    assert t["country_name"] and t["creators"][0]["username"] == "ama"
    assert (await client.get(f"{P}/topics/music?country=ng")).status_code == 404
    assert (await client.get(f"{P}/topics/evil")).status_code == 404
    tags = {x["tag"] for x in (await client.get(f"{P}/tags")).json()["items"]}
    assert "kampalacomedy" in tags and "skits" in tags and not any("0785" in x for x in tags)
    tg = (await client.get(f"{P}/tags/KampalaComedy")).json()
    assert tg["tag"] == "kampalacomedy" and tg["posts_count"] == 8 and "skits" in tg["related"]
    for bad in ("x", "a" * 80, "$ne", "call0785476538"):
        assert (await client.get(f"{P}/tags/{bad}")).status_code == 404, bad
    snd = (await client.get(f"{P}/sounds")).json()["items"]
    assert snd[0]["id"] == "s1" and snd[0]["posts"] == 8
    s = (await client.get(f"{P}/sounds/s1")).json()
    assert s["title"] == "Wano Sauti Yetu" and len(s["posts"]) == 7
    assert (await client.get(f"{P}/sounds/../etc")).status_code == 404
