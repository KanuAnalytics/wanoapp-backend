import pytest
from fastapi import HTTPException

from app.core.disposable_email import (
    DISPOSABLE_EMAIL_MESSAGE,
    ensure_not_disposable_email,
    is_disposable_email,
)


@pytest.mark.parametrize(
    "email",
    [
        "Prussia9490$@uberip.com",
        "bot@maxxspace.com",
        "someone@yopmail.com",
        "UPPER@UBERIP.COM",           # case-insensitive
        "x@inbox.uberip.com",         # subdomain of a blocked domain
        "x@uberip.com.",              # trailing dot
    ],
)
def test_blocks_disposable_domains(email):
    assert is_disposable_email(email)


@pytest.mark.parametrize(
    "email",
    [
        "user@gmail.com",
        "user@outlook.com",
        "user@yahoo.com",
        "user@hotmail.com",
        "user@icloud.com",
        "team@wanoafrica.com",
        None,
        "",
        "not-an-email",
    ],
)
def test_allows_regular_providers_and_empty_input(email):
    assert not is_disposable_email(email)


def test_ensure_raises_400_with_user_facing_message():
    with pytest.raises(HTTPException) as exc:
        ensure_not_disposable_email("bot@maxxspace.com")
    assert exc.value.status_code == 400
    assert exc.value.detail == DISPOSABLE_EMAIL_MESSAGE
    assert "Gmail" in DISPOSABLE_EMAIL_MESSAGE


def test_ensure_passes_regular_email():
    ensure_not_disposable_email("user@gmail.com")


# The endpoints run the check before any database access, so they can be
# exercised directly without a MongoDB connection.

@pytest.mark.asyncio
async def test_register_rejects_disposable_email():
    from app.api.v1.auth import RegisterRequest, register

    req = RegisterRequest(
        username="Prussia9490",
        email="Prussia9490@uberip.com",
        password="Password1",
        display_name="Nofiat",
        localization={"country": "bw"},
    )
    with pytest.raises(HTTPException) as exc:
        await register(req)
    assert exc.value.status_code == 400
    assert exc.value.detail == DISPOSABLE_EMAIL_MESSAGE


@pytest.mark.asyncio
async def test_registration_otp_request_rejects_disposable_email():
    from app.api.v1.auth import RequestRegistrationOtpRequest, request_registration_otp

    with pytest.raises(HTTPException) as exc:
        await request_registration_otp(
            RequestRegistrationOtpRequest(email="bot@maxxspace.com")
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == DISPOSABLE_EMAIL_MESSAGE


@pytest.mark.asyncio
async def test_users_create_rejects_disposable_email(monkeypatch):
    # POST /users/ is admin-only, so the disposable check is reached only
    # after the admin check passes; fake an admin requester for that lookup.
    from app.api.v1 import users as users_api
    from tests.test_users_create_admin_only import ADMIN_ID, FakeDB

    monkeypatch.setattr(users_api, "get_database", lambda: FakeDB("admin"))
    with pytest.raises(HTTPException) as exc:
        await users_api.create_user(
            users_api.UserCreate(
                username="Griffard1681",
                email="Griffard1681@uberip.com",
                password="Password1",
                display_name="x",
                localization={"country": "bw"},
            ),
            current_user=ADMIN_ID,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == DISPOSABLE_EMAIL_MESSAGE
