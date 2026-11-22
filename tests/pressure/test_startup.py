"""The server starts and stops cleanly: the real lifespan (indexes, metrics
buffer, TV release worker, scheduled publisher, Recombee sync, upload rescue,
follower alerts, small images) on the in-memory database. Guards package
upgrades (FastAPI/Starlette), which the request tests alone don't exercise."""

import asyncio

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_ST1_lifespan_starts_serves_and_stops(db, monkeypatch):
    from app import main
    from app.core import database as database_mod

    async def fake_connect():
        await database_mod.create_indexes()
        await database_mod.create_comment_indexes(database_mod.db.db)

    async def fake_close():
        return None
    monkeypatch.setattr(main, "connect_to_mongo", fake_connect)
    monkeypatch.setattr(main, "close_mongo_connection", fake_close)
    errors = []
    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    loop.set_exception_handler(lambda l, ctx: errors.append(ctx.get("message")))
    try:
        async with main.lifespan(main.app):
            await asyncio.sleep(1.5)                     # the loops take a first turn
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                assert (await c.get("/health")).json() == {"status": "healthy"}
                r = await c.get("/api/v1/config/features")
                assert r.status_code == 200 and r.json()["wa_share"] is False
                r = await c.options("/api/v1/config/features", headers={
                    "Origin": "https://wanoafrica.com", "Access-Control-Request-Method": "GET"})
                assert r.status_code in (200, 400)       # CORS layer answers, doesn't crash
    finally:
        loop.set_exception_handler(old)
    assert errors == []
