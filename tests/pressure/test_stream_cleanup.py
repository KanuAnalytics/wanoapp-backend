"""Video storage cleanup (Nov 2026): Cloudflare videos no post uses are listed
for an admin to look at, then deleted. Nothing a post, draft or upload in
progress uses is ever listed or deleted."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def cf(uid, days_old=30, seconds=40.0, state="ready"):
    return {"uid": uid, "created": (NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z"),
            "duration": seconds, "status": {"state": state}, "meta": {"name": f"{uid}.mp4"},
            "thumbnail": f"https://videodelivery.net/{uid}/thumbnails/thumbnail.jpg"}


U = {k: (k * 32)[:32] for k in "abcdefgh"}


@pytest.fixture
def cf_deletes(monkeypatch):
    from app.services import stream_cleanup
    gone = []
    monkeypatch.setattr(stream_cleanup, "_delete", lambda uid: gone.append(uid))
    return gone


async def _world(db):
    creator, _ = await make_user(db)
    # a: public post with stream_uid; b: private post found only by URL (old doc);
    # c: inactive post; d: a draft clip; e: nothing (leftover); f: leftover but new;
    # g: the placeholder; h: leftover, error state
    await make_video(db, creator, stream_uid=U["a"], remoteUrl_CF=f"https://videodelivery.net/{U['a']}/manifest/video.m3u8")
    await make_video(db, creator, privacy="private", remoteUrl_CF=f"https://videodelivery.net/{U['b']}/manifest/video.m3u8")
    await make_video(db, creator, is_active=False, stream_uid=U["c"])
    await db.drafts.insert_one({"is_active": True, "clips": [{"remoteUrl": f"https://videodelivery.net/{U['d']}/manifest/video.m3u8"}]})
    from app.services import upload_DO
    upload_DO.PROTECTED_STREAM_UIDS = frozenset({U["g"]})
    return [cf(U["a"]), cf(U["b"]), cf(U["c"]), cf(U["d"]), cf(U["e"], seconds=90), cf(U["f"], days_old=2),
            cf(U["g"]), cf(U["h"], seconds=0, state="error")]


async def test_SC1_only_true_leftovers_are_listed(db, monkeypatch):
    from app.services import stream_cleanup, upload_DO
    keep = upload_DO.PROTECTED_STREAM_UIDS
    try:
        vids = await _world(db)
        out = await stream_cleanup.scan(db, videos=vids, now=NOW)
    finally:
        upload_DO.PROTECTED_STREAM_UIDS = keep
    assert {r["uid"] for r in out["items"]} == {U["e"], U["h"]}
    assert out["videos"] == 8 and out["unused"] == 2
    assert out["unused_minutes"] == 2          # 90 s + 0 s, rounded
    saved = await stream_cleanup.last_scan(db)
    assert saved["unused"] == 2


async def test_SC2_delete_only_what_was_listed_and_still_unused(db, cf_deletes):
    from app.services import stream_cleanup, upload_DO
    keep = upload_DO.PROTECTED_STREAM_UIDS
    try:
        vids = await _world(db)
        await stream_cleanup.scan(db, videos=vids, now=NOW)
        # Since the scan, a post started using "h".
        creator, _ = await make_user(db)
        await make_video(db, creator, stream_uid=U["h"])
        out = await stream_cleanup.delete(db, [U["e"], U["h"], U["a"], "not-a-uid", U["g"]], admin=str(ObjectId()))
    finally:
        upload_DO.PROTECTED_STREAM_UIDS = keep
    assert cf_deletes == [U["e"]]
    assert out["deleted"] == 1 and set(out["skipped"]) == {U["h"], U["a"], "not-a-uid", U["g"]}
    assert (await stream_cleanup.last_scan(db))["unused"] == 1
    log = await db.admin_log.find_one({"action": "stream_cleanup"})
    assert log and log["detail"]["deleted"] == 1


async def test_SC3_a_failed_delete_doesnt_stop_the_rest(db, monkeypatch):
    from app.services import stream_cleanup
    await stream_cleanup.scan(db, videos=[cf(U["e"]), cf(U["f"], days_old=10)], now=NOW)
    gone = []

    def flaky(uid):
        if uid == U["e"]:
            raise RuntimeError("cloudflare 500")
        gone.append(uid)
    monkeypatch.setattr(stream_cleanup, "_delete", flaky)
    out = await stream_cleanup.delete(db, [U["e"], U["f"]], admin=str(ObjectId()))
    assert gone == [U["f"]] and out["failed"] == [U["e"]]
    assert {r["uid"] for r in (await stream_cleanup.last_scan(db))["items"]} == {U["e"]}


async def test_SC4_endpoints_are_admin_only_and_need_confirm(client, db, monkeypatch, cf_deletes):
    from app.services import stream_cleanup
    monkeypatch.setattr(stream_cleanup, "list_all", lambda: [cf(U["e"])])
    _, h = await make_user(db)
    for m, path in (("get", "/api/v1/admin/stream-storage"), ("post", "/api/v1/admin/stream-storage/scan")):
        assert (await getattr(client, m)(path, headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.post("/api/v1/admin/stream-storage/scan", headers=ha)
    assert r.status_code == 200 and r.json()["unused"] == 1
    r = await client.post("/api/v1/admin/stream-storage/delete", headers=ha, json={"uids": [U["e"]], "confirm": False})
    assert r.status_code == 400 and cf_deletes == []
    r = await client.post("/api/v1/admin/stream-storage/delete", headers=ha, json={"uids": [U["e"]], "confirm": True})
    assert r.status_code == 200 and cf_deletes == [U["e"]]
    assert (await client.get("/api/v1/admin/stream-storage", headers=ha)).json()["unused"] == 0


async def test_SC5_switches_listed_and_off(client, db):
    f = (await client.get("/api/v1/config/features")).json()
    assert f["lean_preload"] is False and f["loop_cap"] is False and f["data_saver"] is False


async def test_SC_tv_and_studio_videos_are_never_listed_or_deleted(db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services import stream_cleanup as sc
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    film, ep, offline, draft, junk = ("a" * 32, "b" * 32, "c" * 32, "d" * 32, "e" * 32)
    await db.tv_titles.insert_one({"slug": "x", "cf_uid": film, "download_cf_uid": offline,
                                   "seasons": [{"episodes": [{"cf_uid": ep}]}]})
    await db.studio_drafts.insert_one({"stream_uid": draft})
    vids = [{"uid": u, "created": old, "duration": 60, "status": {"state": "ready"}, "meta": {}}
            for u in (film, ep, offline, draft, junk)]
    out = await sc.scan(db, videos=vids)
    assert [r["uid"] for r in out["items"]] == [junk]
    # Even if an old list named them, delete skips them.
    await db.config.update_one({"_id": sc.STATE_ID}, {"$set": {"items": [sc._row(v) for v in vids]}})
    gone = []
    monkeypatch.setattr(sc, "_delete", lambda uid: gone.append(uid))
    from app.services import admin_log
    async def _rec(*a, **k): return None
    monkeypatch.setattr(admin_log, "record", _rec)
    await sc.delete(db, [film, ep, offline, draft, junk], "6845fdb76cd85e35c8f722f6")
    assert gone == [junk]
