"""Round 2: the real signing code, stampedes, huge playlists, key changes."""

import asyncio
import time

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401
from tests.pressure.test_tv_protect import CDN, FakeSpaces, _title, spaces  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_VR1_real_presign_is_a_short_lived_s3v4_link(monkeypatch):
    from app.core.config import settings
    from app.services import tv_stream
    monkeypatch.setattr(settings, "DO_SPACES_ENDPOINT", "https://lon1.digitaloceanspaces.com", raising=False)
    monkeypatch.setattr(settings, "DO_SPACES_BUCKET_NAME", "wano-africadev", raising=False)
    monkeypatch.setattr(settings, "DO_SPACES_ACCESS_KEY_ID", "AKIAFAKE", raising=False)
    monkeypatch.setattr(settings, "DO_SPACES_SECRET_KEY", "fakesecret", raising=False)
    url = tv_stream._presign("wano-tv/middleton/720p/seg_000.ts", 3600)
    assert url.startswith("https://") and "X-Amz-Expires=3600" in url and "X-Amz-Signature=" in url
    assert "wano-tv/middleton/720p/seg_000.ts" in url and "fakesecret" not in url


async def test_VR2_missing_file_404_other_storage_errors_502(client, db, rb, monkeypatch):
    from botocore.exceptions import ClientError
    from app.services import tv_stream
    a = await _title(db, "middleton")
    tv_stream.clear_cache()

    class C:
        def __init__(self, code):
            self.code = code

        def get_object(self, **k):
            raise ClientError({"Error": {"Code": self.code}}, "GetObject")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    monkeypatch.setattr(tv_stream, "_client", lambda: C("NoSuchKey"))
    assert (await client.get(f"/api/v1/tv/stream/{token}/master.m3u8")).status_code == 404
    monkeypatch.setattr(tv_stream, "_client", lambda: C("AccessDenied"))
    r = await client.get(f"/api/v1/tv/stream/{token}/720p/index.m3u8")
    assert r.status_code == 502 and "AccessDenied" not in r.text


async def test_VR3_a_burst_of_players_hits_storage_once(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    rs = await asyncio.gather(*[client.get(f"/api/v1/tv/stream/{token}/1080p/index.m3u8") for _ in range(60)])
    assert all(r.status_code == 200 for r in rs)
    assert spaces.fetches.count("wano-tv/middleton/1080p/index.m3u8") == 1


async def test_VR4_two_hour_film_playlist_rewrites_fast(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n" + "".join(f"#EXTINF:6.0,\nseg_{i:05d}.ts\n" for i in range(1500))
    spaces.objects["wano-tv/middleton/1080p/index.m3u8"] = body + "#EXT-X-ENDLIST\n"
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    t = time.perf_counter()
    r = await client.get(f"/api/v1/tv/stream/{token}/1080p/index.m3u8")
    assert r.status_code == 200 and time.perf_counter() - t < 2
    assert r.text.count("https://signed.example/") == 1500 and r.text.rstrip().endswith("#EXT-X-ENDLIST")


async def test_VR5_moving_a_title_off_spaces_kills_old_links(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 100)
    await db.tv_titles.update_one({"_id": ObjectId(a)},
                                  {"$set": {"video_url": "https://videodelivery.net/" + "b" * 32 + "/manifest/video.m3u8"}})
    assert (await client.get(f"/api/v1/tv/stream/{token}/master.m3u8")).status_code == 404


async def test_VR6_tokens_from_another_secret_are_refused(client, db, rb, spaces, monkeypatch):
    from app.core.config import settings
    from app.services import tv_stream
    a = await _title(db, "middleton")
    monkeypatch.setattr(settings, "SECRET_KEY", "someone-elses-secret", raising=False)
    forged = tv_stream.sign(a, "u", int(time.time()) + 100)
    monkeypatch.undo()
    assert (await client.get(f"/api/v1/tv/stream/{forged}/master.m3u8")).status_code == 403


async def test_VR7_re_encoded_title_in_a_new_folder_plays(client, db, rb, spaces):
    a = await _title(db, "middleton", video_url=f"{CDN}/wano-tv/middleton-v2/master.m3u8")
    spaces.objects["wano-tv/middleton-v2/master.m3u8"] = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n720p/index.m3u8\n"
    _, h = await make_user(db)
    t = {x["slug"]: x for x in (await client.get("/api/v1/tv/titles", headers=h)).json()}["middleton"]
    from urllib.parse import urlparse
    r = await client.get(urlparse(t["video_url"]).path)
    assert r.status_code == 200 and "720p/index.m3u8" in r.text and a


async def test_VR8_odd_catalogue_entries_do_not_break_signing(client, db, rb, spaces):
    await _title(db, "odd1", video_url=12345)
    await _title(db, "odd2", video_url=f"{CDN}/not-tv/x/master.m3u8")
    await _title(db, "odd3", captions=[{"language": "en", "label": "E", "url": None}, "junk"])
    _, h = await make_user(db)
    r = await client.get("/api/v1/tv/titles", headers=h)
    assert r.status_code == 200


async def test_VR9_late_start_still_gets_segments_that_outlast_a_pause(client, db, rb, spaces):
    """A token with a few minutes left must not hand out segments that die mid-film."""
    from app.services import tv_stream
    a = await _title(db, "middleton")
    token = tv_stream.sign(a, "u", int(time.time()) + 120)       # 2 minutes left
    body = (await client.get(f"/api/v1/tv/stream/{token}/720p/index.m3u8")).text
    seg = [l for l in body.splitlines() if l.startswith("https://signed.example/")][0]
    expiry = int(seg.split("X-Amz-Expires=")[1].split("&")[0])
    assert expiry >= tv_stream.MIN_SEGMENT_SECONDS >= 3 * 3600
    assert expiry <= 7 * 24 * 3600                                # S3v4 maximum


async def test_VR10_expired_link_tells_the_app_to_refresh(client, db, rb, spaces):
    from app.services import tv_stream
    a = await _title(db, "middleton")
    old = tv_stream.sign(a, "u", int(time.time()) - 5)
    r = await client.get(f"/api/v1/tv/stream/{old}/master.m3u8")
    assert r.status_code == 403 and "expired" in r.text.lower()
