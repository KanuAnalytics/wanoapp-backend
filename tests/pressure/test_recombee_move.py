"""Moving Recombee to a region near Africa (Nov 2026): the region is a
setting, writes are copied to the new database while it fills, and a job
fills it from Wano's own records without starting recommendations from zero."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId
from recombee_api_client.api_client import Region
from recombee_api_client.api_requests import (
    AddBookmark, AddCartAddition, AddPurchase, AddRating, Batch, RecommendItemsToUser,
    RecommendNextItems, SetItemValues, SetUserValues, SetViewPortion,
)

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import FakeRecombee, _catalog, _feed, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


class FakeNewDb:
    """Stands in for the new database's client (synchronous, like the real one)."""

    def __init__(self, existing=()):
        self.got = []
        self.existing = set(existing)    # requests it already has -> 409

    def send(self, req):
        if isinstance(req, Batch):
            out = []
            for r in req.requests:
                self.got.append(r)
                key = (type(r).__name__, getattr(r, "user_id", None), getattr(r, "item_id", None),
                       str(getattr(r, "timestamp", "")))
                if "Add" in type(r).__name__ and key in self.existing:
                    out.append({"code": 409, "json": {"error": "already there"}})
                else:
                    self.existing.add(key)
                    out.append({"code": 200, "json": "ok"})
            return out
        self.got.append(req)
        return "ok"

    def of(self, cls):
        return [r for r in self.got if isinstance(r, cls)]


@pytest.fixture
def newdb(monkeypatch):
    from app.services import recombee_service as rs
    fake = FakeNewDb()
    monkeypatch.setattr(rs, "new_client", fake)
    return fake


async def test_MV1_region_is_a_setting():
    from app.services.recombee_service import region_of
    assert region_of("eu-west") is Region.EU_WEST
    assert region_of("EU_WEST") is Region.EU_WEST
    assert region_of("us-west") is Region.US_WEST
    assert region_of("mars") is Region.US_WEST       # a typo never breaks the server


async def test_MV1_no_move_configured_by_default(db):
    from app.services import recombee_service as rs, recombee_move
    assert rs.new_client is None
    r = await recombee_move.start(db)
    assert r["started"] is False


async def test_MV2_only_writes_are_copied(db, newdb, monkeypatch):
    from app.services import recombee_service as rs
    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    monkeypatch.setattr(rs.recombee_client, "send", lambda req: {"recomms": []})
    await rs.recombee_send(SetViewPortion("u1", "v1", 0.5))
    await rs.recombee_send(RecommendItemsToUser("u1", 5))
    await rs.recombee_send(Batch([AddRating("u1", "v2", 1.0)]))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(newdb.got) >= 2:
            break
    kinds = [type(r).__name__ for r in newdb.got]
    assert "SetViewPortion" in kinds and "AddRating" in kinds   # the batch, unpacked
    assert "RecommendItemsToUser" not in kinds
    assert rs.stats()["calls"]["mirror:SetViewPortion"]["ok"] == 1


async def test_MV2_a_failed_copy_never_breaks_the_live_call(db, monkeypatch):
    from app.services import recombee_service as rs

    class Broken:
        def send(self, req):
            raise RuntimeError("new database down")

    monkeypatch.setattr(rs, "new_client", Broken())
    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    monkeypatch.setattr(rs.recombee_client, "send", lambda req: "ok")
    assert await rs.recombee_send(SetViewPortion("u1", "v1", 0.5)) == "ok"
    await asyncio.sleep(0.05)
    assert rs.stats()["calls"]["mirror:SetViewPortion"]["failed"] == 1


async def _records(db):
    viewer, _ = await make_user(db, tags=["music"], localization={"country": "ng"})
    creator, _ = await make_user(db)
    v1 = await make_video(db, creator)
    v2 = await make_video(db, creator)
    v3 = await make_video(db, creator)
    when = datetime(2026, 11, 1, 12, 0)
    await db.watch_history.insert_many([
        {"user_id": ObjectId(viewer), "video_id": ObjectId(v1), "portion": 0.9, "watched_at": when},
        {"user_id": ObjectId(viewer), "video_id": ObjectId(v2), "watched_at": when, "not_interested": True},
    ])
    await db.users.update_one({"_id": ObjectId(viewer)},
                              {"$set": {"liked_videos": [ObjectId(v1)], "bookmarked_videos": [ObjectId(v3)]}})
    await db.comments.insert_one({"user_id": ObjectId(viewer), "video_id": ObjectId(v1),
                                  "created_at": when, "is_active": True})
    await db.comments.insert_one({"user_id": ObjectId(viewer), "video_id": ObjectId(v2),
                                  "created_at": when, "is_active": False})
    await db.engagement_once.insert_one({"_id": f"share:{viewer}:{v1}", "at": when})
    await db.engagement_once.insert_one({"_id": f"like:{viewer}:{v1}", "at": when})
    await db.feed_impressions.insert_many([
        {"viewer": "anon-dev1", "video_id": ObjectId(v2), "portion": 0.4, "at": when},
        {"viewer": "anon-dev1", "video_id": ObjectId(v3), "at": when},        # shown, never watched
        {"viewer": viewer, "video_id": ObjectId(v3), "portion": 1.0, "at": when},   # already in watches
    ])
    return viewer, creator, (v1, v2, v3)


async def test_MV3_move_copies_everything_wano_keeps(db, newdb):
    from app.services import recombee_move
    viewer, creator, (v1, v2, v3) = await _records(db)
    out = await recombee_move.run(db)
    assert out["state"] == "done", out
    assert {i.item_id for i in newdb.of(SetItemValues)} == {v1, v2, v3}
    assert {u.user_id for u in newdb.of(SetUserValues)} == {viewer, creator}
    tags = [u for u in newdb.of(SetUserValues) if u.user_id == viewer][0].values
    assert tags == {"tags": ["music"], "country": "ng"}

    views = [(r.user_id, r.item_id, r.portion) for r in newdb.of(SetViewPortion)]
    assert (viewer, v1, 0.9) in views
    assert ("anon-dev1", v2, 0.4) in views
    assert not any(u == "anon-dev1" and i == v3 for u, i, _ in views)   # never watched
    assert sum(1 for u, *_ in views if u == viewer) == 1                 # not copied twice

    ratings = {(r.user_id, r.item_id, r.rating) for r in newdb.of(AddRating)}
    assert (viewer, v1, 1.0) in ratings and (viewer, v2, -1.0) in ratings
    assert [(b.user_id, b.item_id) for b in newdb.of(AddBookmark)] == [(viewer, v3)]
    assert [(c.user_id, c.item_id) for c in newdb.of(AddCartAddition)] == [(viewer, v1)]
    assert [(p.user_id, p.item_id) for p in newdb.of(AddPurchase)] == [(viewer, v1)]
    # Original times travel with them.
    assert str(newdb.of(AddPurchase)[0].timestamp).startswith("2026-11-01T12:00")
    assert all(getattr(r, "cascade_create", None) is True for r in newdb.got if isinstance(r, AddRating))
    steps = out["steps"]
    assert all(steps[s]["done"] for s in recombee_move.STEPS)
    assert sum(steps[s]["failed"] for s in recombee_move.STEPS) == 0


async def test_MV3_running_again_adds_nothing_twice(db, newdb):
    from app.services import recombee_move
    await _records(db)
    await recombee_move.run(db)
    first = len(newdb.of(AddRating)) + len(newdb.of(AddBookmark)) + len(newdb.of(AddPurchase))
    out = await recombee_move.run(db)
    s = out["steps"]
    assert s["likes"]["already"] == 1 and s["likes"]["sent"] == 0
    assert s["bookmarks"]["already"] == 1 and s["shares"]["already"] == 1
    assert first == 4


async def test_MV3_failed_batches_are_counted_not_fatal(db, monkeypatch):
    from app.services import recombee_service as rs, recombee_move

    class Flaky(FakeNewDb):
        def send(self, req):
            if isinstance(req, Batch) and any(isinstance(r, SetItemValues) for r in req.requests):
                raise RuntimeError("timeout")
            return super().send(req)

    monkeypatch.setattr(rs, "new_client", Flaky())
    await _records(db)
    out = await recombee_move.run(db)
    assert out["state"] == "done"
    assert out["steps"]["videos"]["failed"] == 3
    assert out["steps"]["watches"]["sent"] == 2


async def test_MV4_endpoints_are_admin_only(client, db, newdb):
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/move", headers=h)).status_code in (401, 403)
    assert (await client.get("/api/v1/recombee/move", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.post("/api/v1/recombee/move", headers=ha)
    assert r.status_code == 200 and r.json()["started"] is True
    for _ in range(100):
        await asyncio.sleep(0.02)
        st = (await client.get("/api/v1/recombee/move", headers=ha)).json()
        if st.get("state") == "done":
            break
    assert st["state"] == "done" and st["configured"] is True


async def test_MV5_unknown_chain_starts_a_fresh_one(client, db, rb):
    """An app holding a recomm_id from the old database still gets picks."""
    _, ids = await _catalog(db)
    rb.pool = ids
    _, h = await make_user(db)
    real = rb.send

    async def picky(req):
        if isinstance(req, RecommendNextItems):
            rb.requests.append(req)
            raise RuntimeError("Recommendation id not found")
        return await real(req)

    from app.services import for_you
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(for_you, "recombee_send", picky)
    try:
        r = await _feed(client, h, next_recomm_id="old-database-chain")
    finally:
        mp.undo()
    assert r.status_code == 200
    body = r.json()
    assert body and any(v["source"] == "personal" for v in body)
    assert rb.of(RecommendNextItems) and rb.of(RecommendItemsToUser)
