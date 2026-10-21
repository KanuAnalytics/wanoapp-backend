"""POST /api/v1/users/ lets the caller pick user_type (including admin), so it
must be admin-only. These tests run without MongoDB: the HTTP test is
rejected by the auth scheme before any database access, and the direct calls
use a fake users collection for the requester lookup."""

import pytest
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1 import users as users_api

ADMIN_ID = str(ObjectId())
STANDARD_ID = str(ObjectId())


class _FakeUsers:
    def __init__(self, requester_type):
        self.requester_type = requester_type
        self.inserted = []

    async def find_one(self, query, projection=None):
        # Requester lookup by _id; any other query (duplicate check) misses.
        if "_id" in query:
            return {"_id": query["_id"], "user_type": self.requester_type}
        return None

    async def insert_one(self, doc):
        self.inserted.append(doc)
        raise AssertionError("create_user must not insert in these tests")


class FakeDB:
    def __init__(self, requester_type):
        self.users = _FakeUsers(requester_type)


def _new_admin_payload():
    return users_api.UserCreate(
        username="sneaky",
        email="sneaky@gmail.com",
        password="Password1",
        display_name="Sneaky",
        user_type="admin",
        localization={"country": "ng"},
    )


def test_anonymous_request_is_rejected():
    app = FastAPI()
    app.include_router(users_api.router, prefix="/api/v1/users")
    client = TestClient(app)

    res = client.post(
        "/api/v1/users/",
        json={
            "username": "sneaky",
            "email": "sneaky@gmail.com",
            "password": "Password1",
            "display_name": "Sneaky",
            "user_type": "admin",
            "localization": {"country": "ng"},
        },
    )
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_standard_user_cannot_create_users(monkeypatch):
    fake = FakeDB("standard")
    monkeypatch.setattr(users_api, "get_database", lambda: fake)

    with pytest.raises(HTTPException) as exc:
        await users_api.create_user(_new_admin_payload(), current_user=STANDARD_ID)

    assert exc.value.status_code == 403
    assert fake.users.inserted == []


@pytest.mark.asyncio
async def test_admin_passes_the_admin_check(monkeypatch):
    # A disposable email stops the request right after the admin check, which
    # proves an admin gets past it without writing anything.
    fake = FakeDB("admin")
    monkeypatch.setattr(users_api, "get_database", lambda: fake)
    payload = _new_admin_payload()
    payload.email = "x@uberip.com"

    with pytest.raises(HTTPException) as exc:
        await users_api.create_user(payload, current_user=ADMIN_ID)

    assert exc.value.status_code == 400
    assert fake.users.inserted == []
