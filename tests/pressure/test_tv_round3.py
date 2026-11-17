"""Wano TV round 3 (Nov 2026): admin titles editor and numbers, age gate, hearts,
Coming soon + reminders, Tell everyone, clips in the feed, downloads, strip
rules, films on Cloudflare Stream. Attacks first."""

from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_tv_protect import CDN, _title, spaces  # noqa: F401
from tests.pressure.test_for_you import mix_on, rb  # noqa: F401

pytestmark = pytest.mark.asyncio
A = "/api/v1/admin/tv"
M = "/api/v1/tv/more"
UID = "a" * 32
UID2 = "b" * 32


async def _on(db, *keys):
    await db.config.delete_many({"features": {"$exists": True}})
    await db.config.insert_one({"features": {k: True for k in keys}})


class Resp:
    def __init__(self, code=200, data=None, headers=None):
        self.status_code, self._data, self.headers, self.text = code, data, headers or {}, str(data)

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class FakeCf:
    def __init__(self):
        self.calls = []
        self.ready = True
        self.dl = "ready"
        self.fail_token = False

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if method == "POST" and path.startswith("?direct_user"):
            return Resp(201, None, {"stream-media-id": UID2, "Location": "https://upload.example/tus/1"})
        if path.endswith("/token"):
            if self.fail_token:
                return Resp(500, {"success": False})
            return Resp(200, {"success": True, "result": {"token": "tok" + path[1:9]}})
        if path.endswith("/downloads"):
            return Resp(200, {"success": True, "result": {"default": {"status": self.dl, "percentComplete": 100}}})
        if "/captions/" in path:
            return Resp(200, {"success": True, "result": {}})
        return Resp(200, {"success": True, "result": {"readyToStream": self.ready, "duration": 1500,
                                                       "status": {"state": "ready" if self.ready else "inprogress"}}})


@pytest.fixture
def cf(monkeypatch):
    from app.services import tv_cf
    fake = FakeCf()
    monkeypatch.setattr(tv_cf, "_request", fake)
    tv_cf.clear_cache()
    return fake


@pytest.fixture
def pushes(monkeypatch):
    from app.services import growth_notifications as gn
    sent = []

    async def fake(messages):
        sent.extend(messages)
        return True
    monkeypatch.setattr(gn, "_send_batch", fake)
    return sent


@pytest.fixture
def puts(monkeypatch, spaces):  # noqa: F811
    from app.services import tv_files
    def put(key, body, ct):
        try:
            spaces.objects[key] = body.decode()
        except UnicodeDecodeError:
            spaces.objects[key] = body
    monkeypatch.setattr(tv_files, "_put_object", put)
    return spaces


async def admin(db):
    _, h = await make_user(db, user_type="admin")
    return h


def new_title(**kw):
    t = {"slug": "harmattan", "title": "Harmattan", "type": "movie", "genres": ["Drama", "Series"], "year": 2026,
         "release_date": "2026-11-01", "duration_minutes": 14, "age_rating": "16+", "description": "Short",
         "synopsis": "A story.", "poster": f"{CDN}/wano-tv/harmattan/art/p.jpg",
         "backdrop": f"{CDN}/wano-tv/harmattan/art/b.jpg",
         "trailer": {"thumbnail": f"{CDN}/wano-tv/harmattan/art/t.jpg", "video_url": "", "duration_seconds": 0},
         "credits": {"director": "Ama Mensah", "production": "Ondore Media", "distributor": "Wano TV",
                     "cast": ["Kofi"], "people": [{"name": "Ama Mensah", "role": "Director"}]}}
    t.update(kw)
    return t


async def _titles(client, h=None):
    r = await client.get("/api/v1/tv/titles", headers=h or {}, params=None if h else {"authenticated": "false"})
    assert r.status_code == 200, r.text
    return {t["slug"]: t for t in r.json()}


# ---------------------------------------------------------------- attacks

async def test_R1_admin_routes_need_an_admin(client, db):
    _, h = await make_user(db)
    for method, path in (("GET", "/titles"), ("POST", "/titles"), ("GET", "/stats"), ("POST", "/strip-rules"),
                         ("POST", f"/titles/{ObjectId()}/status"), ("POST", f"/titles/{ObjectId()}/notify")):
        r = await client.request(method, A + path, headers=h, json={})
        assert r.status_code in (401, 403), (path, r.status_code)
        assert (await client.request(method, A + path, json={})).status_code in (401, 403)


async def test_R2_switches_start_off(client, db, rb, spaces):  # noqa: F811
    t = await _title(db, "middleton")
    _, h = await make_user(db)
    for method, path in (("PUT", f"/love/{t}"), ("GET", "/coming-soon"), ("PUT", f"/remind/{t}"),
                         ("GET", "/clips"), ("GET", f"/download/{t}")):
        assert (await client.request(method, M + path, headers=h)).status_code == 404, path
    assert (await client.get(f"{M}/me", headers=h)).json() == {"loved": [], "reminders": []}
    one = (await _titles(client, h))["middleton"]
    assert one["loves"] == 0 and one["downloadable"] is False


async def test_R3_hostile_admin_input(client, db, puts):
    h = await admin(db)
    for bad in (new_title(slug="Bad Slug"), new_title(slug="../x"), new_title(poster="javascript:alert(1)"),
                new_title(poster="http://plain.example/p.jpg"), new_title(year=1800), new_title(type="show"),
                new_title(release_date="1 Nov"), new_title(extra=1), new_title(featured="yes"),
                new_title(video_url='https://x.example/a".m3u8'), new_title(title="")):
        assert (await client.post(f"{A}/titles", json=bad, headers=h)).status_code == 422, bad
    r = await client.post(f"{A}/titles", json=new_title(), headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["genres"] == ["Drama"]                     # "Series" is a format, not a genre
    assert (await client.post(f"{A}/titles", json=new_title(), headers=h)).status_code == 409
    tid = r.json()["id"]
    assert (await client.post(f"{A}/titles/{tid}/status", json={"status": "gone"}, headers=h)).status_code == 400
    assert (await client.post(f"{A}/titles/nope/status", json={"status": "live"}, headers=h)).status_code == 400
    assert (await client.post(f"{A}/titles/{ObjectId()}/status", json={"status": "live"}, headers=h)).status_code == 404
    r = await client.post(f"{A}/titles/{tid}/captions", json={"language": "en", "text": "just words"}, headers=h)
    assert r.status_code == 400                                 # no film yet / not subtitles
    assert (await client.post(f"{A}/titles/{tid}/clips", json={"video": "nothing"}, headers=h)).status_code == 400


# ---------------------------------------------------------------- editor

async def test_R4_title_starts_hidden_and_goes_live_only_when_complete(client, db, rb, puts):  # noqa: F811
    h = await admin(db)
    body = new_title(video_url="")
    tid = (await client.post(f"{A}/titles", json=body, headers=h)).json()["id"]
    assert "harmattan" not in await _titles(client)
    r = await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)
    assert r.status_code == 400 and "Needs the film" in r.json()["detail"]
    puts.objects["wano-tv/harmattan/master.m3u8"] = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n720p/index.m3u8\n"
    puts.objects["wano-tv/harmattan/720p/index.m3u8"] = "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXTINF:4.0,\nb.ts\n"
    body["video_url"] = f"{CDN}/wano-tv/harmattan/master.m3u8"
    r = await client.put(f"{A}/titles/{tid}", json=body, headers=h)
    assert r.status_code == 200 and r.json()["problems"] == []
    assert (await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)).status_code == 200
    t = (await _titles(client))["harmattan"]
    assert "/api/v1/tv/stream/" in t["video_url"] and t["genres"] == ["Drama"]
    # An edit that would break a live title is refused and nothing changes.
    r = await client.put(f"{A}/titles/{tid}", json={**body, "poster": ""}, headers=h)
    assert r.status_code == 400
    assert (await db.tv_titles.find_one({"_id": ObjectId(tid)}))["poster"].endswith("p.jpg")
    assert await db.admin_log.count_documents({"action": "tv_title_status"}) == 1


async def test_R5_subtitles_on_a_spaces_film_go_inside_the_stream(client, db, rb, puts):  # noqa: F811
    h = await admin(db)
    puts.objects["wano-tv/harmattan/master.m3u8"] = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n720p/index.m3u8\n"
    puts.objects["wano-tv/harmattan/720p/index.m3u8"] = "#EXTM3U\n#EXTINF:6.0,\na.ts\n#EXTINF:4.0,\nb.ts\n"
    tid = (await client.post(f"{A}/titles", json=new_title(video_url=f"{CDN}/wano-tv/harmattan/master.m3u8"),
                             headers=h)).json()["id"]
    srt = "1\n00:00:01,000 --> 00:00:02,500\nHello\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n"
    for lang, label in (("fr", "Français"), ("en", "English"), ("fr", "French")):
        r = await client.post(f"{A}/titles/{tid}/captions", json={"language": lang, "label": label, "text": srt}, headers=h)
        assert r.status_code == 200, r.text
    caps = r.json()["captions"]
    assert sorted(c["language"] for c in caps) == ["en", "fr"] and {c["label"] for c in caps} == {"English", "French"}
    master = puts.objects["wano-tv/harmattan/master.m3u8"]
    assert master.count('LANGUAGE="fr"') == 1 and 'LANGUAGE="en"' in master and 'SUBTITLES="subs"' in master
    vtt = puts.objects["wano-tv/harmattan/subs/fr/captions.vtt"]
    assert vtt.startswith("WEBVTT\nX-TIMESTAMP-MAP") and "00:00:01.000 --> 00:00:02.500" in vtt
    assert "#EXTINF:10.000," in puts.objects["wano-tv/harmattan/subs/fr/index.m3u8"]
    bad = await client.post(f"{A}/titles/{tid}/captions", json={"language": 'en"', "text": srt}, headers=h)
    assert bad.status_code == 400
    r = await client.delete(f"{A}/titles/{tid}/captions/fr", headers=h)
    assert [c["language"] for c in r.json()["captions"]] == ["en"]
    assert 'LANGUAGE="fr"' not in puts.objects["wano-tv/harmattan/master.m3u8"]


async def test_R6_series_episodes_added_and_played_in_order(client, db, rb, puts, cf):  # noqa: F811
    h = await admin(db)
    tid = (await client.post(f"{A}/titles", json=new_title(slug="roots", type="series", age_rating="All",
                                                             poster=f"{CDN}/wano-tv/roots/art/p.jpg"),
                             headers=h)).json()["id"]
    for n in (2, 1):
        r = await client.put(f"{A}/titles/{tid}/episodes", json={"season": 1, "number": n, "title": f"Ep {n}",
                                                                   "duration_minutes": 20}, headers=h)
        assert r.status_code == 200, r.text
    # Upload episode 1 to Cloudflare; until it's processed the title can't go live.
    up = await client.post(f"{A}/titles/{tid}/film?episode=s1e1", json={"filename": "ep1.mp4", "size": 10 ** 9},
                           headers=h)
    assert up.status_code == 200 and up.json()["uid"] == UID2
    cf.ready = False
    await client.post(f"{A}/titles/{tid}/film/check?episode=s1e1", headers=h)
    p = (await client.get(f"{A}/titles/{tid}", headers=h)).json()["problems"]
    assert any("s1e1" in x for x in p) and any("s1e2" in x for x in p)
    cf.ready = True
    assert (await client.post(f"{A}/titles/{tid}/film/check?episode=s1e1", headers=h)).json()["ready"] is True
    await client.put(f"{A}/titles/{tid}/episodes", json={"season": 1, "number": 2, "title": "Ep 2",
                                                          "video_url": f"{CDN}/wano-tv/other/s1e2/master.m3u8"}, headers=h)
    p = (await client.get(f"{A}/titles/{tid}", headers=h)).json()["problems"]
    assert p == ["Episode s1e2: its film must be in this title's own folder"]
    await client.put(f"{A}/titles/{tid}/episodes", json={"season": 1, "number": 2, "title": "Ep 2",
                                                          "video_url": f"{CDN}/wano-tv/roots/s1e2/master.m3u8"}, headers=h)
    assert (await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)).status_code == 200
    t = (await _titles(client))["roots"]
    eps = t["seasons"][0]["episodes"]
    assert [e["id"] for e in eps] == ["s1e1", "s1e2"] and t["episode_count"] == 2
    assert eps[0]["video_url"].startswith("https://videodelivery.net/tok") and "/api/v1/tv/stream/" in eps[1]["video_url"]
    # Can't take away the last episode of a live series.
    await client.delete(f"{A}/titles/{tid}/episodes/s1e2", headers=h)
    assert (await client.delete(f"{A}/titles/{tid}/episodes/s1e1", headers=h)).status_code == 400
    assert (await client.put(f"{A}/titles/{tid}/episodes", json={"season": 0, "number": 1, "title": "x"},
                             headers=h)).status_code == 422


async def test_R7_cloudflare_film_signed_once_and_never_listed_unsigned(client, db, rb, cf):  # noqa: F811
    h = await admin(db)
    tid = (await client.post(f"{A}/titles", json=new_title(age_rating="All"), headers=h)).json()["id"]
    up = await client.post(f"{A}/titles/{tid}/film", json={"filename": "film.mp4", "size": 2 * 10 ** 9}, headers=h)
    assert up.json()["upload_url"].startswith("https://upload.example/")
    meta = cf.calls[0][2]["headers"]["Upload-Metadata"]
    assert "requiresignedurls" in meta
    assert (await client.post(f"{A}/titles/{tid}/film", json={"filename": "x.mp4", "size": 40 * 1024 ** 3},
                              headers=h)).status_code == 502          # over 30 GB
    await client.post(f"{A}/titles/{tid}/film/check", headers=h)
    doc = await db.tv_titles.find_one({"_id": ObjectId(tid)})
    assert doc["cf_ready"] is True and doc["duration_minutes"] == 14   # kept what the admin typed
    await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)
    for _ in range(3):
        t = (await _titles(client))["harmattan"]
        assert t["video_url"] == f"https://videodelivery.net/tok{UID2[:8]}/manifest/video.m3u8"
    assert sum(1 for c in cf.calls if c[1].endswith("/token")) == 1
    # Cloudflare won't sign: the title drops out rather than handing out a dead link.
    from app.services import tv_cf
    tv_cf.clear_cache()
    cf.fail_token = True
    assert "harmattan" not in await _titles(client)
    # Can't swap the film of a live title.
    assert (await client.post(f"{A}/titles/{tid}/film", json={"filename": "f.mp4", "size": 5}, headers=h)).status_code == 400


# ---------------------------------------------------------------- app features

async def test_R8_age_gate_hides_older_ratings_from_minors_only(client, db, rb, spaces):  # noqa: F811
    await _title(db, "middleton", age_rating="16+")
    await _title(db, "one-field", age_rating="All")
    now = datetime.utcnow()
    _, kid = await make_user(db, date_of_birth=now - timedelta(days=365 * 14 + 20))
    _, teen = await make_user(db, date_of_birth=now - timedelta(days=365 * 16 + 20))
    _, adult = await make_user(db)
    assert set(await _titles(client, kid)) == {"middleton", "one-field"}       # switch off: unchanged
    await _on(db, "tv_age_gate")
    assert set(await _titles(client, kid)) == {"one-field"}
    assert set(await _titles(client, teen)) == {"middleton", "one-field"}
    assert set(await _titles(client, adult)) == {"middleton", "one-field"}
    assert set(await _titles(client)) == {"middleton", "one-field"}            # signed out: as the website
    from app.services import tv_more
    assert [tv_more.min_age(x) for x in ("16+", "18", "PG-13", "All", None, "G", "99+")] == [16, 18, 13, 0, 0, 0, 0]


async def test_R9_hearts_counted_once_per_person(client, db, rb, spaces):  # noqa: F811
    t = await _title(db, "middleton")
    await _on(db, "tv_loves")
    users = [await make_user(db) for _ in range(3)]
    for _, h in users:
        for _ in range(2):
            assert (await client.put(f"{M}/love/{t}", headers=h)).status_code == 200
    r = await client.delete(f"{M}/love/{t}", headers=users[0][1])
    assert r.json() == {"loved": False, "loves": 2}
    assert (await _titles(client))["middleton"]["loves"] == 2
    assert (await client.get(f"{M}/me", headers=users[1][1])).json()["loved"] == [t]
    assert (await client.put(f"{M}/love/{ObjectId()}", headers=users[1][1])).status_code == 404
    assert (await client.put(f"{M}/love/{t}")).status_code == 401


async def test_R10_coming_soon_reminds_once_when_it_goes_live(client, db, rb, puts, pushes):  # noqa: F811
    await _on(db, "tv_coming_soon")
    h = await admin(db)
    puts.objects["wano-tv/harmattan/master.m3u8"] = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n720p/index.m3u8\n"
    tid = (await client.post(f"{A}/titles", json=new_title(video_url=f"{CDN}/wano-tv/harmattan/master.m3u8",
                                                            coming_at="2026-12-20", age_rating="All"),
                             headers=h)).json()["id"]
    _, u1 = await make_user(db, expo_push_tokens=["ExponentPushToken[1]"])
    assert (await client.put(f"{M}/remind/{tid}", headers=u1)).status_code == 404     # not coming soon yet
    await client.post(f"{A}/titles/{tid}/status", json={"status": "coming_soon"}, headers=h)
    cs = (await client.get(f"{M}/coming-soon", params={"authenticated": "false"})); assert cs.status_code == 200, cs.text; cs = cs.json()["items"]
    assert [c["title"] for c in cs] == ["Harmattan"] and cs[0]["coming_at"] == "2026-12-20"
    assert "harmattan" not in await _titles(client)                                   # not playable yet
    for _ in range(2):
        assert (await client.put(f"{M}/remind/{tid}", headers=u1)).status_code == 200
    _, u2 = await make_user(db, expo_push_tokens=["ExponentPushToken[2]"])
    await client.put(f"{M}/remind/{tid}", headers=u2)
    await client.delete(f"{M}/remind/{tid}", headers=u2)
    assert (await client.get(f"{M}/me", headers=u1)).json()["reminders"] == [tid]
    r = await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)
    assert r.status_code == 200 and r.json()["reminders_sent_to"] == 1
    assert [p["to"] for p in pushes] == ["ExponentPushToken[1]"]
    assert pushes[0]["data"] == {"type": "tv_title", "title_id": tid}
    # Hidden and live again: nobody is reminded twice.
    await client.post(f"{A}/titles/{tid}/status", json={"status": "coming_soon"}, headers=h)
    await client.post(f"{A}/titles/{tid}/status", json={"status": "live"}, headers=h)
    assert len(pushes) == 1


async def test_R11_tell_everyone_once_skipping_minors_for_rated_titles(client, db, rb, spaces, pushes):  # noqa: F811
    tid = await _title(db, "middleton", age_rating="16+")
    h = await admin(db)
    await make_user(db, expo_push_tokens=["ExponentPushToken[a]"], localization={"country": "ug"})
    await make_user(db, expo_push_tokens=["ExponentPushToken[k]"],
                    date_of_birth=datetime.utcnow() - timedelta(days=365 * 14))
    await make_user(db)                                                                   # no app notifications
    r = await client.post(f"{A}/titles/{tid}/notify", headers=h)
    assert r.status_code == 200
    assert {p["to"] for p in pushes} <= {"ExponentPushToken[a]"}
    assert (await client.post(f"{A}/titles/{tid}/notify", headers=h)).status_code == 409
    hidden = await _title(db, "one-field", is_active=False)
    assert (await client.post(f"{A}/titles/{hidden}/notify", headers=h)).status_code == 400


async def test_R12_clips_link_posts_to_titles(client, db, spaces):  # noqa: F811
    tid = await _title(db, "middleton")
    h = await admin(db)
    vid = (await db.videos.insert_one({"is_active": True, "title": "clip"})).inserted_id
    r = await client.post(f"{A}/titles/{tid}/clips", json={"video": f"https://wanoafrica.com/v/{vid}"}, headers=h)
    assert r.status_code == 200
    assert (await client.post(f"{A}/titles/{tid}/clips", json={"video": str(ObjectId())}, headers=h)).status_code == 404
    assert (await client.get(f"{M}/clips")).status_code == 404
    await _on(db, "tv_clips")
    clips = (await client.get(f"{M}/clips")).json()["clips"]
    assert clips == {str(vid): {"title_id": tid, "title": "Middleton", "type": "movie", "age_rating": "All"}}
    await client.delete(f"{A}/clips/{vid}", headers=h)
    from app.services import tv_more
    tv_more.clear_cache()
    assert (await client.get(f"{M}/clips")).json()["clips"] == {}


async def test_R13_downloads_signed_rated_and_capped(client, db, rb, puts, cf):  # noqa: F811
    from app.services import tv_more
    await _on(db, "tv_downloads", "tv_age_gate")
    h = await admin(db)
    t = await _title(db, "middleton", age_rating="16+")
    _, u = await make_user(db)
    assert (await client.get(f"{M}/download/{t}", headers=u)).status_code == 404          # no offline copy yet
    bad = await client.post(f"{A}/titles/{t}/offline", json={"filename": "x.exe", "size": 5}, headers=h)
    assert bad.status_code == 400
    off = await client.post(f"{A}/titles/{t}/offline", json={"filename": "middleton.mp4", "size": 10 ** 8}, headers=h)
    assert off.json() == {"kind": "upload", "upload_url": "https://upload.example/tus/1", "uid": UID2}
    assert (await _titles(client, u))["middleton"]["downloadable"] is False             # not processed yet
    cf.ready = False
    assert (await client.post(f"{A}/titles/{t}/film/check", headers=h)).json()["download"]["ready"] is False
    cf.ready = True
    assert (await client.post(f"{A}/titles/{t}/film/check", headers=h)).json()["download"]["ready"] is True
    assert (await _titles(client, u))["middleton"]["downloadable"] is True
    r = await client.get(f"{M}/download/{t}", headers=u)
    assert r.status_code == 200, r.text
    assert r.json()["url"] == f"https://videodelivery.net/tok{UID2[:8]}/downloads/default.mp4"
    _, kid = await make_user(db, date_of_birth=datetime.utcnow() - timedelta(days=365 * 14))
    assert (await client.get(f"{M}/download/{t}", headers=kid)).status_code == 403
    assert (await client.get(f"{M}/download/{t}")).status_code == 401
    for _ in range(tv_more.DOWNLOADS_PER_DAY - 1):
        await client.get(f"{M}/download/{t}", headers=u)
    assert (await client.get(f"{M}/download/{t}", headers=u)).status_code == 429
    # A Cloudflare film: the MP4 is asked for, then handed out through a downloadable token.
    from app.services import tv_cf
    tv_cf.clear_cache()
    t2 = (await client.post(f"{A}/titles", json=new_title(age_rating="All"), headers=h)).json()["id"]
    await client.post(f"{A}/titles/{t2}/film", json={"filename": "f.mp4", "size": 5}, headers=h)
    cf.dl = "inprogress"
    o = await client.post(f"{A}/titles/{t2}/offline", json={"filename": ""}, headers=h)
    assert o.json() == {"kind": "cloudflare", "ready": False, "pct": 100}
    cf.dl = "ready"
    assert (await client.post(f"{A}/titles/{t2}/film/check", headers=h)).json()["download"]["ready"] is True
    await client.post(f"{A}/titles/{t2}/status", json={"status": "live"}, headers=h)
    _, u2 = await make_user(db)
    r = await client.get(f"{M}/download/{t2}", headers=u2)
    assert r.json()["url"] == f"https://videodelivery.net/tok{UID2[:8]}/downloads/default.mp4"
    tok_call = [c for c in cf.calls if c[1].endswith("/token") and c[2]["json"].get("downloadable")]
    assert tok_call


async def test_R14_strip_rules_tuned_within_bounds(client, db):
    h = await admin(db)
    for bad in ({"max_per_session": 500}, {"after_portion": -1}, {"min_posts": "3"}, {"nope": 1}, {"gap_posts": True}):
        assert (await client.post(f"{A}/strip-rules", json=bad, headers=h)).status_code in (400, 422), bad
    r = await client.post(f"{A}/strip-rules", json={"max_per_session": 4, "min_posts": 3, "after_portion": 0.3}, headers=h)
    assert r.status_code == 200 and r.json()["rules"]["max_per_session"] == 4
    from app.services import tv_strip
    rules = await tv_strip.rules(db)
    assert rules["min_posts"] == 3 and rules["gap_posts"] == tv_strip.RULES["gap_posts"]
    await db.tv_settings.update_one({"_id": "strip"}, {"$set": {"max_per_session": 10 ** 6}})
    assert (await tv_strip.rules(db))["max_per_session"] == tv_strip.RULES["max_per_session"]


async def test_R15_numbers_per_title(client, db, rb, spaces):  # noqa: F811
    t = await _title(db, "middleton")
    t2 = await _title(db, "one-field")
    h = await admin(db)
    await _on(db, "tv_plus", "tv_loves")
    users = [await make_user(db) for _ in range(3)]
    for i, (_, uh) in enumerate(users):
        await client.post(f"/api/v1/tv/history/{t}", headers=uh)
        await client.put(f"/api/v1/tv/plus/progress/{t}", json={"position": 600 if i == 0 else 330, "duration": 660},
                         headers=uh)
        await client.put(f"/api/v1/tv/plus/list/{t}", headers=uh)
    await client.put(f"{M}/love/{t}", headers=users[0][1])
    r = (await client.get(f"{A}/stats?days=7", headers=h)).json()
    row = next(x for x in r["items"] if x["id"] == t)
    assert row["plays"] == 3 and row["started"] == 3 and row["watching"] == 3 and row["finished"] == 1
    assert row["my_list"] == 3 and row["loves"] == 1 and 50 <= row["avg_pct"] <= 70
    assert r["items"][0]["id"] == t and r["total_plays"] == 3 and len(r["by_day"]) == 7 and r["people_started"] == 3
    assert next(x for x in r["items"] if x["id"] == t2)["plays"] == 0
    assert (await client.get(f"{A}/stats?days=500", headers=h)).status_code == 422


async def test_R16_pictures_go_through_the_server_checked(client, db, puts):
    h = await admin(db)
    tid = (await client.post(f"{A}/titles", json=new_title(), headers=h)).json()["id"]
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 100
    r = await client.post(f"{A}/titles/{tid}/art-file", files={"file": ("poster.jpg", jpg, "image/jpeg")}, headers=h)
    assert r.status_code == 200 and "/wano-tv/harmattan/art/" in r.json()["url"] and r.json()["url"].endswith(".jpg")
    for name, data in (("p.jpg", b"<html>"), ("p.svg", b"<svg/>"), ("p.png", jpg), ("p.jpg", b"")):
        r = await client.post(f"{A}/titles/{tid}/art-file", files={"file": (name, data, "image/jpeg")}, headers=h)
        assert r.status_code == 400, name
    _, u = await make_user(db)
    r = await client.post(f"{A}/titles/{tid}/art-file", files={"file": ("p.jpg", jpg, "image/jpeg")}, headers=u)
    assert r.status_code in (401, 403)
