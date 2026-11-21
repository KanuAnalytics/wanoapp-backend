"""share_play: a ▶ on shared videos' link previews. Attacks: a young person's
post, a private post, a photo post, a thumbnail on someone else's host (no
fetching arbitrary URLs), a broken picture, and the switch off."""

import io

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


def _jpeg(w=720, h=1280):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def fetched(monkeypatch):
    from app.services import og_image
    og_image._cache.clear()
    urls = []

    async def fake(url):
        urls.append(url)
        return _jpeg()
    monkeypatch.setattr(og_image, "_fetch", fake)
    from app.routes import share
    share._BRAND["at"] = 0.0
    yield urls
    share._BRAND["at"] = 0.0
    share._PLAY["on"] = False


async def _on(db, on=True):
    await db.config.insert_one({"features": {"share_play": on}})


async def test_SP1_video_preview_gets_a_play_button(client, db, fetched):
    await _on(db)
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, title="market day")
    page = (await client.get(f"/v/{vid}")).text
    assert f"/og/{vid}.jpg" in page                       # og:image points at the play picture
    r = await client.get(f"/og/{vid}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    from PIL import Image
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    w, h = img.size
    assert max(w, h) <= 720
    assert img.getpixel((w // 2, h // 2))[0] > 200        # white triangle in the middle
    assert img.getpixel((5, 5))[1] > 100                   # the picture itself around it
    await client.get(f"/og/{vid}.jpg")
    assert len(fetched) == 1                               # second time from memory
    out = (await client.get(f"/api/v1/public/videos/{vid}")).json()
    assert out["preview_image"].endswith(f"/og/{vid}.jpg")


async def test_SP2_never_for_young_private_photo_or_switch_off(client, db, fetched):
    await _on(db)
    from datetime import datetime
    teen, _ = await make_user(db, date_of_birth=datetime(datetime.utcnow().year - 14, 1, 1))
    adult, _ = await make_user(db)
    tv = await make_video(db, teen)
    pv = await make_video(db, adult, privacy="private")
    assert (await client.get(f"/og/{tv}.jpg")).status_code == 404
    assert (await client.get(f"/og/{pv}.jpg")).status_code == 404
    assert (await client.get(f"/og/{ObjectId()}.jpg")).status_code == 404
    assert (await client.get("/og/nope.jpg")).status_code == 404
    assert fetched == []
    await db.config.update_one({}, {"$set": {"features.share_play": False}})
    from app.routes import share
    share._BRAND["at"] = 0.0
    vid = await make_video(db, adult)
    assert (await client.get(f"/og/{vid}.jpg")).status_code == 404
    assert f"/og/{vid}.jpg" not in (await client.get(f"/v/{vid}")).text
    assert (await client.get(f"/api/v1/public/videos/{vid}")).json()["preview_image"] is None


async def test_SP3_only_wano_hosts_are_fetched_and_a_bad_picture_falls_back(client, db, monkeypatch):
    from app.services import og_image
    assert og_image.allowed("https://videodelivery.net/abc/thumbnails/thumbnail.jpg")
    assert og_image.allowed("https://customer-x.cloudflarestream.com/abc/thumbnails/thumbnail.jpg")
    assert og_image.allowed("https://wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg")
    for bad in ("http://videodelivery.net/a.jpg", "https://evil.com/a.jpg", "https://videodelivery.net.evil.com/a.jpg",
                "https://user:pw@videodelivery.net/a.jpg", "https://169.254.169.254/latest", None):
        assert not og_image.allowed(bad), bad
    og_image._cache.clear()

    async def junk(url):
        return b"not a picture"
    monkeypatch.setattr(og_image, "_fetch", junk)
    await _on(db)
    from app.routes import share
    share._BRAND["at"] = 0.0
    owner, _ = await make_user(db)
    vid = await make_video(db, owner)
    r = await client.get(f"/og/{vid}.jpg")
    assert r.status_code == 302 and "thumbnail" in r.headers["location"]
    share._BRAND["at"] = 0.0
    share._PLAY["on"] = False


async def test_SP4_open_after_install_puts_the_post_in_the_play_link(client, db, monkeypatch):
    from urllib.parse import unquote
    from app.core.config import settings
    from app.routes import share
    monkeypatch.setattr(settings, "ANDROID_PLAY_STORE_URL", "https://play.google.com/store/apps/details?id=com.wano.app")
    owner, _ = await make_user(db)
    vid = await make_video(db, owner)
    android = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile"}
    share._BRAND["at"] = 0.0
    page = unquote(unquote(unquote((await client.get(f"/v/{vid}", headers=android)).text)))
    assert "wano_path" not in page and "utm_source=wano_share" in page        # switch off: as today
    await db.config.insert_one({"features": {"open_after_install": True}})
    share._BRAND["at"] = 0.0
    page = unquote(unquote(unquote((await client.get(f"/v/{vid}", headers=android)).text)))
    assert f"wano_path=v/{vid}" in page
    share._BRAND["at"] = 0.0
    share._OPEN["on"] = False
