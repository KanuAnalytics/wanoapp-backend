"""Recombee: faster calls ("recombee_fast"), better picks ("recombee_tune"),
and a health page that shows whether calls work (Nov 2026).

Measured on the live server, Nov 15 2026: the Recombee step took 600-1050 ms
of every For You page, because Recombee's client opened a new HTTPS
connection for every call; failed calls were swallowed with no trace."""

import asyncio

import pytest
import requests
from recombee_api_client.api_requests import ListScenarios, RecommendItemsToUser, SetViewPortion

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import FakeRecombee, _catalog, _feed, rb  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _switch(db, **on):
    from app.services import recombee_service
    await db.config.insert_one({"features": {k: v for k, v in on.items()}})
    recombee_service.forget()


async def test_RF1_switch_off_uses_a_new_connection_per_call_as_before(db):
    from app.services import recombee_service as rs
    assert rs.fast_on() is False
    assert rs.transport._via() is requests


async def test_RF1_switch_on_reuses_connections(db):
    from app.services import recombee_service as rs
    from recombee_api_client import api_client
    await _switch(db, recombee_fast=True)
    assert await rs.refresh_switch() is True
    assert rs.transport._via() is rs.transport.session
    # Recombee's client looks `requests` up in its own module: it's ours.
    assert api_client.requests is rs.transport
    assert rs.transport.exceptions is requests.exceptions


async def test_RF2_fewer_picks_asked_for_when_fast(client, db, rb):
    _, ids = await _catalog(db, n=40)
    rb.pool = ids
    _, h = await make_user(db)
    await _feed(client, h, limit=20)
    assert rb.of(RecommendItemsToUser)[-1].count == 100
    await _switch(db, recombee_fast=True)
    _, h2 = await make_user(db)
    r = await _feed(client, h2, limit=20)
    assert r.status_code == 200 and len(r.json()) == 20
    assert rb.of(RecommendItemsToUser)[-1].count == 60


async def test_RF3_tune_lowers_rotation(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    _, h = await make_user(db)
    await _feed(client, h)
    assert rb.of(RecommendItemsToUser)[-1].rotation_rate == 0.5
    await _switch(db, recombee_tune=True)
    _, h2 = await make_user(db)
    await _feed(client, h2)
    assert rb.of(RecommendItemsToUser)[-1].rotation_rate == 0.1


async def test_RF4_view_signal_goes_in_background_when_fast(client, db, rb, monkeypatch):
    _, ids = await _catalog(db, n=3)
    _, h = await make_user(db)
    await _switch(db, recombee_fast=True)
    gate = asyncio.Event()
    real = rb.send

    async def slow(req):
        if isinstance(req, SetViewPortion):
            await gate.wait()
        return await real(req)

    from app.services import recombee_service
    monkeypatch.setattr(recombee_service, "recombee_send", slow)
    if True:
        r = await asyncio.wait_for(
            client.post(f"/api/v1/videos/{ids[0]}/watch", headers=h, json={"portion": 0.8}), 5)
        assert r.status_code == 200          # answered while Recombee was still busy
        assert not rb.of(SetViewPortion)
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if rb.of(SetViewPortion):
                break
        assert rb.of(SetViewPortion)[0].portion == 0.8


async def test_RF4_view_signal_still_awaited_when_off(client, db, rb):
    _, ids = await _catalog(db, n=3)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/videos/{ids[0]}/watch", headers=h, json={"portion": 0.5})
    assert r.status_code == 200
    assert rb.of(SetViewPortion)[0].portion == 0.5


async def test_RF5_calls_and_failures_are_counted(db, monkeypatch):
    from app.services import recombee_service as rs
    from recombee_api_client.exceptions import ResponseException
    seen = []

    def fake_send(req):
        seen.append(req)
        if len(seen) == 2:
            raise ResponseException(req, 404, "Item not found")
        return []

    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    monkeypatch.setattr(rs.recombee_client, "send", fake_send)
    await rs.recombee_send(ListScenarios())
    with pytest.raises(ResponseException):
        await rs.recombee_send(ListScenarios())
    await rs.recombee_signal(ListScenarios())          # never raises
    s = rs.stats()
    assert s["calls"]["ListScenarios"]["ok"] == 2
    assert s["calls"]["ListScenarios"]["failed"] == 1
    assert s["last_errors"][0]["status"] == 404
    assert "Item not found" in s["last_errors"][0]["error"]


async def test_RF6_health_is_admin_only_and_reports(client, db, monkeypatch):
    from app.services import recombee_service as rs
    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    monkeypatch.setattr(rs.recombee_client, "send", lambda req: [])
    _, h = await make_user(db)
    assert (await client.get("/api/v1/recombee/health", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/recombee/health", headers=ha)
    assert r.status_code == 200
    body = r.json()
    assert len(body["pings_ms"]) == 3 and all(isinstance(x, int) for x in body["pings_ms"])
    assert body["calls"]["ListScenarios"]["ok"] == 3
    assert body["fast"] is False


async def test_RF7_switches_are_listed(client, db):
    r = await client.get("/api/v1/config/features")
    assert r.json()["recombee_fast"] is False and r.json()["recombee_tune"] is False
