"""Attacks on Wano TV film protection: expiring, signed playback instead of public files."""

import time
from datetime import date

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio

CDN = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key"

MASTER = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="subs/en/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=6083930,RESOLUTION=1920x1080,SUBTITLES="subs"
1080p/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3402549,RESOLUTION=1280x720,SUBTITLES="subs"
720p/index.m3u8
"""
VARIANT = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
seg_000.ts
#EXTINF:6.0,
seg_001.ts
#EXTINF:4.2,
https://evil.example/inject.ts
#EXT-X-ENDLIST
"""


class FakeSpaces:
    def __init__(self):
        self.objects = {}
        self.fetches = []
        self.down = False

    def fetch(self, key):
        self.fetches.append(key)
        if self.down:
            raise RuntimeError("spaces down")
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def presign(self, key, expires_in):
        return f"https://signed.example/{key}?X-Amz-Expires={int(expires_in)}&X-Amz-Signature=abc"


@pytest.fixture
def spaces(monkeypatch):
    from app.services import tv_stream
    fake = FakeSpaces()
    monkeypatch.setattr(tv_stream, "_fetch_object", fake.fetch)
    monkeypatch.setattr(tv_stream, "_presign", fake.presign)
    tv_stream.clear_cache()
    for folder in ("middleton", "one-field"):
        fake.objects[f"wano-tv/{folder}/master.m3u8"] = MASTER
        for v in ("1080p", "720p", "subs/en"):
            fake.objects[f"wano-tv/{folder}/{v}/index.m3u8"] = VARIANT
    return fake


async def _title(db, folder, **extra):
    doc = {
        "slug": folder, "title": folder.title(), "type": "movie", "genres": ["Documentary"], "year": 2026,
        "release_date": date(2026, 10, 1).isoformat(), "duration_minutes": 11, "age_rating": "All",
        "description": "d", "synopsis": "s",
        "trailer": {"duration_seconds": 60, "thumbnail": f"{CDN}/wano-tv/{folder}/land.jpg",
                    "video_url": f"{CDN}/wano-tv/{folder}/trailer/master.m3u8"},
        "credits": {"director": "Eilmer Bahri", "cast": [], "production": "Ondore Media", "distributor": "Wano TV"},
        "poster": f"{CDN}/wano-tv/{folder}/poster.jpg", "backdrop": f"{CDN}/wano-tv/{folder}/land.jpg",
        "video_url": f"{CDN}/wano-tv/{folder}/master.m3u8", "is_active": True, "popularity": 1,
        "captions": [{"language": "en", "label": "English", "url": f"{CDN}/wano-tv/{folder}/subs/en/index.m3u8"}],
    }
    doc.update(extra)
    res = await db.tv_titles.insert_one(doc)
    return str(res.inserted_id)


async def _titles(client, h):
    r = await client.get("/api/v1/tv/titles", headers=h)
    assert r.status_code == 200, r.text
    return {t["slug"]: t for t in r.json()}


def _path(url):
    from urllib.parse import urlparse
    return urlparse(url).path


async def test_V1_catalogue_hands_out_expiring_links_not_public_files(client, db, rb, spaces):
    await _title(db, "middleton")
    _, h = await make_user(db)
    t = (await _titles(client, h))["middleton"]
    assert "digitaloceanspaces" not in t["video_url"] and "/api/v1/tv/stream/" in t["video_url"]
    assert t["video_url"].endswith("/master.m3u8")
    assert "/api/v1/tv/stream/" in t["captions"][0]["url"]
    assert t["trailer"]["video_url"].startswith(CDN)              # trailers stay public
    assert t["poster"].startswith(CDN)


async def test_V2_master_playlist_points_back_through_the_backend(client, db, rb, spaces):
    await _title(db, "middleton")
    _, h = await make_user(db)
    url = (await _titles(client, h))["middleton"]["video_url"]
    r = await client.get(_path(url))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    body = r.text
    assert "digitaloceanspaces" not in body
    lines = [l for l in body.splitlines() if l and not l.startswith("#")]
    assert len(lines) == 2 and all("/api/v1/tv/stream/" in l and l.endswith("index.m3u8") for l in lines)
    assert 'URI="' in body and "/api/v1/tv/stream/" in body.split('URI="')[1]


async def test_V3_segments_get_short_lived_signed_links(client, db, rb, spaces):
    from app.services import tv_stream
    await _title(db, "middleton")
    _, h = await make_user(db)
    master = (await client.get(_path((await _titles(client, h))["middleton"]["video_url"]))).text
    variant_url = [l for l in master.splitlines() if l.endswith("720p/index.m3u8")][0]
    body = (await client.get(_path(variant_url))).text
    segs = [l for l in body.splitlines() if l and not l.startswith("#")]
    assert segs == [f"https://signed.example/wano-tv/middleton/720p/seg_00{i}.ts?X-Amz-Expires=" in s and s or s
                    for i, s in enumerate(segs)]
    assert all(s.startswith("https://signed.example/wano-tv/middleton/720p/") for s in segs)
    assert "evil.example" not in body                                    # foreign URLs dropped
    assert 'URI="https://signed.example/wano-tv/middleton/720p/init.mp4' in body
    expiry = int(segs[0].split("X-Amz-Expires=")[1].split("&")[0])
    assert tv_stream.MIN_SEGMENT_SECONDS <= expiry <= max(tv_stream.TOKEN_TTL_SECONDS, tv_stream.MIN_SEGMENT_SECONDS)


async def test_V4_expired_or_tampered_tokens_are_refused(client, db, rb, spaces, monkeypatch):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    b = await _title(db, "one-field")
    _, h = await make_user(db)
    url = _path((await _titles(client, h))["middleton"]["video_url"])
    token = url.split("/stream/")[1].split("/")[0]
    # Tampered signature / payload
    for bad in (token[:-2] + ("AA" if not token.endswith("AA") else "BB"), "x" + token, token.replace(".", ""), "a.b"):
        assert (await client.get(f"/api/v1/tv/stream/{bad}/master.m3u8")).status_code == 403
    # Token for A can't open B even if the path is B's
    forged = tv_stream.sign(a, "u", int(time.time()) + 100)
    assert (await client.get(f"/api/v1/tv/stream/{forged}/../one-field/master.m3u8")).status_code in (400, 403, 404)
    # Expired
    old = tv_stream.sign(a, "u", int(time.time()) - 1)
    assert (await client.get(f"/api/v1/tv/stream/{old}/master.m3u8")).status_code == 403
    assert b


@pytest.mark.parametrize("path", ["../one-field/master.m3u8", "%2e%2e/one-field/master.m3u8", "720p/seg_000.ts",
                                  "720p/../../one-field/master.m3u8", "..%2f..%2fone-field%2fmaster.m3u8",
                                  "720p/index.m3u8%00.ts", "/etc/passwd", "a" * 600 + ".m3u8", "720p\\index.m3u8"])
async def test_V5_path_tricks_are_refused(client, db, rb, spaces, path):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    r = await client.get(f"/api/v1/tv/stream/{token}/{path}")
    assert r.status_code in (400, 403, 404), (path, r.status_code)
    assert not any("one-field" in k for k in spaces.fetches)


async def test_V6_taken_down_title_stops_playing(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    assert (await client.get(f"/api/v1/tv/stream/{token}/master.m3u8")).status_code == 200
    await db.tv_titles.update_one({"_id": ObjectId(a)}, {"$set": {"is_active": False}})
    assert (await client.get(f"/api/v1/tv/stream/{token}/master.m3u8")).status_code == 404


async def test_V7_playlists_are_cached(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    for _ in range(50):
        assert (await client.get(f"/api/v1/tv/stream/{token}/720p/index.m3u8")).status_code == 200
    assert spaces.fetches.count("wano-tv/middleton/720p/index.m3u8") == 1


async def test_V8_storage_down_is_a_clean_502(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    spaces.down = True
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    r = await client.get(f"/api/v1/tv/stream/{token}/master.m3u8")
    assert r.status_code == 502 and "spaces" not in r.text.lower()
    missing = tv_stream.sign(a, "u", int(time.time()) + 100)
    spaces.down = False
    assert (await client.get(f"/api/v1/tv/stream/{missing}/480p/index.m3u8")).status_code == 404


async def test_V9_titles_hosted_elsewhere_are_left_alone(client, db, rb, spaces):
    await _title(db, "elsewhere", video_url="https://videodelivery.net/" + "a" * 32 + "/manifest/video.m3u8")
    _, h = await make_user(db)
    assert (await _titles(client, h))["elsewhere"]["video_url"].startswith("https://videodelivery.net/")


async def test_V10_tokens_carry_no_secrets_and_are_short(client, db, rb, spaces):
    from app.core.config import settings
    await _title(db, "middleton")
    uid, h = await make_user(db)
    url = (await _titles(client, h))["middleton"]["video_url"]
    assert settings.SECRET_KEY not in url and len(url) < 400


async def test_V11_big_catalogue_signs_fast(client, db, rb, spaces):
    for i in range(300):
        await _title(db, f"film-{i}")
    _, h = await make_user(db)
    t = time.perf_counter()
    assert len(await _titles(client, h)) == 300
    assert time.perf_counter() - t < 5


async def test_V12_protect_script_locks_films_but_not_trailers_or_art(client, db, rb):
    from app.services import tv_stream

    class FakeClient:
        def __init__(self):
            self.acl = {}

        def list(self, prefix):
            return [f"{prefix}middleton/master.m3u8", f"{prefix}middleton/720p/seg_000.ts",
                    f"{prefix}middleton/subs/en/index.m3u8", f"{prefix}middleton/trailer/master.m3u8",
                    f"{prefix}middleton/trailer/720p/seg_000.ts", f"{prefix}middleton/poster.jpg",
                    f"{prefix}middleton/land.webp"]

        def set_private(self, key):
            self.acl[key] = "private"

    fc = FakeClient()
    plan = tv_stream.protect_films(fc, apply=False)
    assert set(plan["private"]) == {"wano-tv/middleton/master.m3u8", "wano-tv/middleton/720p/seg_000.ts",
                                    "wano-tv/middleton/subs/en/index.m3u8"}
    assert not fc.acl
    tv_stream.protect_films(fc, apply=True)
    assert set(fc.acl) == set(plan["private"])


async def test_V13_new_film_uploads_are_private_trailers_public():
    from app.services.tv_stream import acl_for
    assert acl_for("wano-tv/x/master.m3u8") == "private"
    assert acl_for("wano-tv/x/1080p/seg_000.ts") == "private"
    assert acl_for("wano-tv/x/trailer/master.m3u8") == "public-read"
    assert acl_for("wano-tv/x/poster.jpg") == "public-read"
