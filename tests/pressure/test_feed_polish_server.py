"""2.3.39 server: photo posts the rules would refuse are counted, never refused;
the For You report splits wide videos, tall videos and photos; five new
switches, all off."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import photo_checks
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

OURS = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key/photos/a.jpg"


async def _photo(client, h, images):
    r = await client.post("/api/v1/videos/post", headers=h, json={"media_type": "photo", "images": images,
                                                                  "description": "x"})
    return r


async def test_PC1_normal_photo_posts_noted_nowhere(client, db):
    _, h = await make_user(db)
    r = await _photo(client, h, [OURS] * 3)
    assert r.status_code in (200, 201), r.text
    assert await db.photo_checks.count_documents({}) == 0


async def test_PC2_eleven_photos_still_posted_but_counted(client, db):
    _, h = await make_user(db)
    r = await _photo(client, h, [OURS] * 11)
    assert r.status_code in (200, 201), r.text
    row = await db.photo_checks.find_one({})
    assert row["too_many"] == 11 and "elsewhere" not in row


async def test_PC3_photos_from_elsewhere_counted_with_host_only(client, db):
    _, h = await make_user(db)
    r = await _photo(client, h, [OURS, "https://evil.example.com/track.gif?u=123", "http://insecure.test/a.jpg"])
    assert r.status_code in (200, 201), r.text
    row = await db.photo_checks.find_one({})
    assert row["elsewhere"] == 2 and row["hosts"] == ["evil.example.com", "insecure.test"]
    assert "track.gif" not in str(row) and "u=123" not in str(row)


async def test_PC4_lookalike_hosts_are_not_ours(client, db):
    for bad in ("https://wano-africadev.lon1.cdn.digitaloceanspaces.com.evil.com/a.jpg",
                "https://evil.com/wano-africadev.lon1.cdn.digitaloceanspaces.com/a.jpg",
                "https://user@evil.com/a.jpg", "javascript:alert(1)"):
        assert photo_checks._ours(bad) is False, bad
    assert photo_checks._ours(OURS) is True


async def test_PC5_counting_never_breaks_a_post(client, db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db.photo_checks, "insert_one", boom, raising=False)
    _, h = await make_user(db)
    r = await _photo(client, h, [OURS] * 12)
    assert r.status_code in (200, 201), r.text


async def test_PC6_admin_summary(client, db):
    _, h = await make_user(db)
    await _photo(client, h, [OURS])
    await _photo(client, h, [OURS] * 11)
    await _photo(client, h, ["https://evil.example.com/a.jpg"])
    _, ah = await make_user(db, user_type="admin")
    s = (await client.get("/api/v1/admin/photo-checks", headers=ah)).json()
    assert s["photo_posts"] == 3 and s["would_refuse"] == 2 and s["too_many"] == 1 and s["elsewhere"] == 1
    assert s["hosts"] == [["evil.example.com", 1]]
    _, uh = await make_user(db)
    assert (await client.get("/api/v1/admin/photo-checks", headers=uh)).status_code == 403


async def test_SH1_report_splits_wide_tall_and_photo(client, db):
    c, _ = await make_user(db)
    wide = await make_video(db, c, supports_landscape=True)
    tall = await make_video(db, c)
    photo = await make_video(db, c, media_type="photo", images=[OURS])
    now = datetime.utcnow()
    rows = []
    for vid, portions in ((wide, [0.1, 0.1, 0.95]), (tall, [0.95, 0.95]), (photo, [None])):
        for p in portions:
            d = {"viewer": ObjectId(), "video_id": ObjectId(vid), "at": now - timedelta(minutes=5),
                 "variant": "a", "source": "x"}
            if p is not None:
                d["portion"] = p
            rows.append(d)
    await db.feed_impressions.insert_many(rows)
    _, ah = await make_user(db, user_type="admin")
    assert "by_shape" not in (await client.get("/api/v1/metrics/for-you?days=1", headers=ah)).json()
    rep = (await client.get("/api/v1/metrics/for-you?days=1&shapes=true", headers=ah)).json()
    s = rep["by_shape"]
    assert s["wide_video"]["impressions"] == 3 and abs(s["wide_video"]["skip_rate"] - 2 / 3) < 0.01
    assert s["tall_video"]["completion_rate"] == 1.0
    assert s["photo"]["impressions"] == 1 and s["photo"]["watched"] == 0
    assert "variants" in rep


async def test_SW1_new_switches_off_by_default(client, db):
    f = (await client.get("/api/v1/config/features")).json()
    for k in ("wide_fill", "quiet_counts", "follow_plus", "dark_nav", "photo_zoom", "photo_full"):
        assert f[k] is False, k
