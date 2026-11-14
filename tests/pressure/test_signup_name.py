"""Sign-up with a blank display name: refused with a message naming the box
(switch off, the default), or given the username (switch "signup_names" on).
Names people actually use are never refused."""

import pytest

pytestmark = pytest.mark.asyncio

R = "/api/v1/auth/register"


@pytest.fixture
def verified(client, monkeypatch):
    from app.api.v1 import auth

    async def fake_send(to_email, username=None):
        return True, "111222"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)

    async def go(email):
        await client.post("/api/v1/auth/request-registration-otp", json={"email": email})
        r = await client.post("/api/v1/auth/verify-registration-otp", json={"email": email, "otp": "111222"})
        assert r.status_code == 200, r.text
    return go


def body(username, email, **kw):
    return {"username": username, "email": email, "password": "Password1",
            "localization": {"country": "cm"}, **kw}


async def _switch(db, on):
    await db.config.insert_one({"appVersionNumber": "2.1.1", "features": {"signup_names": on}})


@pytest.mark.parametrize("name", ["", None])
async def test_SN1_off_blank_refused_naming_the_box(client, db, verified, name):
    await verified("a@example.com")
    kw = {} if name is None else {"display_name": name}
    r = await client.post(R, json=body("judith_a", "a@example.com", **kw))
    assert r.status_code == 422
    assert "display name" in r.json()["detail"][0]["msg"].lower()
    assert await db.users.count_documents({}) == 0


async def test_SN2_off_by_default_with_no_config(client, db, verified):
    await verified("b@example.com")
    r = await client.post(R, json=body("raymond_b", "b@example.com", display_name=""))
    assert r.status_code == 422


async def test_SN3_on_blank_becomes_username(client, db, verified):
    await _switch(db, True)
    for i, name in enumerate(("", None, "   ")):
        email = f"c{i}@example.com"
        await verified(email)
        kw = {} if name is None else {"display_name": name}
        r = await client.post(R, json=body(f"nkanda_rita{i}", email, **kw))
        assert r.status_code == 200, r.text
        u = await db.users.find_one({"username": f"nkanda_rita{i}"})
        assert u["display_name"] == f"nkanda_rita{i}"


async def test_SN4_switch_must_be_exactly_true(client, db, verified):
    await db.config.insert_one({"features": {"signup_names": "yes"}})
    await verified("d@example.com")
    assert (await client.post(R, json=body("judith_d", "d@example.com", display_name=""))).status_code == 422


@pytest.mark.parametrize("on", [False, True])
async def test_SN5_real_names_kept_as_typed(client, db, verified, on):
    await _switch(db, on)
    for i, name in enumerate(("Judith", "Raymond", "Nkanda Rita", "Ngo Biyong Élise", "Rita 🌸")):
        email = f"e{i}{on}@example.com"
        await verified(email)
        r = await client.post(R, json=body(f"user_{i}_{int(on)}", email, display_name=name))
        assert r.status_code == 200, (name, r.text)
        assert (await db.users.find_one({"username": f"user_{i}_{int(on)}"}))["display_name"] == name


async def test_SN6_too_long_still_refused(client, db, verified):
    await _switch(db, True)
    await verified("f@example.com")
    assert (await client.post(R, json=body("longname", "f@example.com", display_name="x" * 101))).status_code == 422


async def test_SN7_bad_words_still_refused_even_with_fallback(client, db, verified):
    await _switch(db, True)
    await verified("g@example.com")
    r = await client.post(R, json=body("okname", "g@example.com", display_name="porn star"))
    assert r.status_code == 400 and "Display name" in r.json()["detail"]


INSIDE_WORDS = ("Mbappé Essex", "Dickson", "Hancock", "Lucumba", "Draper", "Sussex Nude-Nkoa"[:6], "Cumbe")


async def test_SN8_off_names_with_short_words_inside_still_refused_as_today(client, db, verified):
    await _switch(db, False)
    await verified("h@example.com")
    r = await client.post(R, json=body("essexgirl", "h@example.com", display_name="Mbappé Essex"))
    assert r.status_code == 400


async def test_SN9_on_names_with_short_words_inside_allowed(client, db, verified):
    await _switch(db, True)
    for i, name in enumerate(INSIDE_WORDS):
        email = f"i{i}@example.com"
        await verified(email)
        r = await client.post(R, json=body(f"inside_{i}", email, display_name=name))
        assert r.status_code == 200, (name, r.text)


@pytest.mark.parametrize("name", ["sex", "Big Dick", "cum.queen", "free_sex", "SEXY Lady", "nude2024",
                                  "pornhub", "fuckboy", "Bitches", "mr pussycat", "rape-me"])
async def test_SN10_on_rude_names_still_refused(client, db, verified, name):
    await _switch(db, True)
    await verified("j@example.com")
    r = await client.post(R, json=body("cleanname", "j@example.com", display_name=name))
    assert r.status_code == 400, name


async def test_SN11_usernames_checked_as_before(client, db, verified):
    await _switch(db, True)
    await verified("k@example.com")
    r = await client.post(R, json=body("essex_k", "k@example.com", display_name="Essex"))
    assert r.status_code == 400 and "Username" in r.json()["detail"]
