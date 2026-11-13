"""Share links for profiles, Wano TV and the plain app page, plus links older
apps already sent (share.wanoafrica.com/pages/feed?videoId=...).

Attacks: hidden / banned / deleted profiles, under-18s on the open web, HTML
injection through names and titles, open redirects from the old links."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.core.config import settings
from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio
X = '"><script>alert(1)</script>'


async def test_profile_page_shows_name_photo_and_open_in_wano(client, db):
    uid, _ = await make_user(db, display_name="Ama Creator", username="ama",
                             profile_picture="https://cdn.example.com/a.jpg")
    r = await client.get(f"/u/{uid}")
    assert r.status_code == 200
    assert "Ama Creator" in r.text and "@ama" in r.text and "https://cdn.example.com/a.jpg" in r.text
    assert f'href="wano://u/{uid}"' in r.text
    assert "noindex" in r.text and r.headers["x-robots-tag"].startswith("noindex")
    assert "apps.apple.com" in r.text and "play.google.com" in r.text
    assert "<script" not in r.text


async def test_profile_never_leaks_private_fields(client, db):
    uid, _ = await make_user(db, display_name="Ama", email="secret-mail@example.com", phone_number="+256700111222")
    b = (await client.get(f"/u/{uid}")).text
    assert "secret-mail" not in b and "256700111222" not in b


async def test_under_18_profile_has_no_name_or_photo(client, db):
    uid, _ = await make_user(db, display_name="Young Name", username="younghandle",
                             profile_picture="https://cdn.example.com/kid.jpg",
                             date_of_birth=datetime.utcnow() - timedelta(days=365 * 15))
    r = await client.get(f"/u/{uid}")
    assert r.status_code == 200
    for leak in ("Young Name", "younghandle", "kid.jpg"):
        assert leak not in r.text
    assert f'href="wano://u/{uid}"' in r.text


@pytest.mark.parametrize("extra", [{"is_active": False}, {"is_banned": True}, {"deleted_at": datetime.utcnow()}])
async def test_hidden_profiles_look_like_unknown(client, db, extra):
    uid, _ = await make_user(db, display_name="HIDDENNAME", **extra)
    r = await client.get(f"/u/{uid}")
    assert r.status_code == 404 and "HIDDENNAME" not in r.text
    assert "This profile isn't available" in r.text


@pytest.mark.parametrize("bad", ["x", "../../etc/passwd", "0" * 25, X])
async def test_garbage_ids(client, db, bad):
    for base in ("/u/", "/tv/"):
        r = await client.get(base + bad)
        assert r.status_code == 404 and "<script>alert" not in r.text


async def test_names_cannot_inject_html(client, db):
    uid, _ = await make_user(db, display_name=X, username=X, profile_picture="javascript:alert(1)")
    b = (await client.get(f"/u/{uid}")).text
    assert "<script>alert" not in b and "javascript:" not in b


async def test_tv_page(client, db):
    tid = (await db.tv_titles.insert_one({"title": "Kampala Nights", "description": "A city story",
                                          "poster": "https://cdn.example.com/p.jpg", "is_active": True})).inserted_id
    r = await client.get(f"/tv/{tid}")
    assert r.status_code == 200 and "Kampala Nights" in r.text and "https://cdn.example.com/p.jpg" in r.text
    assert f'href="wano://tv/{tid}"' in r.text and 'property="og:title"' in r.text
    off = (await db.tv_titles.insert_one({"title": "SECRETTV", "is_active": False})).inserted_id
    r = await client.get(f"/tv/{off}")
    assert r.status_code == 404 and "SECRETTV" not in r.text
    inj = (await db.tv_titles.insert_one({"title": X, "description": X, "poster": "http://x/p.jpg",
                                          "is_active": True})).inserted_id
    b = (await client.get(f"/tv/{inj}")).text
    assert "<script>alert" not in b and "http://x/p.jpg" not in b


async def test_app_page(client, db):
    r = await client.get("/app")
    assert r.status_code == 200 and "apps.apple.com" in r.text and 'href="wano://"' in r.text


async def test_old_links_go_to_new_pages(client, db):
    v, u, t = ObjectId(), ObjectId(), ObjectId()
    cases = {
        f"/pages/feed?videoId={v}&single=true": f"/v/{v}",
        f"/pages/feed?userId={u}": f"/u/{u}",
        f"/pages/v2/wano-tv/details?id={t}": f"/tv/{t}",
        "/pages/feed": "/app",
        "/pages/feed?videoId=https://evil.example.com": "/app",
        "/pages/feed?videoId=//evil.example.com": "/app",
        "/pages/v2/wano-tv/details?id=@evil.com": "/app",
    }
    for src, to in cases.items():
        r = await client.get(src, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == to, src


async def test_app_link_files_cover_new_paths(client, db, monkeypatch):
    monkeypatch.setattr(settings, "IOS_APP_IDS", "8BY222TD3Q.com.wano.app")
    d = (await client.get("/.well-known/apple-app-site-association")).json()["applinks"]["details"]
    paths = d[1]["paths"]
    for p in ("/v/*", "/u/*", "/tv/*", "/pages/feed"):
        assert p in paths
    assert "/app" not in paths  # the plain page stays a web page


async def test_defaults_point_at_wano(client, db):
    assert settings.SHARE_BASE_URL == "https://devbe.wanoafrica.com"
    assert settings.ANDROID_PACKAGE == "com.wano.app"
