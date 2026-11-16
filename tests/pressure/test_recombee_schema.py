"""Recombee schema repair (Nov 2026): the production database lacked
media_type, the count properties and the user properties, so Recombee refused
every new post's values and For You never recommended anything newer than
September. The server now adds what's missing and re-sends every video once."""

from datetime import datetime, timedelta

import pytest
from recombee_api_client.api_requests import (
    AddItemProperty, AddUserProperty, Batch, RecommendItemsToUser, SetItemValues,
)

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import FakeRecombee, _catalog, _feed, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


class MissingProps(Exception):
    pass


async def test_RS1_repair_adds_properties_and_resends_once(db, rb):
    from app.services import recommendations as rec
    creator, _ = await make_user(db)
    for _ in range(3):
        await make_video(db, creator)
    out = await rec.repair_schema(db)
    assert out["resynced"] is True and out["sent"] == 3
    names = {r.property_name for r in rb.of(AddItemProperty)}
    assert {"media_type", "views_count", "likes_count", "comments_count", "bookmarks_count"} <= names
    assert {r.property_name for r in rb.of(AddUserProperty)} == {"tags", "country"}
    sent = rb.of(SetItemValues)
    assert len(sent) == 3 and all(v.values["is_active"] is True and v.values["privacy"] == "public" for v in sent)
    # Next start: properties checked again, videos not re-sent.
    again = await rec.repair_schema(db)
    assert again["resynced"] is False and len(rb.of(SetItemValues)) == 3
    # An admin can force it.
    forced = await rec.repair_schema(db, force=True)
    assert forced["resynced"] is True and len(rb.of(SetItemValues)) == 6


async def test_RS2_only_one_server_process_resends(db, rb):
    from app.services import recommendations as rec
    now = datetime.utcnow()
    assert await rec._claim(db, now) is True
    assert await rec._claim(db, now) is False
    assert await rec._claim(db, now + timedelta(hours=2)) is True     # a crashed one is taken over


async def test_RS3_a_failed_resend_is_tried_again_next_start(db, rb):
    from app.services import recommendations as rec
    creator, _ = await make_user(db)
    await make_video(db, creator)
    rb.down = True
    out = await rec.repair_schema(db)
    assert out["failed"] == 1
    assert (await db.config.find_one({"_id": rec.SCHEMA_STATE_ID})) is None
    rb.down = False
    await db.config.delete_many({"_id": {"$regex": "lock"}})
    assert (await rec.repair_schema(db))["resynced"] is True


async def test_RS4_a_missing_property_is_added_and_the_post_sent_again(db, rb, monkeypatch):
    from app.services import recommendations as rec
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    video = await db.videos.find_one({})
    calls = {"n": 0}
    real = rb.send

    async def picky(req):
        if isinstance(req, SetItemValues):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MissingProps('item property "media_type" does not exist!')
        return await real(req)
    monkeypatch.setattr(rec, "recombee_send", picky)
    assert await rec.push_item(video) is True
    assert calls["n"] == 2 and rb.of(AddItemProperty)


async def test_RS5_unknown_scenario_asks_as_for_you(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    _, h = await make_user(db)
    r = await _feed(client, h, scenario="photo-viewer")
    assert r.status_code == 200
    assert rb.of(RecommendItemsToUser)[-1].scenario == "for-you"
    await _feed(client, h, scenario="trending")
    assert rb.of(RecommendItemsToUser)[-1].scenario == "trending"


async def test_RS6_repair_endpoint_is_admin_only(client, db, rb):
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/repair", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.post("/api/v1/recombee/repair", headers=ha)
    assert r.status_code == 200 and "created" in r.json()


async def test_RS7_invalid_thumbnails_are_left_out_not_sent(db):
    from app.services.recommendations import item_values
    good = {"_id": 1, "urls": {"thumbnail": "https://videodelivery.net/abc/thumbnails/thumbnail.jpg"}}
    for bad in ("", "not a url", "/relative/pic.jpg", "file:///x.jpg", "https://", "https://x.com/a b.jpg"):
        v = item_values({"_id": 2, "urls": {"thumbnail": bad}, "thumbnail": bad})
        assert "thumbnail" not in v or v["thumbnail"].startswith("http"), bad
    vg = item_values(good)
    assert vg.get("thumbnail", "").startswith("https://")
