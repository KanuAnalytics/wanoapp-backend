"""reserved_names: no one but Wano can look like Wano, disguises included;
real names that contain "wano" are never blocked. And the Wano Team badge
is set only by owners on the Admin page."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from app.core.security import create_access_token
from app.services.reserved_names import is_reserved
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

BLOCKED = ["Wano Support", "wano_support", "WanoSupport", "W4no Supp0rt", "Wаno Team", "Wano Team ✔", "✅ Mary",
           "Official Wano", "wano.africa.help", "W a n o  T e a m", "Wano​Team", "Wano", "Wano Africa Support",
           "wanoafrica", "TEAM WANO", "Wano Admin", "wano-help", "Wano Safety", "Wanο Care"]
FINE = ["Wanojiru", "Mwano Help", "Wanono", "wano lover", "Team Ama", "Amara", "Kampala Support", "Wanoa",
        "Wano's biggest fan", "Juma Wanyama", "Twano"]


@pytest.mark.parametrize("name", BLOCKED)
async def test_RN1_disguised_official_names_are_caught(name):
    assert is_reserved(name), name


@pytest.mark.parametrize("name", FINE)
async def test_RN2_real_names_pass(name):
    assert not is_reserved(name), name


async def test_RN3_signup_and_rename_refused_only_with_the_switch(client, db):
    _, h = await make_user(db)
    r = await client.patch("/api/v1/users/me", json={"display_name": "Wano Support"}, headers=h)
    assert r.status_code == 200, r.text                      # switch off: as before
    await db.config.insert_one({"features": {"reserved_names": True}})
    r = await client.patch("/api/v1/users/me", json={"display_name": "Wano Team ✔"}, headers=h)
    assert r.status_code == 400 and "official" in r.text
    r = await client.patch("/api/v1/users/me", json={"display_name": "Wanojiru"}, headers=h)
    assert r.status_code == 200
    aid, ah = await make_user(db, user_type="admin")
    r = await client.patch("/api/v1/users/me", json={"display_name": "Wano Team"}, headers=ah)
    assert r.status_code == 200                              # staff are exempt


async def test_RN4_badge_only_from_an_owner_on_the_admin_page(client, db):
    await db.config.insert_one({"features": {"safe_ban": True, "comment_guard": True}})
    aid, app_h = await make_user(db, user_type="admin")
    exp = int((datetime.now(timezone.utc) + timedelta(hours=8)).timestamp())
    web = {"Authorization": f"Bearer {create_access_token({'sub': aid, 'exp': exp, 'scope': 'admin_web'})}"}
    team, th = await make_user(db, display_name="Wano Team")
    assert (await client.post("/api/v1/mod/official", json={"user_id": team, "on": True}, headers=app_h)).status_code == 401
    assert (await client.post("/api/v1/mod/official", json={"user_id": team, "on": True}, headers=web)).status_code == 200
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    await client.post("/api/v1/", json={"content": "Welcome!", "video_id": vid}, headers=th)
    rows = (await client.get(f"/api/v1/video/{vid}", headers=th)).json()
    assert rows[0]["is_official"] is True
    # A user can't set it on themselves.
    _, uh = await make_user(db)
    r = await client.patch("/api/v1/users/me", json={"official": True}, headers=uh)
    assert r.status_code in (400, 422)
    me = (await db.users.find_one({"_id": ObjectId(team)}))
    assert me["official"] is True
