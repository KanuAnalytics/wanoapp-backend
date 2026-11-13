"""Logged-out viewers can browse and play Wano TV; personal parts stay private."""

import pytest

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401
from tests.pressure.test_tv_protect import _title, spaces  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_T1_logged_out_can_browse_and_gets_protected_links(client, db, rb, spaces):
    await _title(db, "middleton")
    await db.tv_genres.insert_one({"name": "Documentary", "image": "https://x/y.jpg", "is_active": True, "order": 1})
    r = await client.get("/api/v1/tv/titles?authenticated=false")
    assert r.status_code == 200, r.text
    t = r.json()[0]
    assert "/api/v1/tv/stream/" in t["video_url"] and "digitaloceanspaces" not in t["video_url"]
    g = await client.get("/api/v1/tv/genres?authenticated=false")
    assert g.status_code == 200 and g.json()[0]["name"] == "Documentary"


async def test_T2_logged_out_link_actually_plays(client, db, rb, spaces):
    from urllib.parse import urlparse
    await _title(db, "middleton")
    t = (await client.get("/api/v1/tv/titles?authenticated=false")).json()[0]
    assert (await client.get(urlparse(t["video_url"]).path)).status_code == 200


async def test_T3_history_and_watch_recording_still_need_login(client, db, rb, spaces):
    tid = await _title(db, "middleton")
    assert (await client.get("/api/v1/tv/history?authenticated=false")).status_code == 401
    assert (await client.post(f"/api/v1/tv/history/{tid}?authenticated=false")).status_code == 401


async def test_T4_logged_in_unchanged_and_bad_tokens_refused(client, db, rb, spaces):
    await _title(db, "middleton")
    _, h = await make_user(db)
    assert (await client.get("/api/v1/tv/titles", headers=h)).status_code == 200
    assert (await client.get("/api/v1/tv/titles", headers={"Authorization": "Bearer junk"})).status_code == 401
    _, gone = await make_user(db, is_active=False)
    assert (await client.get("/api/v1/tv/titles", headers=gone)).status_code in (401, 403)


async def test_T5_no_flag_and_no_token_keeps_the_app_contract(client, db, rb, spaces):
    await _title(db, "middleton")
    assert (await client.get("/api/v1/tv/titles")).status_code == 401


async def test_T9_genres_only_with_titles_no_series_and_real_pictures(client, db, rb, spaces):
    await _title(db, "middleton", genres=["Documentary", "Series"], popularity=5)
    await _title(db, "one-field", genres=["Documentary", "Sport"], popularity=9)
    for i, (name, img) in enumerate([("Documentary", "https://picsum.photos/seed/x/322/200"), ("Series", "https://x/s.jpg"),
                                     ("Comedy", "https://picsum.photos/a"), ("Sport", "https://cdn.example/sport.jpg")]):
        await db.tv_genres.insert_one({"name": name, "image": img, "is_active": True, "order": i})
    g = (await client.get("/api/v1/tv/genres?authenticated=false")).json()
    assert [x["name"] for x in g] == ["Documentary", "Sport"]
    assert "picsum" not in g[0]["image"] and "one-field" in g[0]["image"]     # most popular title's picture
    assert g[1]["image"] == "https://cdn.example/sport.jpg"                     # real artwork kept
