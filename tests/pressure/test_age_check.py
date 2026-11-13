"""Wrong birthdays from the old sign-up screen (kellKell: 7 Jul 2016) locked
adults out of messages. Attacks: impossible dates count as unknown everywhere;
the fix works once, only for people flagged young, only to a real 13-110 age;
switch off means nothing; real under-18s stay protected until they change it."""

from datetime import date, datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
A = "/api/v1/age"


def born(years: float) -> datetime:
    return datetime.utcnow() - timedelta(days=int(365.25 * years))


async def _on(db, *keys):
    await db.config.insert_one({"features": {k: True for k in ("age_check", "messages") + keys}})


def test_AG1_impossible_birthdays_are_unknown():
    from app.services.home_ground import is_minor
    assert is_minor({"date_of_birth": born(10.3)}) is True          # could be a real child: stays protected
    assert is_minor({"date_of_birth": born(16)}) is True
    assert is_minor({"date_of_birth": born(1)}) is False            # "born" last year: a default date
    assert is_minor({"date_of_birth": born(7.5)}) is False
    assert is_minor({"date_of_birth": datetime.utcnow()}) is False
    assert is_minor({"date_of_birth": born(30)}) is False and is_minor({}) is False


async def test_AG2_off_by_default(client, db):
    _, h = await make_user(db, date_of_birth=born(10))
    assert (await client.get(f"{A}/check", headers=h)).status_code == 404
    assert (await client.post(f"{A}/birthday", json={"birthday": "1990-01-01"}, headers=h)).status_code == 404
    assert (await client.post(f"{A}/confirm", headers=h)).status_code == 404


async def test_AG3_flagged_young_fixes_once_then_can_message(client, db):
    await _on(db)
    kid, kh = await make_user(db, date_of_birth=datetime(2016, 7, 7))
    friend, _ = await make_user(db)
    s = (await client.get(f"{A}/check", headers=kh)).json()
    assert s["needs_check"] and s["can_fix"] and s["treated_as_minor"] and s["birthday"] == "2016-07-07"
    r = await client.post("/api/v1/messages/conversations", json={"user_id": friend}, headers=kh)
    assert r.status_code == 403 and "18" in r.json()["detail"]
    r = await client.post(f"{A}/birthday", json={"birthday": "1988-03-02"}, headers=kh)
    assert r.status_code == 200, r.text
    assert r.json() == {"birthday": "1988-03-02", "age": r.json()["age"], "treated_as_minor": False,
                        "needs_check": False, "can_fix": False}
    assert (await client.post("/api/v1/messages/conversations", json={"user_id": friend}, headers=kh)).status_code in (200, 201)
    # Once only.
    r = await client.post(f"{A}/birthday", json={"birthday": "2015-01-01"}, headers=kh)
    assert r.status_code == 409
    doc = await db.users.find_one({"_id": ObjectId(kid)})
    assert doc["date_of_birth"] == datetime(1988, 3, 2)
    assert doc["dob_history"][0]["old"] == datetime(2016, 7, 7)


async def test_AG4_bad_dates_refused_and_adults_cant_use_it(client, db):
    await _on(db)
    _, kh = await make_user(db, date_of_birth=datetime(2016, 7, 7))
    today = date.today()
    for bad in ("", "nope", "2016-02-30", "07/07/1990", f"{today.year - 12}-01-01",
                f"{today.year + 1}-01-01", "1800-01-01", "1990-01-01T00:00:00"):
        r = await client.post(f"{A}/birthday", json={"birthday": bad}, headers=kh)
        assert r.status_code == 400, (bad, r.text)
    for bad in ({"birthday": 19900101}, {}, {"birthday": "1990-01-01", "x": 1}):
        assert (await client.post(f"{A}/birthday", json=bad, headers=kh)).status_code == 422
    # Still fixable after all those.
    assert (await client.get(f"{A}/check", headers=kh)).json()["can_fix"] is True
    # Adults and people with no birthday: nothing to fix, never nudged.
    for extra in ({}, {"date_of_birth": born(30)}, {"date_of_birth": None}):
        _, h = await make_user(db, **extra)
        s = (await client.get(f"{A}/check", headers=h)).json()
        assert s["needs_check"] is False and s["can_fix"] is False
        assert (await client.post(f"{A}/birthday", json={"birthday": "1990-01-01"}, headers=h)).status_code == 409
    # Logged out: nothing.
    assert (await client.get(f"{A}/check")).status_code == 401


async def test_AG5_its_right_stops_the_nudge_but_keeps_the_one_fix(client, db):
    await _on(db)
    _, h = await make_user(db, date_of_birth=born(16))
    s = (await client.post(f"{A}/confirm", headers=h)).json()
    assert s["needs_check"] is False and s["can_fix"] is True and s["treated_as_minor"] is True
    # Confirming again changes nothing.
    assert (await client.post(f"{A}/confirm", headers=h)).json() == s
    assert (await client.post(f"{A}/birthday", json={"birthday": "2000-05-05"}, headers=h)).json()["treated_as_minor"] is False


async def test_AG6_impossible_date_already_unlocked_but_still_nudged(client, db):
    await _on(db)
    _, h = await make_user(db, date_of_birth=born(1))
    friend, _ = await make_user(db)
    s = (await client.get(f"{A}/check", headers=h)).json()
    assert s["treated_as_minor"] is False and s["needs_check"] is True
    assert (await client.post("/api/v1/messages/conversations", json={"user_id": friend}, headers=h)).status_code in (200, 201)


async def test_AG7_two_fixes_at_once_only_one_counts(client, db):
    from app.services import age_check
    await _on(db)
    uid, _ = await make_user(db, date_of_birth=datetime(2016, 7, 7))
    await age_check.fix(db, uid, "1990-01-01")
    with pytest.raises(Exception):
        await age_check.fix(db, uid, "1970-01-01")
    assert (await db.users.find_one({"_id": ObjectId(uid)}))["date_of_birth"] == datetime(1990, 1, 1)
