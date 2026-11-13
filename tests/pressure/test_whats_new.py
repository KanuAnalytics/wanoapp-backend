"""What's new: welcome, not noise. Attacks: off means nothing; never more than
one a day, never twice, never to brand-new accounts, never for a feature that's
off or an app too old; counts can't be pumped; only admins write them; nothing
but plain text and Wano's own pictures goes out."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
W = "/api/v1/whats-new"
OLD = datetime.utcnow() - timedelta(days=30)
CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"


async def _on(db, **extra):
    await db.config.insert_one({"features": {"whats_new": True, **extra}})


async def _admin(db):
    return (await make_user(db, user_type="admin"))[1]


async def _add(client, ah, **kw):
    body = {"title": "New: quick posts", "body": "Put a voice note on your photo.", "button": "Make one",
            "action": "quick_post", "active": True, **kw}
    r = await client.post("/api/v1/admin/announcements", json=body, headers=ah)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _next(client, h, app="2.3.28"):
    r = await client.get(f"{W}/next", params={"app": app}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["item"]


async def test_WN1_off_by_default(client, db):
    ah = await _admin(db)
    aid = await _add(client, ah)
    _, h = await make_user(db, created_at=OLD)
    assert (await client.get(f"{W}/next", headers=h)).status_code == 404
    assert (await client.post(f"{W}/{aid}/event", json={"kind": "shown"}, headers=h)).status_code == 404


async def test_WN2_once_each_and_one_a_day(client, db):
    await _on(db)
    ah = await _admin(db)
    a1 = await _add(client, ah, title="First", priority=5)
    a2 = await _add(client, ah, title="Second", priority=1)
    _, h = await make_user(db, created_at=OLD)
    item = await _next(client, h)
    assert item["id"] == a1 and item["title"] == "First" and item["action"] == "quick_post"
    await client.post(f"{W}/{a1}/event", json={"kind": "shown"}, headers=h)
    assert await _next(client, h) is None                     # one a day
    await db.announce_last.update_many({}, {"$set": {"at": datetime.utcnow() - timedelta(hours=25)}})
    assert (await _next(client, h))["id"] == a2                # next day: the next one, never the first again
    await client.post(f"{W}/{a2}/event", json={"kind": "shown"}, headers=h)
    await db.announce_last.update_many({}, {"$set": {"at": datetime.utcnow() - timedelta(days=5)}})
    assert await _next(client, h) is None


async def test_WN3_not_for_new_accounts_old_apps_or_switched_off_features(client, db):
    await _on(db)
    ah = await _admin(db)
    await _add(client, ah, feature="quick_posts", min_app="2.3.28")
    _, new_h = await make_user(db)                             # created just now
    _, h = await make_user(db, created_at=OLD)
    assert await _next(client, new_h) is None
    assert await _next(client, h) is None                      # quick_posts is off
    await db.config.update_one({}, {"$set": {"features.quick_posts": True}})
    assert await _next(client, h, "2.3.27") is None             # too old
    assert await _next(client, h, "nonsense") is None
    r = await client.get(f"{W}/next", headers=h)                # no version said: not shown
    assert r.json()["item"] is None
    assert (await _next(client, h, "2.3.28"))["title"] == "New: quick posts"
    assert (await _next(client, h, "2.10.0")) is not None       # compares numbers, not text


async def test_WN4_counts_once_per_person_and_only_after_shown(client, db):
    await _on(db)
    ah = await _admin(db)
    aid = await _add(client, ah)
    _, h = await make_user(db, created_at=OLD)
    r = await client.post(f"{W}/{aid}/event", json={"kind": "tap"}, headers=h)
    assert r.json()["counted"] is False                        # never shown: a tap doesn't count
    for _ in range(5):
        await client.post(f"{W}/{aid}/event", json={"kind": "shown"}, headers=h)
    for _ in range(5):
        await client.post(f"{W}/{aid}/event", json={"kind": "tap"}, headers=h)
    await client.post(f"{W}/{aid}/event", json={"kind": "close"}, headers=h)
    row, = (await client.get("/api/v1/admin/announcements", headers=ah)).json()["items"]
    assert (row["shown"], row["tap"], row["close"], row["tap_rate"]) == (1, 1, 0, 1.0)
    for bad in ({"kind": "view"}, {}, {"kind": "tap", "x": 1}):
        assert (await client.post(f"{W}/{aid}/event", json=bad, headers=h)).status_code == 422
    for bad in ("nope", str(ObjectId()), "$where"):
        assert (await client.post(f"{W}/{bad}/event", json={"kind": "shown"}, headers=h)).status_code == 404


async def test_WN5_only_admins_write_and_only_clean_content(client, db):
    await _on(db)
    ah = await _admin(db)
    _, uh = await make_user(db, created_at=OLD)
    good = {"title": "Hi", "body": "Hello", "active": False}
    assert (await client.post("/api/v1/admin/announcements", json=good, headers=uh)).status_code == 403
    assert (await client.post("/api/v1/admin/announcements/suggested", headers=uh)).status_code == 403
    for bad in ({"title": "", "body": "x"}, {"title": "x" * 61, "body": "x"}, {"title": "x", "body": "x" * 181},
                {"title": "x", "body": "x", "action": "open_url"}, {"title": "x", "body": "x", "action": "wano_tv"},
                {"title": "x", "body": "x", "feature": "hack"}, {"title": "x", "body": "x", "min_app": "latest"},
                {"title": "x", "body": "x", "image": "https://evil.com/a.png"},
                {"title": "x", "body": "x", "priority": 1000}):
        r = await client.post("/api/v1/admin/announcements", json=bad, headers=ah)
        assert r.status_code in (400, 422), (bad, r.text)
    aid = await _add(client, ah, title="Pic‮", image=f"{CDN}/announce/a.webp", active=True)
    item = await _next(client, uh)
    assert item["title"] == "Pic" and item["image"] == f"{CDN}/announce/a.webp"


async def test_WN6_drafts_turning_on_and_the_list(client, db):
    await _on(db, gzip_api=True)
    ah = await _admin(db)
    assert (await client.post("/api/v1/admin/announcements/suggested", headers=ah)).json() == {"added": 3}
    assert (await client.post("/api/v1/admin/announcements/suggested", headers=ah)).json() == {"added": 0}
    _, h = await make_user(db, created_at=OLD)
    assert await _next(client, h) is None                      # drafts are off
    rows = (await client.get("/api/v1/admin/announcements", headers=ah)).json()["items"]
    data = next(a for a in rows if a["title"].startswith("Wano now uses less data"))
    body = {k: data[k] for k in ("title", "body", "button", "action", "feature", "min_app", "priority")}
    r = await client.put(f"/api/v1/admin/announcements/{data['id']}", json={**body, "active": True}, headers=ah)
    assert r.status_code == 200
    assert (await _next(client, h))["title"] == "Wano now uses less data"
    lst = (await client.get(W, params={"app": "2.3.28"}, headers=h)).json()["items"]
    assert [a["title"] for a in lst] == ["Wano now uses less data"]   # switched-off features stay out of the list too
    assert (await client.get(W, headers={})).status_code == 401
