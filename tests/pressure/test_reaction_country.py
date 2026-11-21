"""reaction_country: '+1 Uganda' for people without a city, the creator's
country on the post's line, counts that come back off; and phone languages
counted per launch with nothing personal. Attacks: made-up language codes,
switch off keeps the old 'pick a city'."""

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def _on(db, country=True):
    await db.config.insert_one({"features": {"feed_fx": True, "reaction_country": country, "perf": True}})


async def test_RC1_plus_one_for_my_country_and_back(client, db):
    await _on(db)
    owner, _ = await make_user(db)                                   # localization.country = ug
    vid = await make_video(db, owner)
    fan, fh = await make_user(db, localization={"country": "ke"})
    r = await client.post(f"/api/v1/videos/{vid}/reactions", json={"kind": "plus_one", "on": True}, headers=fh)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["country"] == {"id": "ke", "name": "Kenya", "count": 1}
    assert out["creator_country"]["name"] == "Uganda"
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["country_reactions"] == {"ke": 1}
    await client.post(f"/api/v1/videos/{vid}/reactions", json={"kind": "plus_one", "on": False}, headers=fh)
    v = await db.videos.find_one({"_id": ObjectId(vid)})
    assert v["country_reactions"] == {"ke": 0} and v["reactions"]["plus_one"] == 0


async def test_RC2_switch_off_still_asks_for_a_city(client, db):
    await _on(db, country=False)
    owner, _ = await make_user(db)
    vid = await make_video(db, owner)
    _, fh = await make_user(db)
    r = await client.post(f"/api/v1/videos/{vid}/reactions", json={"kind": "plus_one", "on": True}, headers=fh)
    assert r.status_code == 409
    out = (await client.get(f"/api/v1/videos/{vid}/reactions", headers=fh)).json()
    assert "country" not in out and "creator_country" not in out


async def test_RC3_phone_language_counted_per_launch_junk_ignored(client, db):
    await _on(db)
    body = {"events": [{"name": "feed_load", "ms": 700}], "tz": "Africa/Kinshasa", "net": "cellular"}
    assert (await client.post("/api/v1/perf", json={**body, "lang": "fr"})).status_code == 200
    assert (await client.post("/api/v1/perf", json={**body, "lang": "FR"})).status_code == 200
    assert (await client.post("/api/v1/perf", json={**body, "lang": "x1"})).status_code == 200
    assert (await client.post("/api/v1/perf", json={**body, "lang": "<script>"})).status_code == 422
    assert (await client.post("/api/v1/perf", json=body)).status_code == 200      # older apps
    _, ah = await make_user(db, user_type="admin")
    rows = (await client.get("/api/v1/admin/perf/languages", headers=ah)).json()["rows"]
    assert rows == [{"lang": "fr", "country": "CD", "launches": 2}]
