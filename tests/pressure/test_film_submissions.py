"""Wano TV film submissions saved on the server (Nov 2026)."""

from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.asyncio

POSTER = "data:image/jpeg;base64," + "A" * 400


def form(**kw):
    f = dict(full_name="Ama Mensah", email="Ama@Example.com", country="Ghana", film_title="Harmattan",
             country_represented="Ghana", year_completed="2025", runtime="14 min", category="Short film",
             languages="Twi", subtitles="English", synopsis="A story.", viewing_link="https://vimeo.com/1",
             rights_confirmed=True, poster_data_url=POSTER, poster_file_name="p.jpg", who="w1")
    f.update(kw)
    return f


async def test_FS1_saved_listed_and_opened(client, db):
    from tests.pressure.conftest import make_user
    r = await client.post("/api/v1/film-submissions", json=form())
    assert r.status_code == 200, r.text
    ref = r.json()["ref"]
    assert r.json()["created"] is True and ref.startswith("WTV-")
    _, ha = await make_user(db, user_type="admin")
    lst = (await client.get("/api/v1/admin/film-submissions", headers=ha)).json()
    assert lst["counts"]["new"] == 1 and lst["rows"][0]["ref"] == ref
    assert "poster" not in lst["rows"][0] and lst["rows"][0]["has_poster"] is True
    one = (await client.get(f"/api/v1/admin/film-submissions/{ref}", headers=ha)).json()
    assert one["poster"] == POSTER and one["email"] == "ama@example.com"


async def test_FS2_double_tap_returns_first(client, db):
    a = (await client.post("/api/v1/film-submissions", json=form())).json()
    b = (await client.post("/api/v1/film-submissions", json=form(email="ama@example.com"))).json()
    assert b == {"ref": a["ref"], "created": False}
    assert await db.film_submissions.count_documents({}) == 1


async def test_FS3_bad_input_refused(client, db):
    for bad in (form(film_title=""), form(email="nope"), form(rights_confirmed=False),
                form(viewing_link="javascript:alert(1)"), form(rights_confirmed="yes")):
        r = await client.post("/api/v1/film-submissions", json=bad)
        assert r.status_code == 400, bad
    assert (await client.post("/api/v1/film-submissions", content=b"[1]", headers={"content-type": "application/json"})).status_code == 400
    # A poster that isn't a small picture is dropped, the film is kept.
    r = await client.post("/api/v1/film-submissions", json=form(poster_data_url="data:text/html;base64,PGI+"))
    assert r.status_code == 200
    assert (await db.film_submissions.find_one({}))["poster"] is None
    r = await client.post("/api/v1/film-submissions", json=form(film_title="Big", poster_data_url="data:image/png;base64," + "A" * 2_000_000))
    assert (await db.film_submissions.find_one({"film_title": "Big"}))["poster"] is None


async def test_FS4_rate_limits(client, db):
    from app.services import film_submissions as fs
    for i in range(fs.PER_SENDER_HOUR):
        assert (await client.post("/api/v1/film-submissions", json=form(film_title=f"F{i}"))).status_code == 200
    assert (await client.post("/api/v1/film-submissions", json=form(film_title="one more"))).status_code == 429
    assert (await client.post("/api/v1/film-submissions", json=form(film_title="other sender", who="w2"))).status_code == 200
    # An hour later the same sender can send again.
    later = datetime.utcnow() + timedelta(hours=1, minutes=1)
    assert (await fs.create(db, form(film_title="later"), "w1", now=later))["created"]


async def test_FS5_status_admin_only_and_logged(client, db):
    from tests.pressure.conftest import make_user
    ref = (await client.post("/api/v1/film-submissions", json=form())).json()["ref"]
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/film-submissions", headers=h)).status_code in (401, 403)
    assert (await client.post(f"/api/v1/admin/film-submissions/{ref}/status", json={"status": "selected"}, headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.post(f"/api/v1/admin/film-submissions/{ref}/status", json={"status": "shortlisted", "note": "Strong"}, headers=ha)
    assert r.status_code == 200 and r.json()["status"] == "shortlisted" and r.json()["note"] == "Strong"
    assert (await client.post(f"/api/v1/admin/film-submissions/{ref}/status", json={"status": "bogus"}, headers=ha)).status_code == 400
    assert (await client.post("/api/v1/admin/film-submissions/WTV-000000/status", json={"status": "new"}, headers=ha)).status_code == 404
    lst = (await client.get("/api/v1/admin/film-submissions?status=shortlisted", headers=ha)).json()
    assert len(lst["rows"]) == 1 and lst["rows"][0]["history"][0]["status"] == "shortlisted"
    assert await db.admin_log.count_documents({"action": "film_submission_status"}) == 1
