"""Tapping a tile in a grid (Discover, profile, Trending) opens THAT post, whatever
position the tile was in (the app sends skip=<tile index> with video_id)."""

import pytest

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def test_TT1_tapped_post_leads_at_any_position(client, db):
    a, _ = await make_user(db)
    _, h = await make_user(db)
    ids = [await make_video(db, a, title=f"p{i}", views_count=i) for i in range(12)]
    for skip in (0, 3, 7):
        tapped = ids[skip + 1]
        r = await client.get("/api/v1/feed/", params={"privacy": "public", "limit": 4, "skip": skip,
                                                       "video_id": tapped, "scenario": "explore"}, headers=h)
        assert r.status_code == 200, r.text
        got = [v["id"] for v in r.json()]
        assert got[0] == tapped, (skip, got)
        assert got.count(tapped) == 1
    # A page without video_id is the plain list.
    r = await client.get("/api/v1/feed/", params={"privacy": "public", "limit": 4, "skip": 4}, headers=h)
    assert r.status_code == 200 and len(r.json()) <= 4
