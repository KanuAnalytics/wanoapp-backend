"""Shared links that bring installs: the first button is the one that works on
the phone that opened the link, store links say they came from a share, and
the pages still run no scripts. Attacks first."""

import html
import re
from urllib.parse import unquote

import pytest

from app.core.config import settings
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
ANDROID = "Mozilla/5.0 (Linux; Android 14; TECNO KJ5) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 Version/18.1 Mobile/15E148"
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
CF = f"https://videodelivery.net/{'d' * 32}/manifest/video.m3u8"


async def _vid(db):
    creator, _ = await make_user(db, display_name="Ama")
    return await make_video(db, creator, remoteUrl_CF=CF, title="Kampala sunset", description="Golden hour")


def _runs_script(body):
    """A script that runs (the JSON-LD data block for search engines doesn't)."""
    return re.search(r'<script(?![^>]*type="application/ld\+json")', body, re.I) is not None


def _buttons(body):
    return [(html.unescape(h), t) for h, t in re.findall(r'<a(?: class="primary")? href="([^"]*)">([^<]*)</a>', body)]


async def test_SI1_android_one_button_opens_app_or_play_store(client, db):
    vid = await _vid(db)
    r = await client.get(f"/v/{vid}", headers={"User-Agent": ANDROID})
    b = _buttons(r.text)
    href, label = b[0]
    assert label == "Watch on Wano · free" and 'class="primary"' in r.text
    assert href.startswith(f"intent://v/{vid}#Intent;scheme=wano;package=com.wano.app;S.browser_fallback_url=")
    fallback = unquote(href.split("S.browser_fallback_url=")[1].split(";end")[0])
    assert fallback.startswith("https://play.google.com/store/apps/details?id=com.wano.app&referrer=")
    assert unquote(fallback.split("referrer=")[1]) == "utm_source=wano_share&utm_medium=video&utm_campaign=share"
    assert len(b) == 1 and not _runs_script(r.text) and "User-Agent" in r.headers["vary"]


async def test_SI2_iphone_app_store_first_then_open(client, db):
    vid = await _vid(db)
    r = await client.get(f"/v/{vid}", headers={"User-Agent": IPHONE})
    b = _buttons(r.text)
    assert b[0] == ("https://apps.apple.com/app/id6753104927?ct=share_video", "Get Wano · free")
    assert b[1] == (f"wano://v/{vid}", "I have Wano: open it")
    assert "apple-itunes-app" in r.text                       # Safari's own banner stays


async def test_SI3_computer_or_unknown_unchanged_but_counted(client, db):
    vid = await _vid(db)
    for ua in (DESKTOP, ""):
        b = _buttons((await client.get(f"/v/{vid}", headers={"User-Agent": ua})).text)
        assert b[0] == (f"wano://v/{vid}", "Open in Wano")
        assert b[1][1] == "App Store" and b[2][1] == "Google Play"
        assert "referrer=utm_source%3Dwano_share" in b[2][0]


async def test_SI4_each_page_says_what_was_shared(client, db):
    uid, _ = await make_user(db, display_name="Ama", username="ama")
    r = await client.get(f"/u/{uid}", headers={"User-Agent": IPHONE})
    assert "ct=share_profile" in r.text
    r = await client.get("/app", headers={"User-Agent": ANDROID})
    assert "utm_medium%253Dapp" in r.text and "intent://#Intent;scheme=wano" in r.text
    r = await client.get("/v/000000000000000000000000", headers={"User-Agent": ANDROID})
    assert r.status_code == 404 and "intent://#Intent" in r.text   # not available: still a way to get Wano


async def test_SI5_hostile_settings_never_reach_the_page(client, db, monkeypatch):
    vid = await _vid(db)
    monkeypatch.setattr(settings, "APP_SCHEME", 'javascript:alert(1)//"><b')
    monkeypatch.setattr(settings, "ANDROID_PLAY_STORE_URL",
                        'https://play.google.com/store/apps/details?id=com.wano.app;S.x="><b')
    for ua in (ANDROID, IPHONE, DESKTOP):
        body = (await client.get(f"/v/{vid}", headers={"User-Agent": ua})).text
        assert "javascript:alert" not in body and '"><b' not in body and not _runs_script(body)


async def test_SI6_hostile_user_agent_is_only_read(client, db):
    vid = await _vid(db)
    r = await client.get(f"/v/{vid}", headers={"User-Agent": 'Android"><script>x</script>' + "a" * 5000})
    assert r.status_code == 200 and "<script>x" not in r.text and "aaaaaaaaaa" not in r.text


async def test_SI7_wano_colours(client, db):
    vid = await _vid(db)
    r = await client.get(f"/v/{vid}", headers={"User-Agent": ANDROID})
    assert "#3AB586" in r.text and "#ff3b30" not in r.text


# ---------------------------------------------------------------- asking for ratings

async def test_SI8_rating_prompt_switch_admin_only_and_read_by_every_app(client, db):
    await db.config.insert_one({"appVersionNumber": "2.1.1"})
    _, uh = await make_user(db)
    _, ah = await make_user(db, user_type="admin")
    assert (await client.put("/api/v1/admin/review-prompt", headers=uh, json={"platform": "android", "on": True})).status_code == 403
    assert (await client.get("/api/v1/admin/review-prompt", headers=ah)).json() == {"android": False, "ios": False}
    r = await client.put("/api/v1/admin/review-prompt", headers=ah, json={"platform": "android", "on": True})
    assert r.json() == {"android": True, "ios": False}
    v = (await client.get("/api/v1/config/version")).json()
    assert v["showReviewAndroid"] is True and v["showReviewIos"] is False and v["appVersionNumber"] == "2.1.1"
    for bad in ({"platform": "web", "on": True}, {"platform": "ios", "on": "yes"}, {"platform": "ios", "on": 1},
                {"platform": "ios", "on": True, "x": 1}):
        assert (await client.put("/api/v1/admin/review-prompt", headers=ah, json=bad)).status_code == 422
    assert await db.admin_log.count_documents({"action": "review_prompt"}) == 1
