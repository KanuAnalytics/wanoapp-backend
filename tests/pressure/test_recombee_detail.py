"""recombee_detail: watching a post also sends "opened this post" (AddDetailView)
with the recommendation it came from, once per counted view. And the one-time
clean-up of staff history from both Recombee databases."""

import asyncio

import pytest
from recombee_api_client.api_requests import (
    AddDetailView, Batch, DeleteDetailView, DeleteRating, DeleteViewPortion, SetViewPortion,
)

from app.services import recombee_cleanup
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sent(monkeypatch):
    from app.services import recombee_service as rs
    got = []

    async def send(req):
        got.append(req)
    monkeypatch.setattr(rs, "recombee_send", send)
    return got


async def _watch(client, h, vid, **body):
    r = await client.post(f"/api/v1/videos/{vid}/watch", json={"portion": 0.8, **body}, headers=h)
    assert r.status_code == 200, r.text


async def test_RD1_off_no_detail_views(client, db, sent):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    await _watch(client, h, vid, recomm_id="abc")
    assert [type(r).__name__ for r in sent] == ["SetViewPortion"]


async def test_RD2_on_detail_view_with_recomm_id_once(client, db, sent):
    await db.config.insert_one({"features": {"recombee_detail": True}})
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    uid, h = await make_user(db)
    await _watch(client, h, vid, recomm_id="rec-1", time_spent=12.6)
    await _watch(client, h, vid, recomm_id="rec-1")          # again soon after: not a new view
    d = [r for r in sent if isinstance(r, AddDetailView)]
    assert len(d) == 1
    assert d[0].user_id == uid and d[0].item_id == vid and d[0].recomm_id == "rec-1" and d[0].duration == 12
    assert len([r for r in sent if isinstance(r, SetViewPortion)]) == 2


class Fake:
    def __init__(self, rows):
        self.rows, self.sent = rows, []

    def send(self, req):
        self.sent.append(req)
        kind = type(req).__name__
        if kind.startswith("List"):
            if req.user_id not in self.rows:
                raise RuntimeError("status: 404, description: user not found")
            return self.rows[req.user_id].get(kind, [])
        return {}


async def test_RD3_clear_staff_both_databases_only_staff(db, monkeypatch):
    from app.services import recombee_service as rs
    staff, _ = await make_user(db, user_type="admin")
    mod, _ = await make_user(db, staff_role="moderator")
    normal, _ = await make_user(db)
    rows = {
        staff: {"ListUserDetailViews": [{"itemId": "v1", "timestamp": 1}, {"itemId": "v1", "timestamp": 2},
                                        {"itemId": "v2", "timestamp": 3}],
                "ListUserViewPortions": [{"itemId": "v1", "sessionId": "s1"}, {"itemId": "v1", "sessionId": None}],
                "ListUserRatings": [{"itemId": "v3", "rating": 1}]},
        mod: {"ListUserDetailViews": [{"itemId": "v9"}]},
        normal: {"ListUserDetailViews": [{"itemId": "v5"}]},
    }
    us, eu = Fake(rows), Fake({staff: rows[staff]})            # mod isn't in the EU database
    monkeypatch.setattr(rs, "recombee_client", us)
    monkeypatch.setattr(rs, "new_client", eu)
    out = await recombee_cleanup.start(db)
    assert out["started"] is True
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not recombee_cleanup.running():
            break
    st = await recombee_cleanup.status(db)
    assert st["accounts"] == 2 and st["done_accounts"] == 2 and st["errors"] == 0 and st["finished_at"]
    assert st["deleted"]["us:detail_views"] == 3 and st["deleted"]["eu:detail_views"] == 2
    assert st["deleted"]["us:view_portions"] == 2 and st["deleted"]["us:ratings"] == 1
    batches = [r for r in us.sent if isinstance(r, Batch)]
    deletes = [d for b in batches for d in b.requests]
    assert all(d.user_id != normal for d in deletes)
    assert {(d.item_id, getattr(d, "session_id", None)) for d in deletes if isinstance(d, DeleteViewPortion)} \
        == {("v1", "s1"), ("v1", recombee_cleanup.DeleteViewPortion("a", "b").session_id)}
    assert sum(isinstance(d, DeleteDetailView) for d in deletes) == 3
    assert sum(isinstance(d, DeleteRating) for d in deletes) == 1


async def test_RD4_admin_only(client, db, monkeypatch):
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/clear-staff", headers=h)).status_code in (401, 403)
    _, ah = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/recombee/clear-staff", headers=ah)
    assert r.status_code == 200 and r.json()["running"] is False
