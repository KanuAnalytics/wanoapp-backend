"""WhatsApp sharing ("wa_share"): shares to WhatsApp, share-page opens and
installs from shared links are counted, only with the switch on."""

from datetime import datetime, timedelta

import pytest

from app.services import wa_growth
from app.routes import share as share_routes
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def _on(db, **more):
    await db.config.insert_one({"features": {"wa_share": True, **more}})
    share_routes._BRAND["at"] = 0.0


async def test_WA1_off_records_nothing(client, db):
    share_routes._BRAND["at"] = 0.0
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/videos/{vid}/share", json={"to": "whatsapp"}, headers=h)
    assert r.status_code == 200 and r.json()["shared"] is True
    assert await db.wa_daily.count_documents({}) == 0
    page = await client.get(f"/v/{vid}?s=wa", headers={"user-agent": "Mozilla/5.0 (Linux; Android 14)"})
    assert "/s/open.gif" not in page.text and "utm_content" not in page.text
    r = await client.get(f"/s/open.gif?k=v&i={vid}&s=wa&p=android")
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif"
    assert await db.wa_daily.count_documents({}) == 0
    r = await client.post("/api/v1/growth/install", json={"referrer": "utm_source=wano_share&utm_content=wa"},
                          headers={"x-device-id": "d1"})
    assert r.json() == {"counted": False}


async def test_WA2_shares_counted_by_destination(client, db):
    await _on(db)
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    await client.post(f"/api/v1/videos/{vid}/share", json={"to": "whatsapp"}, headers=h)
    await client.post(f"/api/v1/videos/{vid}/share", json={"to": "other"}, headers=h)
    await client.post(f"/api/v1/videos/{vid}/share", json={"to": "telegram-bot<script>"}, headers=h)
    await client.post(f"/api/v1/videos/{vid}/share", json={}, headers=h)            # old apps
    st = await wa_growth.stats(db)
    w, = st["weeks"]
    assert w["shared_wa"] == 1 and w["shared_other"] == 1
    assert st["top"][0]["video_id"] == vid and st["top"][0]["shares"] == 1


async def test_WA3_share_page_pixel_and_store_links(client, db):
    await _on(db, open_after_install=True)
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    android = {"user-agent": "Mozilla/5.0 (Linux; Android 14; SM-A145F)"}
    page = (await client.get(f"/v/{vid}?s=wa", headers=android)).text
    assert f"/s/open.gif?k=v&amp;i={vid}&amp;s=wa&amp;p=android" in page
    assert "utm_content%253Dwa" in page                 # installs from WhatsApp links told apart
    plain = (await client.get(f"/v/{vid}", headers=android)).text
    assert "s=link" in plain and "utm_content" not in plain
    iphone = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"}
    ios = (await client.get(f"/v/{vid}?s=wa", headers=iphone)).text
    assert "share_video_wa" in ios and "p=ios" in ios
    # Something else in ?s= is ignored.
    odd = (await client.get(f"/v/{vid}?s=<b>", headers=android)).text
    assert "s=link" in odd and "<b>" not in odd


async def test_WA4_opens_once_per_phone_per_post_per_day(client, db):
    await _on(db)
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    ua = {"user-agent": "Mozilla/5.0 (Linux; Android 14)", "x-forwarded-for": "41.1.2.3"}
    for _ in range(3):
        await client.get(f"/s/open.gif?k=v&i={vid}&s=wa&p=android", headers=ua)
    await client.get(f"/s/open.gif?k=v&i={vid}&s=wa&p=android",
                     headers={**ua, "x-forwarded-for": "41.9.9.9"})
    await client.get("/s/open.gif?k=v&i=notanid&s=wa&p=android", headers=ua)
    await client.get("/s/open.gif?k=v&i=" + "f" * 24 + "&s=wa&p=android", headers=ua)   # no such post
    st = await wa_growth.stats(db)
    w, = st["weeks"]
    assert w["opens_wa"] == 2 and w["opens_android"] == 2
    assert st["top"][0]["opens"] == 2
    assert await db.wa_seen.count_documents({}) == 2
    # Nothing personal kept: the seen marks are hashes.
    async for d in db.wa_seen.find({}):
        assert "41." not in d["_id"] and len(d["_id"]) == 32


async def test_WA5_installs_from_links_once_per_device(client, db):
    await _on(db)
    ref = "utm_source=wano_share&utm_medium=video&utm_campaign=share&utm_content=wa&wano_path=v%2F" + "a" * 24
    r = await client.post("/api/v1/growth/install", json={"referrer": ref}, headers={"x-device-id": "dev-1"})
    assert r.json() == {"counted": True}
    r = await client.post("/api/v1/growth/install", json={"referrer": ref}, headers={"x-device-id": "dev-1"})
    assert r.json() == {"counted": False}
    r = await client.post("/api/v1/growth/install",
                          json={"referrer": "utm_source=wano_share&utm_medium=profile&utm_campaign=share"},
                          headers={"x-device-id": "dev-2"})
    assert r.json() == {"counted": True}
    r = await client.post("/api/v1/growth/install", json={"referrer": "utm_source=google-play&utm_medium=organic"},
                          headers={"x-device-id": "dev-3"})
    assert r.json() == {"counted": False}                     # not from a Wano link
    r = await client.post("/api/v1/growth/install", json={"referrer": ref})
    assert r.json() == {"counted": False}                     # no device id
    w, = (await wa_growth.stats(db))["weeks"]
    assert w["installs_wa"] == 1 and w["installs_link"] == 1


async def test_WA6_admin_page_numbers_admin_only(client, db):
    await _on(db)
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/whatsapp", headers=h)).status_code in (401, 403)
    _, ah = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/admin/whatsapp?days=28", headers=ah)
    assert r.status_code == 200 and r.json()["on"] is True and r.json()["weeks"] == []


async def test_WA7_weeks_grouped_and_window(db):
    await _on(db)
    now = datetime(2026, 11, 21, 12)
    for d in (0, 1, 8, 40):
        await wa_growth.shared(db, "a" * 24, "u", "whatsapp", now=now - timedelta(days=d))
    st = await wa_growth.stats(db, 28, now=now)
    assert [w["week"] for w in st["weeks"]] == ["2026-11-09", "2026-11-16"]
    assert sum(w["shared_wa"] for w in st["weeks"]) == 3


async def test_WA8_switch_listed(client, db):
    assert (await client.get("/api/v1/config/features")).json()["wa_share"] is False


async def test_WA9_invented_device_ids_capped_per_connection(client, db):
    await _on(db)
    ref = "utm_source=wano_share&utm_medium=video&utm_campaign=share&utm_content=wa"
    net = {"x-forwarded-for": "102.1.1.1"}
    got = [(await client.post("/api/v1/growth/install", json={"referrer": ref},
                              headers={**net, "x-device-id": f"fake-{i}"})).json()["counted"] for i in range(35)]
    assert got.count(True) == wa_growth.INSTALLS_PER_NETWORK_DAY
    r = await client.post("/api/v1/growth/install", json={"referrer": ref},
                          headers={"x-forwarded-for": "102.9.9.9", "x-device-id": "real-1"})
    assert r.json() == {"counted": True}                     # another connection isn't affected
