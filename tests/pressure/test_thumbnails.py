"""Attacks on real thumbnails: no post shows the grey placeholder when Cloudflare has a frame."""

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_for_you import _catalog, _feed, ago, mix_on, rb  # noqa: F401
from tests.pressure.test_new_feed import _ids

pytestmark = pytest.mark.asyncio

UID = "20eb73eed704d1863d5a7ad1b434d340"
CF = f"https://videodelivery.net/{UID}/manifest/video.m3u8"
CUSTOM = f"https://customer-abc123.cloudflarestream.com/{UID}/manifest/video.m3u8"


def _placeholder():
    from app.services.video_posts import DEFAULT_THUMBNAIL
    return DEFAULT_THUMBNAIL


async def _post(db, thumb, **extra):
    c, _ = await make_user(db)
    return await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True,
                            urls={"thumbnail": thumb}, **{"remoteUrl_CF": CF, "remoteUrl": CF, **extra})


async def _new_thumbs(client):
    r = await client.get("/api/v1/feed/?cursor=start&limit=50&authenticated=false")
    return {v["id"]: v["thumbnail"] for v in r.json()}


async def test_T1_placeholder_becomes_the_cloudflare_frame_in_new(client, db, rb):
    vid = await _post(db, _placeholder())
    assert (await _new_thumbs(client))[vid] == f"https://videodelivery.net/{UID}/thumbnails/thumbnail.jpg?time=1s&height=720"


async def test_T1_missing_thumbnail_too(client, db, rb):
    vid = await _post(db, None)
    assert UID in (await _new_thumbs(client))[vid]


async def test_T2_customer_subdomain_keeps_its_host(client, db, rb):
    vid = await _post(db, _placeholder(), remoteUrl_CF=CUSTOM, remoteUrl=CUSTOM)
    assert (await _new_thumbs(client))[vid].startswith(f"https://customer-abc123.cloudflarestream.com/{UID}/thumbnails/")


async def test_T3_for_you_and_trending_too(client, db, rb):
    _, ids = await _catalog(db, n=20, creators=5)
    vid = await _post(db, _placeholder())
    rb.pool = [vid] + ids
    _, h = await make_user(db)
    page = {v["id"]: v["thumbnail"] for v in (await _feed(client, h, limit=20)).json()}
    assert UID in page[vid]


async def test_T4_real_thumbnails_and_photos_untouched(client, db, rb):
    real = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key/profile-pictures/x_thumbnail.jpg"
    vid = await _post(db, real)
    c, _ = await make_user(db)
    photo = await make_video(db, c, created_at=ago(minutes=4), media_type="photo", remoteUrl_CF=None,
                             images=["https://cdn.example/p1.jpg"], urls={"thumbnail": "https://cdn.example/p1.jpg"})
    t = await _new_thumbs(client)
    assert t[vid] == real and t[photo] == "https://cdn.example/p1.jpg"


@pytest.mark.parametrize("url", [
    "javascript:alert(1)//videodelivery.net/" + UID + "/",
    "https://evil.example/videodelivery.net/" + UID + "/manifest/video.m3u8",
    "https://videodelivery.net.evil.example/" + UID + "/manifest/video.m3u8",
    "https://videodelivery.net/NOTHEX" + "x" * 26 + "/manifest/video.m3u8",
    "https://spaces.example/video.mp4",
])
async def test_T5_no_frame_from_foreign_or_bad_urls(client, db, rb, url):
    vid = await _post(db, _placeholder(), remoteUrl_CF=url, remoteUrl=url)
    assert (await _new_thumbs(client))[vid] == _placeholder()


async def test_T6_stream_ready_webhook_fixes_the_stored_thumbnail(client, db, rb, monkeypatch):
    from app.routes import upload_video
    c, _ = await make_user(db)
    vid = await make_video(db, c, isReadyToStream=False, stream_uid=UID, urls={"thumbnail": _placeholder()},
                           remoteUrl_CF=CF, remoteUrl=CF)
    real = "https://cdn.example/custom.jpg"
    vid2 = await make_video(db, c, isReadyToStream=False, stream_uid="a" * 32, urls={"thumbnail": real},
                            remoteUrl_CF=f"https://videodelivery.net/{'a' * 32}/manifest/video.m3u8")
    for v, uid in ((vid, UID), (vid2, "a" * 32)):
        await upload_video.mark_stream_ready(uid)
    d1 = await db.videos.find_one({"_id": ObjectId(vid)})
    d2 = await db.videos.find_one({"_id": ObjectId(vid2)})
    assert d1["isReadyToStream"] is True and UID in d1["urls"]["thumbnail"]
    assert d2["urls"]["thumbnail"] == real


async def test_T7_backfill_dry_run_then_real_run_then_idempotent(client, db, rb):
    from app.services import thumbnails
    bad = [await _post(db, _placeholder()) for _ in range(3)]
    none = await _post(db, None)
    real = await _post(db, "https://cdn.example/r.jpg")
    nouid = await _post(db, _placeholder(), remoteUrl_CF="https://spaces.example/v.mp4", remoteUrl=None)
    plan = await thumbnails.backfill(db, apply=False)
    assert plan["would_fix"] == 4 and plan["fixed"] == 0
    assert (await db.videos.find_one({"_id": ObjectId(bad[0])}))["urls"]["thumbnail"] == _placeholder()
    done = await thumbnails.backfill(db, apply=True)
    assert done["fixed"] == 4
    for v in bad + [none]:
        assert UID in (await db.videos.find_one({"_id": ObjectId(v)}))["urls"]["thumbnail"]
    assert (await db.videos.find_one({"_id": ObjectId(real)}))["urls"]["thumbnail"] == "https://cdn.example/r.jpg"
    assert (await db.videos.find_one({"_id": ObjectId(nouid)}))["urls"]["thumbnail"] == _placeholder()
    assert (await thumbnails.backfill(db, apply=True))["fixed"] == 0


async def test_T8_backfill_is_admin_only(client, db, rb):
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/fix-thumbnails", headers=h)).status_code == 403
    _, ah = await make_user(db, user_type="admin")
    await _post(db, _placeholder())
    r = await client.post("/api/v1/recombee/fix-thumbnails?apply=false", headers=ah)
    assert r.status_code == 200 and r.json()["would_fix"] == 1


async def test_T9_recombee_gets_the_real_frame(client, db, rb):
    from app.services.recommendations import item_values
    vid = await _post(db, _placeholder())
    doc = await db.videos.find_one({"_id": ObjectId(vid)})
    assert UID in item_values(doc)["thumbnail"]


async def test_T10_creator_stats_show_the_frame(client, db, rb):
    c, ch = await make_user(db)
    vid = await make_video(db, c, created_at=ago(minutes=5), isReadyToStream=True,
                           urls={"thumbnail": _placeholder()}, remoteUrl_CF=CF)
    posts = (await client.get("/api/v1/creators/me/stats", headers=ch)).json()["posts"]
    assert UID in next(p for p in posts if p["id"] == vid)["thumbnail"]


async def test_T11_odd_documents(client, db, rb):
    c, _ = await make_user(db)
    await make_video(db, c, created_at=ago(minutes=1), isReadyToStream=True, urls="junk", remoteUrl_CF=12345)
    await make_video(db, c, created_at=ago(minutes=2), isReadyToStream=True, urls={"thumbnail": 7}, remoteUrl_CF=None)
    r = await client.get("/api/v1/feed/?cursor=start&limit=10&authenticated=false")
    assert r.status_code == 200, r.text
    from app.services import thumbnails
    assert (await thumbnails.backfill(db, apply=True))["errors"] == 0
