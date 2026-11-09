"""Pages that looked dead: a Home Ground city page with one row, and Trending
sounds with the same music note on every row. Attacks written with the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_engagement import _sound

pytestmark = pytest.mark.asyncio
P = "/api/v1/home-ground/pulse"


async def _on(db):
    await db.config.insert_one({"features": {"home_ground": True, "leaderboard": True}})


async def _local(db, city="accra", dob=datetime(1990, 1, 1), **extra):
    return await make_user(db, date_of_birth=dob, localization={"country": "gh"},
                           home_ground={"city": city, "set_at": extra.pop("set_at", datetime.utcnow())},
                           profile_picture="https://cdn/p.jpg", **extra)


async def test_UL1_city_pulse_has_people_posts_and_faces(client, db):
    await _on(db)
    a, _ = await _local(db, set_at=datetime.utcnow() - timedelta(days=30))
    b, _ = await _local(db)
    kid, _ = await _local(db, dob=datetime.utcnow() - timedelta(days=365 * 14))
    banned, _ = await _local(db, is_active=False)
    other, _ = await _local(db, city="lagos")
    hot = await make_video(db, a, views_count=900, isReadyToStream=True)
    await make_video(db, b, views_count=10, isReadyToStream=True)
    await make_video(db, a, privacy="private", views_count=99999)
    await make_video(db, a, views_count=5000, created_at=datetime.now(timezone.utc) - timedelta(days=30))
    await make_video(db, banned, views_count=8000)
    await make_video(db, kid, views_count=7000)
    await make_video(db, other, views_count=6000)
    r = await client.get(P, params={"city": "accra", "authenticated": "false"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["place"]["name"] == "Accra"
    assert d["members"] == 2 and d["new_this_week"] == 1            # under-18s and banned never counted
    assert d["posts"][0]["id"] == hot and len(d["posts"]) == 2
    assert {f["id"] for f in d["faces"]} == {a, b}


async def test_UL2_country_pulse_and_bad_input(client, db):
    await _on(db)
    u, _ = await make_user(db, localization={"country": "UG"})
    v = await make_video(db, u, country="UG", views_count=5, isReadyToStream=True)
    d = (await client.get(P, params={"country": "ug", "authenticated": "false"})).json()
    assert d["place"]["name"] == "Uganda" and d["posts"][0]["id"] == v and d["members"] >= 1
    for bad in [{}, {"city": "atlantis"}, {"country": "u1"}, {"country": "$n"}]:
        assert (await client.get(P, params={**bad, "authenticated": "false"})).status_code in (400, 404, 422), bad


async def test_UL3_pulse_is_off_with_the_switch(client, db):
    assert (await client.get(P, params={"city": "accra", "authenticated": "false"})).status_code == 404


async def test_UL4_trending_sounds_have_a_cover_and_faces(client, db):
    makers = []
    for i in range(4):
        u, _ = await make_user(db, profile_picture=f"https://cdn/{i}.jpg")
        makers.append(u)
        await make_video(db, u, sound=_sound(9), views_count=10 * (i + 1), thumbnail=f"https://cdn/t{i}.jpg")
    r = (await client.get("/api/v1/music/trending", params={"authenticated": "false"})).json()
    row = r[0]
    assert row["sound"]["id"] == "song9"
    assert row["cover"]["post_id"]                      # the most-watched post using it
    assert 1 <= len(row["faces"]) <= 3
    assert all(f["profile_picture"] for f in row["faces"])
