"""The "recombee_eu" switch (Nov 2026): For You asks the new EU Recombee
database instead of the US one. Off = exactly as before. On = the EU database
answers, every write is copied to the US one (so turning it off loses
nothing), and a read the EU database can't answer is asked of the US one."""

import asyncio

import pytest
from recombee_api_client.api_requests import AddDetailView, Batch, RecommendItemsToUser

pytestmark = pytest.mark.asyncio


class Fake:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.sent = name, fail, []

    def send(self, request):
        self.sent.append(request)
        if self.fail:
            raise RuntimeError(f"{self.name} down")
        return {"from": self.name}


@pytest.fixture
def two(monkeypatch):
    from app.services import recombee_service as rs
    us, eu = Fake("us"), Fake("eu")
    monkeypatch.setattr(rs, "recombee_client", us)
    monkeypatch.setattr(rs, "new_client", eu)
    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    return rs, us, eu


async def _switch(db, **on):
    from app.services import recombee_service
    await db.config.delete_many({"features": {"$exists": True}})
    await db.config.insert_one({"features": on})
    recombee_service.forget()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def test_EU1_off_reads_from_us_and_copies_writes_to_eu(db, two):
    rs, us, eu = two
    assert (await rs.recombee_send(RecommendItemsToUser("u1", 5)))["from"] == "us"
    await rs.recombee_send(AddDetailView("u1", "v1"))
    await _settle()
    assert [type(r).__name__ for r in eu.sent] == ["AddDetailView"]   # reads never copied
    assert rs.stats()["answering"] == "us-west"


async def test_EU2_on_reads_from_eu_and_copies_writes_to_us(db, two):
    rs, us, eu = two
    await _switch(db, recombee_eu=True)
    assert (await rs.recombee_send(RecommendItemsToUser("u1", 5)))["from"] == "eu"
    await rs.recombee_send(Batch([AddDetailView("u1", "v1")]))
    await _settle()
    assert [type(r).__name__ for r in us.sent] == ["Batch"]
    assert not any(isinstance(r, RecommendItemsToUser) for r in us.sent)
    assert rs.stats()["answering"] == "eu-west"


async def test_EU3_on_a_failed_eu_read_is_asked_of_us(db, two):
    rs, us, eu = two
    eu.fail = True
    await _switch(db, recombee_eu=True)
    assert (await rs.recombee_send(RecommendItemsToUser("u1", 5)))["from"] == "us"
    assert rs.stats()["calls"]["fallback:RecommendItemsToUser"]["ok"] == 1


async def test_EU4_on_a_failed_eu_write_is_not_retried_on_us(db, two):
    rs, us, eu = two
    eu.fail = True
    await _switch(db, recombee_eu=True)
    with pytest.raises(RuntimeError):
        await rs.recombee_send(AddDetailView("u1", "v1"))
    await _settle()
    assert us.sent == []


async def test_EU5_switch_does_nothing_without_a_new_database(db, monkeypatch):
    from app.services import recombee_service as rs
    us = Fake("us")
    monkeypatch.setattr(rs, "recombee_client", us)
    monkeypatch.setattr(rs, "new_client", None)
    monkeypatch.setattr(rs, "recombee_send", rs._real_send)
    await _switch(db, recombee_eu=True)
    assert (await rs.recombee_send(RecommendItemsToUser("u1", 5)))["from"] == "us"
    assert rs.eu_on() is False


async def test_EU6_listed_and_off_by_default(client):
    r = await client.get("/api/v1/config/features")
    assert r.status_code == 200 and r.json()["recombee_eu"] is False
