"""Force update from the admin page: the numbers land where GET /config/version
reads them, bad input is refused, only admins can do it, and it's logged."""

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

A = "/api/v1/admin/app-version"
V = "/api/v1/config/version"


async def _admin(db):
    return await make_user(db, user_type="admin")


async def test_FU1_only_admins(client, db):
    _, uh = await make_user(db)
    for method, kw in (("GET", {}), ("PUT", {"json": {"platform": "android", "version": "2.3.32"}})):
        assert (await client.request(method, A, **kw)).status_code == 401
        assert (await client.request(method, A, headers=uh, **kw)).status_code == 403


async def test_FU2_android_and_ios_reach_the_version_check(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1", "showUpdateIos": True})
    r = await client.put(A, headers=ah, json={"platform": "android", "version": "2.3.32"})
    assert r.status_code == 200 and r.json() == {"android": "2.3.32", "ios": None, "all": "2.1.1"}
    r = await client.put(A, headers=ah, json={"platform": "ios", "version": " 2.3.32 "})
    assert r.json()["ios"] == "2.3.32"
    v = (await client.get(V)).json()
    assert v["appVersionAndroid"] == "2.3.32" and v["appVersionIos"] == "2.3.32" and v["appVersionNumber"] == "2.1.1"
    assert v["showUpdateIos"] is True                   # other config fields untouched
    assert await db.config.count_documents({}) == 1


async def test_FU3_written_into_the_newest_document_with_the_switches(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "1.0.0"})
    newest = await db.config.insert_one({"appVersionNumber": "2.1.1", "features": {"messages": True}})
    await client.put(A, headers=ah, json={"platform": "android", "version": "2.3.32"})
    doc = await db.config.find_one({"_id": newest.inserted_id})
    assert doc["appVersionAndroid"] == "2.3.32" and doc["features"] == {"messages": True}
    assert (await client.get("/api/v1/config/features")).json()["messages"] is True
    assert (await client.get(V)).json()["appVersionAndroid"] == "2.3.32"


async def test_FU4_bad_versions_refused(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1"})
    for bad in ("2.3", "v2.3.32", "2.3.32.1", "2.3.x", "$gt", "1000.0.0", "2.3.32; drop"):
        r = await client.put(A, headers=ah, json={"platform": "android", "version": bad})
        assert r.status_code == 422, bad
    for body in ({"platform": "web", "version": "2.3.32"}, {"platform": "android", "version": "2.3.32", "x": 1},
                 {"platform": "android", "version": {"$set": 1}}):
        assert (await client.put(A, headers=ah, json=body)).status_code == 422, body
    assert (await client.get(V)).json()["appVersionAndroid"] is None


async def test_FU5_clearing_stops_forcing_and_never_breaks_the_check(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1", "appVersionAndroid": "2.3.32"})
    r = await client.put(A, headers=ah, json={"platform": "android", "version": ""})
    assert r.json()["android"] is None
    r = await client.put(A, headers=ah, json={"platform": "all", "version": None})
    assert r.json()["all"] == "0.0.0"                   # the check needs this field; 0.0.0 forces nobody
    v = await client.get(V)
    assert v.status_code == 200 and v.json()["appVersionNumber"] == "0.0.0"


async def test_FU6_works_with_no_config_at_all(client, db):
    _, ah = await _admin(db)
    r = await client.put(A, headers=ah, json={"platform": "ios", "version": "2.3.32"})
    assert r.status_code == 200 and r.json() == {"android": None, "ios": "2.3.32", "all": "0.0.0"}
    v = await client.get(V)
    assert v.status_code == 200 and v.json()["appVersionIos"] == "2.3.32"


async def test_FU7_logged_with_before_and_after(client, db):
    admin, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": "2.1.1"})
    await client.put(A, headers=ah, json={"platform": "android", "version": "2.3.32"})
    row = await db.admin_log.find_one({"action": "app_version"})
    assert row and row["detail"] == {"platform": "android", "from": None, "to": "2.3.32"}
    log = (await client.get("/api/v1/admin/log", headers=ah)).json()
    assert any(r["action"] == "app_version" for r in log)


async def test_FU8_quoted_legacy_value_reads_clean(client, db):
    _, ah = await _admin(db)
    await db.config.insert_one({"appVersionNumber": '"2.1.1"'})
    assert (await client.get(A, headers=ah)).json()["all"] == "2.1.1"
