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


# The 2.1.1 store app: sends the username untrimmed, and only learned it was
# refused at the last step, after the code.

OTP = "/api/v1/auth/request-registration-otp"


async def test_SN12_on_bad_username_refused_before_the_code_with_a_tip(client, db, monkeypatch):
    from app.api.v1 import auth
    sent = []

    async def fake_send(to_email, username=None):
        sent.append(to_email)
        return True, "111222"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    await _switch(db, True)
    r = await client.post(OTP, json={"email": "n@example.com", "username": "Nkanda Rita"})
    assert r.status_code == 400 and "Try Nkanda_Rita" in r.json()["detail"] and not sent
    r = await client.post(OTP, json={"email": "n@example.com", "username": "Élise"})
    assert r.status_code == 400 and "Try Elise" in r.json()["detail"]
    r = await client.post(OTP, json={"email": "n@example.com", "username": "ok_name "})
    assert r.status_code == 200 and sent == ["n@example.com"]


async def test_SN13_off_code_step_as_today(client, db, monkeypatch):
    from app.api.v1 import auth

    async def fake_send(to_email, username=None):
        return True, "111222"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    r = await client.post(OTP, json={"email": "o@example.com", "username": "Nkanda Rita"})
    assert r.status_code == 200


async def test_SN14_on_trailing_space_username_accepted_and_saved_trimmed(client, db, verified):
    await _switch(db, True)
    await verified("p@example.com")
    r = await client.post(R, json=body("judith_p ", "p@example.com", display_name="Judith"))
    assert r.status_code == 200, r.text
    assert await db.users.find_one({"username": "judith_p"})
    r = await client.post("/api/v1/auth/login/json", json={"username": "judith_p", "password": "Password1"})
    assert r.status_code == 200


async def test_SN15_off_trailing_space_refused_as_today(client, db, verified):
    await verified("q@example.com")
    r = await client.post(R, json=body("judith_q ", "q@example.com", display_name="Judith"))
    assert r.status_code == 400 and r.json()["detail"].startswith("Username must be 3-30")


async def test_SN16_on_taken_username_still_taken_after_trim(client, db, verified):
    from tests.pressure.conftest import make_user
    await make_user(db, username="takenname")
    await _switch(db, True)
    await verified("t@example.com")
    r = await client.post(R, json=body(" takenname ", "t@example.com", display_name="T"))
    assert r.status_code == 400 and "already" in r.json()["detail"]


async def test_SN17_on_the_whole_211_payload_goes_through(client, db, verified):
    """What 2.1.1 actually sends: its whole form, untrimmed username."""
    await _switch(db, True)
    await verified("r@example.com")
    r = await client.post(R, json={"display_name": "Nkanda Rita", "username": "nkanda_rita ", "email": "r@example.com",
                                   "password": "Password1", "date_of_birth": "1999-05-01T00:00:00.000Z",
                                   "gender": "female", "bio": "", "agreed_to_terms": True, "verification_code": "111222",
                                   "localization": {"country": "cm"}})
    assert r.status_code == 200, r.text
    u = await db.users.find_one({"username": "nkanda_rita"})
    assert u["display_name"] == "Nkanda Rita"
