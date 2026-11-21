"""Phone (WhatsApp) sign-up budget and sign-in with a code. Both behind
switches that are off by default. Attacks first."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import get_password_hash
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

OTP = "/api/v1/auth/request-registration-otp"
LC = "/api/v1/auth/login-code"
LCV = "/api/v1/auth/login-code/verify"


async def _on(db, **sw):
    await db.config.insert_one({"appVersionNumber": "2.1.1", "features": sw})


@pytest.fixture
def wa(monkeypatch):
    from app.api.v1 import auth
    sent = []

    async def fake(to, content_variables):
        sent.append((to, content_variables["1"]))
        return {"sid": "x"}
    monkeypatch.setattr(auth.twilio_whatsapp_service, "send_template_message", fake)
    return sent


@pytest.fixture
def mail(monkeypatch):
    from app.api.v1 import auth
    sent = []

    async def fake(to_email, username=None, otp=None, expiry_minutes=10):
        code = "{:06d}".format(len(sent) * 7919 % 1000000)
        sent.append((to_email, code))
        return True, code
    monkeypatch.setattr(auth.email_service, "send_login_code", fake)
    return sent


def num(i):
    return f"+2376{i:08d}"


# --- phone sign-up budget -------------------------------------------------

async def test_P1_off_phone_codes_work_as_today_no_budget(client, db, wa):
    for i in range(25):
        r = await client.post(OTP, json={"phone_number": num(i)}, headers={"X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 200, r.text
    assert len(wa) == 25 and await db.otp_budget.count_documents({}) == 0


async def test_P2_on_one_network_capped_per_hour_others_fine(client, db, wa):
    from app.api.v1 import auth
    await _on(db, phone_signup=True)
    codes = [(await client.post(OTP, json={"phone_number": num(i)}, headers={"X-Forwarded-For": "5.6.7.8"})).status_code
             for i in range(auth.WHATSAPP_CODES_PER_IP_HOUR + 3)]
    assert codes.count(200) == auth.WHATSAPP_CODES_PER_IP_HOUR and codes[-1] == 429
    r = await client.post(OTP, json={"phone_number": num(99)}, headers={"X-Forwarded-For": "9.9.9.9"})
    assert r.status_code == 200
    assert len(wa) == auth.WHATSAPP_CODES_PER_IP_HOUR + 1


async def test_P3_on_daily_total_cap_and_message_points_to_email(client, db, wa, monkeypatch):
    from app.api.v1 import auth
    monkeypatch.setattr(auth, "WHATSAPP_CODES_PER_DAY", 5)
    await _on(db, phone_signup=True)
    res = [await client.post(OTP, json={"phone_number": num(i)}, headers={"X-Forwarded-For": f"10.0.0.{i}"})
           for i in range(8)]
    assert [r.status_code for r in res].count(200) == 5
    assert "email" in res[-1].json()["detail"]
    assert len(wa) == 5


async def test_P4_spoofed_forwarded_for_does_not_dodge_the_ip_cap(client, db, wa):
    """Only the rightmost entry (added by our proxy) counts."""
    from app.api.v1 import auth
    await _on(db, phone_signup=True)
    ok = 0
    for i in range(auth.WHATSAPP_CODES_PER_IP_HOUR + 5):
        r = await client.post(OTP, json={"phone_number": num(i)},
                              headers={"X-Forwarded-For": f"66.{i}.0.1, 5.5.5.5"})
        ok += r.status_code == 200
    assert ok == auth.WHATSAPP_CODES_PER_IP_HOUR


async def test_P5_parallel_burst_never_exceeds_cap(client, db, wa, monkeypatch):
    from app.api.v1 import auth
    monkeypatch.setattr(auth, "WHATSAPP_CODES_PER_DAY", 7)
    await _on(db, phone_signup=True)
    rs = await asyncio.gather(*[client.post(OTP, json={"phone_number": num(i)},
                                            headers={"X-Forwarded-For": f"20.0.{i}.1"}) for i in range(30)])
    assert sum(r.status_code == 200 for r in rs) == 7 and len(wa) == 7


async def test_P6_email_signup_untouched_by_phone_budget(client, db, wa, monkeypatch):
    from app.api.v1 import auth
    monkeypatch.setattr(auth, "WHATSAPP_CODES_PER_DAY", 0)

    async def fake_send(to_email, username=None):
        return True, "111222"
    monkeypatch.setattr(auth.email_service, "send_registration_otp", fake_send)
    await _on(db, phone_signup=True)
    assert (await client.post(OTP, json={"email": "e@example.com"})).status_code == 200


async def test_P7_phone_signup_end_to_end(client, db, wa):
    await _on(db, phone_signup=True)
    assert (await client.post(OTP, json={"phone_number": "+237677000001"})).status_code == 200
    code = wa[-1][1]
    r = await client.post("/api/v1/auth/verify-registration-otp", json={"phone_number": "+237677000001", "otp": code})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/auth/register", json={
        "username": "phone_user", "phone_number": "+237677000001", "password": "Password1",
        "display_name": "Phone User", "localization": {"country": "cm"}})
    assert r.status_code == 200, r.text
    u = await db.users.find_one({"username": "phone_user"})
    assert u["phone_number"] == "+237677000001" and not u.get("email")
    # and that number can't sign up twice
    assert (await client.post(OTP, json={"phone_number": "+237677000001"})).status_code == 400


# --- sign in with a code ---------------------------------------------------

async def _person(db, **kw):
    uid, _ = await make_user(db, username=kw.pop("username", "rita"), email=kw.pop("email", "rita@example.com"),
                             password_hash=get_password_hash("Password1"), **kw)
    return uid


async def test_C1_off_both_endpoints_404(client, db, mail):
    await _person(db)
    assert (await client.post(LC, json={"username_or_email": "rita"})).status_code == 404
    assert (await client.post(LCV, json={"username_or_email": "rita", "otp": "123456"})).status_code == 404
    assert not mail


async def test_C2_same_answer_for_real_and_unknown_accounts(client, db, mail):
    await _on(db, code_login=True)
    await _person(db)
    a = await client.post(LC, json={"username_or_email": "rita"})
    b = await client.post(LC, json={"username_or_email": "nobody_here"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert len(mail) == 1 and mail[0][0] == "rita@example.com"


async def test_C3_code_signs_in_once(client, db, mail):
    await _on(db, code_login=True)
    uid = await _person(db)
    await client.post(LC, json={"username_or_email": "Rita@Example.com"})
    code = mail[-1][1]
    r = await client.post(LCV, json={"username_or_email": "rita@example.com", "otp": code})
    assert r.status_code == 200 and r.json()["user_id"] == uid and r.json()["access_token"]
    me = await client.get("/api/v1/users/me/profile", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200
    again = await client.post(LCV, json={"username_or_email": "rita@example.com", "otp": code})
    assert again.status_code == 400


async def test_C4_five_wrong_guesses_kill_the_code(client, db, mail):
    await _on(db, code_login=True)
    await _person(db)
    await client.post(LC, json={"username_or_email": "rita"})
    code = mail[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    st = [(await client.post(LCV, json={"username_or_email": "rita", "otp": wrong})).status_code for _ in range(5)]
    assert st[-1] == 429
    assert (await client.post(LCV, json={"username_or_email": "rita", "otp": code})).status_code in (400, 429)


async def test_C5_expired_code_refused(client, db, mail):
    await _on(db, code_login=True)
    uid = await _person(db)
    await client.post(LC, json={"username_or_email": "rita"})
    await db.users.update_one({"_id": ObjectId(uid)},
                              {"$set": {"login_code_expires": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    assert (await client.post(LCV, json={"username_or_email": "rita", "otp": mail[-1][1]})).status_code == 400


async def test_C6_code_stored_hashed_never_plain(client, db, mail):
    await _on(db, code_login=True)
    uid = await _person(db)
    await client.post(LC, json={"username_or_email": "rita"})
    u = await db.users.find_one({"_id": ObjectId(uid)})
    assert mail[-1][1] not in str(u)


async def test_C7_one_a_minute_and_ten_a_day(client, db, mail):
    await _on(db, code_login=True)
    uid = await _person(db)
    for _ in range(3):
        await client.post(LC, json={"username_or_email": "rita"})
    assert len(mail) == 1
    for _ in range(15):
        await db.users.update_one({"_id": ObjectId(uid)}, {"$unset": {"login_code_requested_at": ""}})
        await client.post(LC, json={"username_or_email": "rita"})
    from app.api.v1 import auth
    assert len(mail) == auth.MAX_OTPS_PER_DAY


async def test_C8_banned_account_gets_nothing(client, db, mail):
    await _on(db, code_login=True)
    await _person(db, is_active=False)
    r = await client.post(LC, json={"username_or_email": "rita"})
    assert r.status_code == 200 and not mail


async def test_C9_code_for_one_account_never_opens_another(client, db, mail):
    await _on(db, code_login=True)
    await _person(db)
    await _person(db, username="judith", email="judith@example.com")
    await client.post(LC, json={"username_or_email": "rita"})
    r = await client.post(LCV, json={"username_or_email": "judith", "otp": mail[-1][1]})
    assert r.status_code == 400


async def test_C10_phone_only_account_gets_whatsapp(client, db, mail, wa):
    await _on(db, code_login=True)
    await make_user(db, username="phoneonly", phone_number="+237677000009", email=None)
    await client.post(LC, json={"username_or_email": "+237677000009"})
    assert wa and wa[-1][0] == "+237677000009" and not mail
    r = await client.post(LCV, json={"username_or_email": "phoneonly", "otp": wa[-1][1]})
    assert r.status_code == 200


async def test_C11_two_at_once_only_one_signs_in(client, db, mail):
    await _on(db, code_login=True)
    await _person(db)
    await client.post(LC, json={"username_or_email": "rita"})
    code = mail[-1][1]
    rs = await asyncio.gather(*[client.post(LCV, json={"username_or_email": "rita", "otp": code}) for _ in range(6)])
    assert sum(r.status_code == 200 for r in rs) == 1


async def test_C12_bad_input_refused(client, db, mail):
    await _on(db, code_login=True)
    for body in ({"username_or_email": "rita", "otp": "12345"}, {"username_or_email": "rita", "otp": "abcdef"},
                 {"username_or_email": {"$ne": 1}, "otp": "123456"}, {"username_or_email": "rita", "otp": {"$ne": 1}}):
        assert (await client.post(LCV, json=body)).status_code == 422, body
    assert (await client.post(LC, json={"username_or_email": {"$gt": ""}})).status_code == 422


async def test_C13_password_sign_in_unchanged(client, db, mail):
    await _on(db, code_login=True)
    uid = await _person(db)
    r = await client.post("/api/v1/auth/login/json", json={"username": "rita", "password": "Password1"})
    assert r.status_code == 200 and r.json()["user_id"] == uid
    assert (await client.post("/api/v1/auth/login/json", json={"username": "rita", "password": "nope"})).status_code == 401


async def test_C14_password_reset_by_whatsapp_shares_the_code_budget(client, db, wa):
    """A script asking for reset codes for every phone account from one network
    stops at the per-network budget; the answer stays the same."""
    for i in range(25):
        await make_user(db, username=f"ph{i}", email=None, phone_number=num(i))
    codes = []
    for i in range(25):
        r = await client.post("/api/v1/auth/forgot-password", json={"username_or_email": num(i)},
                              headers={"X-Forwarded-For": "9.9.9.9"})
        assert r.status_code == 200, r.text
        codes.append(r.json()["message"])
    assert len(wa) == 20 and len(set(codes)) == 1
