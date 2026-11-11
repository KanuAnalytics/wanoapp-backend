"""Speed for slow connections: small picture copies and speed timings. Attacks first."""

import io
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"


def _jpeg(w=2400, h=3000, color=(200, 40, 40)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=95)
    return buf.getvalue()


@pytest.fixture
def store(monkeypatch):
    from app.services import small_images
    objects, puts = {}, {}

    def fetch(key):
        if key not in objects:
            raise KeyError(key)
        return objects[key]

    def put(key, data):
        puts[key] = data

    monkeypatch.setattr(small_images, "_fetch", fetch)
    monkeypatch.setattr(small_images, "_put", put)
    return objects, puts


# ------------------------------------------------------------------ small pictures

def test_SI1_paths_only_for_our_cdn_and_pictures():
    from app.services import small_images as si
    assert si.source_key(f"{CDN}/profile-pictures/a b.JPG?x=1") == "profile-pictures/a b.JPG"
    assert si.variant_url(f"{CDN}/profile-pictures/abc.jpg", 160) == f"{CDN}/small/w160/profile-pictures/abc.webp"
    for bad in (None, 5, "https://evil.com/x.jpg", f"{CDN}/videos/clip.mp4", f"{CDN}/small/w160/a.webp",
                f"{CDN}/../etc/passwd.jpg", "https://videodelivery.net/abc/thumbnails/thumbnail.jpg", "x" * 5000):
        assert si.source_key(bad) is None, bad


def test_SI2_copies_are_small_upright_and_never_bigger_than_the_original():
    from PIL import Image
    from app.services import small_images as si
    out = dict(si.make_variants(_jpeg()))
    assert set(out) == {160, 320, 480, 1080}
    assert len(out[160]) < 3000 and len(out[480]) < 20000
    assert Image.open(io.BytesIO(out[480])).size == (480, 600)
    tiny = dict(si.make_variants(_jpeg(100, 80)))
    assert Image.open(io.BytesIO(tiny[1080])).size == (100, 80)          # not blown up
    # Transparent PNG keeps working; garbage raises (the caller records it as failed).
    buf = io.BytesIO()
    Image.new("RGBA", (300, 300), (0, 0, 0, 0)).save(buf, "PNG")
    assert dict(si.make_variants(buf.getvalue()))[160]
    with pytest.raises(Exception):
        si.make_variants(b"not an image")


async def test_SI3_sweep_does_recent_then_backlog_once_and_records_failures(db, store):
    from app.services import small_images as si
    objects, puts = store
    objects["profile-pictures/me.jpg"] = _jpeg()
    objects["profile-pictures/old.png"] = _jpeg(900, 900)
    objects["images/post.jpg"] = _jpeg(1500, 2000)
    me, _ = await make_user(db, profile_picture=f"{CDN}/profile-pictures/me.jpg", updated_at=datetime.utcnow())
    await make_user(db, profile_picture=f"{CDN}/profile-pictures/old.png",
                    updated_at=datetime.utcnow() - timedelta(days=300))
    await make_user(db, profile_picture=f"{CDN}/profile-pictures/missing.jpg")
    await make_user(db, profile_picture="https://lh3.googleusercontent.com/a/x")   # not ours: skipped
    await make_video(db, me, media_type="photo", images=[f"{CDN}/images/post.jpg"],
                     urls={"thumbnail": f"{CDN}/images/post.jpg"})
    r = await si.sweep(db)
    assert r["done"] == 3 and r["failed"] == 1
    assert f"small/w160/profile-pictures/me.webp" in puts and "small/w1080/images/post.webp" in puts
    assert (await db.image_variants.find_one({"_id": "profile-pictures/missing.jpg"}))["state"] == "failed"
    puts.clear()
    r = await si.sweep(db)                                  # nothing new: nothing redone
    assert r["done"] == 0 and r["failed"] == 0 and not puts
    st = await si.status(db)
    assert st["done"] == 3 and st["failed"] == 1 and st["totals"]["orig"] > st["totals"]["w160"]


async def test_SI4_two_processes_never_do_the_same_picture(db, store):
    from app.services import small_images as si
    assert await si.claim(db, "profile-pictures/a.jpg") is True
    assert await si.claim(db, "profile-pictures/a.jpg") is False
    # A stuck claim from a crashed process is taken back after a while.
    await db.image_variants.update_one({"_id": "profile-pictures/a.jpg"},
                                       {"$set": {"at": datetime.utcnow() - timedelta(hours=1)}})
    assert await si.claim(db, "profile-pictures/a.jpg") is True


async def test_SI5_admin_progress_needs_admin(client, db, store):
    _, adh = await make_user(db, user_type="admin")
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/small-images", headers=h)).status_code in (401, 403)
    r = await client.get("/api/v1/admin/small-images", headers=adh)
    assert r.status_code == 200 and r.json()["done"] == 0


# ------------------------------------------------------------------ speed timings

P = "/api/v1/perf"


async def _on(db):
    await db.config.insert_one({"features": {"perf": True}})


async def test_PF1_off_by_default(client, db):
    r = await client.post(P, json={"events": [{"name": "feed_load", "ms": 500}]})
    assert r.status_code == 404


async def test_PF2_hostile_bodies(client, db):
    await _on(db)
    for b in ({"events": [{"name": "x", "ms": 1}]}, {"events": [{"name": "stall", "ms": -1}]},
              {"events": [{"name": "stall", "ms": "5"}]}, {"events": [{"name": "stall", "ms": True}]},
              {"events": [{"name": "stall", "ms": 10 ** 9}]}, {"events": [], "net": "5g!"},
              {"events": [{"name": "stall", "ms": 1}] * 61}, {"events": [], "user": "me"},
              {"events": [], "tz": "x" * 100}, {"events": [{"name": "stall", "ms": 1, "who": 1}]}):
        assert (await client.post(P, json=b)).status_code == 422, b


async def test_PF3_counts_by_country_from_time_zone_and_reports_percentiles(client, db):
    await _on(db)
    ev = [{"name": "first_video", "ms": ms} for ms in (900, 1200, 1800, 2500, 9000)] + \
         [{"name": "stall", "ms": 400}]
    r = await client.post(P, json={"events": ev, "tz": "Africa/Lagos", "net": "cellular"})
    assert r.json() == {"recorded": 6}
    await client.post(P, json={"events": [{"name": "first_video", "ms": 300}], "tz": "Mars/Base", "net": "wifi"})
    assert await db.perf_daily.count_documents({}) == 3
    _, adh = await make_user(db, user_type="admin")
    rows = (await client.get("/api/v1/admin/perf", headers=adh)).json()["rows"]
    ng = next(x for x in rows if x["country"] == "NG" and x["metric"] == "first_video")
    assert ng["count"] == 5 and ng["net"] == "cellular" and ng["p50"] == 2000 and ng["p90"] == 13000
    assert any(x["country"] == "other" for x in rows)
    # Nothing that identifies anyone is stored.
    doc = await db.perf_daily.find_one({"country": "NG", "metric": "first_video"})
    assert set(doc) <= {"_id", "count", "sum", "b", "day", "country", "net", "metric"}


async def test_PF4_rate_limited_per_network(client, db, monkeypatch):
    from app.api.v1 import perf as perf_api
    monkeypatch.setattr(perf_api, "PER_MINUTE", 3)
    perf_api._hits.clear()
    await _on(db)
    codes = [(await client.post(P, json={"events": [{"name": "stall", "ms": 5}]})).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    perf_api._hits.clear()


def test_PF5_percentile_helper():
    from app.services import perf
    assert perf.percentile({}, 0.5) is None
    assert perf.bucket(0) == 0 and perf.bucket(100) == 0 and perf.bucket(101) == 1 and perf.bucket(10 ** 6) == len(perf.EDGES)
    assert perf.country_of("Africa/Kampala") == "UG" and perf.country_of(None) == "other"
