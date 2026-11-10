"""Wano TV extras (tv_plus): Continue watching / resume and My List. Attacks first."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_tv_protect import _title, spaces  # noqa: F401
from tests.pressure.test_tv_series import _series

pytestmark = pytest.mark.asyncio
P = "/api/v1/tv/plus"


async def _on(db):
    await db.config.insert_one({"features": {"tv_plus": True}})


async def test_TP1_off_by_default_and_needs_sign_in(client, db, spaces):
    t = await _title(db, "middleton")
    _, h = await make_user(db)
    assert (await client.get(f"{P}/me", headers=h)).status_code == 404
    assert (await client.put(f"{P}/progress/{t}", json={"position": 60, "duration": 600}, headers=h)).status_code == 404
    await _on(db)
    assert (await client.get(f"{P}/me")).status_code == 401
    assert (await client.put(f"{P}/list/{t}")).status_code == 401


async def test_TP2_hostile_bodies_and_ids(client, db, spaces):
    await _on(db)
    t = await _title(db, "middleton")
    _, h = await make_user(db)
    bad = [{"position": -1, "duration": 600}, {"position": 5, "duration": 0}, {"position": "60", "duration": 600},
           {"position": True, "duration": 600}, {"position": 1e12, "duration": 600},
           {"position": 60, "duration": 600, "episode": "../x"}, {"position": 60, "duration": 600, "x": 1},
           {"position": {"$gt": 0}, "duration": 600}, {"position": float("nan") if False else 60}]
    for b in bad:
        r = await client.put(f"{P}/progress/{t}", json=b, headers=h)
        assert r.status_code == 422, (b, r.status_code)
    assert (await client.put(f"{P}/progress/nope", json={"position": 60, "duration": 600}, headers=h)).status_code == 400
    assert (await client.put(f"{P}/progress/{ObjectId()}", json={"position": 60, "duration": 600},
                             headers=h)).status_code == 404
    # An episode a film doesn't have.
    assert (await client.put(f"{P}/progress/{t}", json={"position": 60, "duration": 600, "episode": "s1e1"},
                             headers=h)).status_code == 404
    assert (await client.put(f"{P}/list/{ObjectId()}", headers=h)).status_code == 404


async def test_TP3_resume_point_saved_then_film_drops_off_when_finished(client, db, spaces):
    await _on(db)
    t = await _title(db, "middleton")
    _, h = await make_user(db)
    r = await client.put(f"{P}/progress/{t}", json={"position": 5, "duration": 660}, headers=h)
    assert r.json() == {"saved": False}            # just opened it: not "continue watching"
    assert (await client.get(f"{P}/me", headers=h)).json()["continue"] == []
    await client.put(f"{P}/progress/{t}", json={"position": 310.5, "duration": 660}, headers=h)
    c = (await client.get(f"{P}/me", headers=h)).json()["continue"]
    assert len(c) == 1 and c[0]["title_id"] == t and c[0]["position"] == 310.5 and c[0]["done"] is False
    # Past the end is clamped; finishing a film removes it.
    r = await client.put(f"{P}/progress/{t}", json={"position": 9999, "duration": 660}, headers=h)
    assert r.json()["done"] is True
    assert (await client.get(f"{P}/me", headers=h)).json()["continue"] == []


async def test_TP4_series_keeps_the_last_episode_even_when_finished_so_the_app_offers_the_next(client, db, spaces):
    await _on(db)
    s = await _series(db, "roots")
    _, h = await make_user(db)
    await client.put(f"{P}/progress/{s}", json={"position": 1190, "duration": 1200, "episode": "s1e2"}, headers=h)
    c = (await client.get(f"{P}/me", headers=h)).json()["continue"]
    assert c[0]["episode"] == "s1e2" and c[0]["done"] is True
    assert (await client.put(f"{P}/progress/{s}", json={"position": 60, "duration": 1200, "episode": "s9e9"},
                             headers=h)).status_code == 404


async def test_TP5_newest_first_only_mine_taken_down_titles_hidden_and_removable(client, db, spaces):
    await _on(db)
    a = await _title(db, "middleton")
    b = await _title(db, "one-field")
    _, h = await make_user(db)
    _, other = await make_user(db)
    await client.put(f"{P}/progress/{a}", json={"position": 100, "duration": 660}, headers=h)
    await asyncio.sleep(0.01)
    await client.put(f"{P}/progress/{b}", json={"position": 100, "duration": 660}, headers=h)
    assert [x["title_id"] for x in (await client.get(f"{P}/me", headers=h)).json()["continue"]] == [b, a]
    assert (await client.get(f"{P}/me", headers=other)).json() == {"continue": [], "list": []}
    await db.tv_titles.update_one({"_id": ObjectId(b)}, {"$set": {"is_active": False}})
    assert [x["title_id"] for x in (await client.get(f"{P}/me", headers=h)).json()["continue"]] == [a]
    await client.delete(f"{P}/progress/{a}", headers=h)
    assert (await client.get(f"{P}/me", headers=h)).json()["continue"] == []
    # Very old progress fades out.
    await client.put(f"{P}/progress/{a}", json={"position": 100, "duration": 660}, headers=h)
    await db.tv_progress.update_many({}, {"$set": {"updated_at": datetime.utcnow() - timedelta(days=400)}})
    assert (await client.get(f"{P}/me", headers=h)).json()["continue"] == []


async def test_TP6_my_list_add_twice_remove_newest_first(client, db, spaces):
    await _on(db)
    a = await _title(db, "middleton")
    b = await _title(db, "one-field")
    _, h = await make_user(db)
    for t in (a, b, b):
        assert (await client.put(f"{P}/list/{t}", headers=h)).json() == {"in_list": True}
    assert (await client.get(f"{P}/me", headers=h)).json()["list"] == [b, a]
    assert (await db.tv_list.count_documents({})) == 2
    await client.delete(f"{P}/list/{b}", headers=h)
    await client.delete(f"{P}/list/{b}", headers=h)
    assert (await client.get(f"{P}/me", headers=h)).json()["list"] == [a]


async def test_TP7_list_is_capped(client, db, spaces, monkeypatch):
    from app.api.v1 import tv_plus
    monkeypatch.setattr(tv_plus, "LIST_LIMIT", 2)
    await _on(db)
    ids = [await _title(db, f"t{i}") for i in range(3)]
    _, h = await make_user(db)
    await client.put(f"{P}/list/{ids[0]}", headers=h)
    await client.put(f"{P}/list/{ids[1]}", headers=h)
    assert (await client.put(f"{P}/list/{ids[2]}", headers=h)).status_code == 409
    assert (await client.put(f"{P}/list/{ids[1]}", headers=h)).status_code == 200   # already in: fine


async def test_TP8_catalogue_routes_still_work(client, db, spaces):
    await _on(db)
    await _title(db, "middleton")
    r = await client.get("/api/v1/tv/titles", params={"authenticated": "false"})
    assert r.status_code == 200 and len(r.json()) == 1
