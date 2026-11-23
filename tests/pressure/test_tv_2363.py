"""Wano TV 2.3.63: true facts (countries, languages), episodes from one film
(time ranges, proposals from scene changes / pauses / subtitles), Clip it,
Finish the Story, playback error details. Attacks first: switches off change
nothing, bad ranges, rated films, teens as winners, other people's limits."""

import asyncio
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from app.services import tv_parts
from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_tv_protect import _title, spaces  # noqa: F401
from tests.pressure.test_tv_round3 import cf, Resp, UID  # noqa: F401

pytestmark = pytest.mark.asyncio
A = "/api/v1/admin/tv"
T = "/api/v1/tv"


async def _on(db, *keys):
    await db.config.delete_many({"features": {"$exists": True}})
    await db.config.insert_one({"features": {k: True for k in keys}})


def _born(years):
    now = datetime.utcnow()
    return datetime(now.year - years, 1, 1) if (now.month, now.day) > (1, 1) else datetime(now.year - years - 1, 1, 1)


async def _get(client, h, tid):
    r = await client.get(f"{T}/titles", headers=h)
    assert r.status_code == 200, r.text
    return next(t for t in r.json() if t["id"] == tid)


# ---------------------------------------------------------------- countries and languages

async def test_W1_countries_and_languages_saved_checked_and_served(client, db, spaces):
    _, ah = await make_user(db, user_type="admin")
    body = {"slug": "yaounde-pc", "title": "Yaoundé", "year": 2026, "countries": ["CM", "cm", " ng "],
            "languages": ["fr"]}
    r = await client.post(f"{A}/titles", json=body, headers=ah)
    assert r.status_code == 201, r.text
    assert r.json()["countries"] == ["cm", "ng"] and r.json()["languages"] == ["fr"]
    bad = await client.post(f"{A}/titles", json={**body, "slug": "x2", "countries": ["Cameroon"]}, headers=ah)
    assert bad.status_code == 422
    # An old admin page (no countries field) doesn't wipe them.
    tid0 = r.json()["id"]
    r = await client.put(f"{A}/titles/{tid0}", json={"slug": "yaounde-pc", "title": "Yaoundé 2", "year": 2026}, headers=ah)
    assert r.status_code == 200 and r.json()["countries"] == ["cm", "ng"] and r.json()["title"] == "Yaoundé 2"
    tid = await _title(db, "middleton", countries=["jm"], languages=["en"])
    _, h = await make_user(db)
    t = await _get(client, h, tid)
    assert t["countries"] == ["jm"] and t["languages"] == ["en"] and t["parts"] == [] and t["story"] is None


# ---------------------------------------------------------------- episodes from one film

def test_W2_choosing_cuts():
    # 60 minutes, a strong cut every 10 minutes, weak noise in between.
    pts = [(600.0 * k, 3) for k in range(1, 6)] + [(300.0 + 600 * k, 1) for k in range(5)]
    ranges = tv_parts.choose(sorted(pts), 3600.0)
    assert [b for _a, b in ranges] == [600.0, 1200.0, 1800.0, 2400.0, 3000.0, 3600.0]
    assert all(tv_parts.MIN_S <= b - a <= tv_parts.MAX_S for a, b in ranges)
    # Short films stay whole; no usable signals: even parts near the target.
    assert tv_parts.choose(pts, 15 * 60) == []
    even = tv_parts.choose([], 90 * 60)
    assert len(even) == 8 and even[-1][1] == 5400.0


def test_W3_reading_ffmpeg_and_subtitles():
    log = ("Duration: 01:30:00.50, start: 0\n[Parsed_showinfo_3 @ 0x1] n:1 pts:1 pts_time:600.5 duration\n"
           "[silencedetect @ 0x2] silence_end: 603.0 | silence_duration: 2.0\n")
    scenes, pauses, length = tv_parts.parse_ffmpeg(log)
    assert scenes == [600.5] and pauses == [602.0] and length == 5400.5
    vtt = "WEBVTT\n\n00:09:55.000 --> 00:09:58.000\nBye\n\n00:10:04.000 --> 00:10:06.000\nHi\n\n00:10:06.500 --> 00:10:08.000\nOk"
    assert tv_parts.dialogue_gaps(vtt) == [601.0]
    c = tv_parts.candidates(scenes, pauses, [601.0])
    assert c == [(600.5, 3)]


async def test_W4_parts_admin_only_checked_and_off_by_default(client, db, spaces):
    tid = await _title(db, "middleton", duration_minutes=60)
    _, h = await make_user(db)
    _, ah = await make_user(db, user_type="admin")
    parts = [{"start": 0, "end": 900, "title": "The storm"}, {"start": 900, "end": 1800}, {"start": 1800, "end": 3600}]
    assert (await client.put(f"{A}/titles/{tid}/parts", json={"parts": parts}, headers=h)).status_code in (401, 403)
    for bad in ([{"start": 0, "end": 900}],                                             # one episode
                [{"start": 0, "end": 900}, {"start": 800, "end": 1800}],                # overlap
                [{"start": 0, "end": 30}, {"start": 30, "end": 1800}],                  # under a minute
                [{"start": 0, "end": 1800}, {"start": 1800, "end": 9000}]):             # past the end
        r = await client.put(f"{A}/titles/{tid}/parts", json={"parts": bad}, headers=ah)
        assert r.status_code == 400, (bad, r.text)
    r = await client.put(f"{A}/titles/{tid}/parts", json={"parts": parts}, headers=ah)
    assert r.status_code == 200 and [p["title"] for p in r.json()["parts"]] == ["The storm", "Episode 2", "Episode 3"]
    assert (await _get(client, h, tid))["parts"] == []                                  # switch off
    await _on(db, "tv_parts")
    got = (await _get(client, h, tid))["parts"]
    assert [(p["number"], p["start"], p["end"]) for p in got] == [(1, 0, 900), (2, 900, 1800), (3, 1800, 3600)]
    r = await client.put(f"{A}/titles/{tid}/parts", json={"parts": []}, headers=ah)
    assert r.json() == {"parts": []} and (await _get(client, h, tid))["parts"] == []


async def test_W5_series_cant_get_parts_and_playback_events_accept_parts(client, db, spaces):
    await _on(db, "tv_parts")
    ep = {"id": "s1e1", "season": 1, "number": 1, "title": "E1", "duration_minutes": 20,
          "video_url": "https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key/wano-tv/e1/master.m3u8"}
    sid = await _title(db, "one-field", type="series", seasons=[{"number": 1, "title": "S1", "episodes": [ep]}])
    _, ah = await make_user(db, user_type="admin")
    two = {"parts": [{"start": 0, "end": 600}, {"start": 600, "end": 1200}]}
    r = await client.put(f"{A}/titles/{sid}/parts", json=two, headers=ah)
    assert r.status_code == 400                                    # a real series keeps its own episodes
    # A "series" that is still one long video (Yaoundé) can be watched as episodes.
    one = await _title(db, "middleton", type="series", duration_minutes=41)
    r = await client.put(f"{A}/titles/{one}/parts", json=two, headers=ah)
    assert r.status_code == 200 and len(r.json()["parts"]) == 2
    _, vh = await make_user(db)
    got = [t for t in (await client.get(f"{T}/titles", headers=vh)).json() if t["id"] == one][0]
    assert len(got["parts"]) == 2
    tid = await _title(db, "middleton", duration_minutes=40,
                       parts=[{"number": 1, "title": "A", "start": 0, "end": 1200}, {"number": 2, "title": "B", "start": 1200, "end": 2400}])
    _, h = await make_user(db)
    ev = {"session": "s" * 20, "kind": "play_start", "episode": "p2", "position": 1300, "duration": 2400}
    assert (await client.post(f"{T}/titles/{tid}/events", json=ev, headers=h)).status_code == 200
    assert (await client.post(f"{T}/titles/{tid}/events", json={**ev, "episode": "p9"}, headers=h)).status_code == 404


async def test_W6_propose_runs_in_the_background_one_at_a_time(client, db, spaces, monkeypatch):
    tid = await _title(db, "middleton", duration_minutes=33)
    _, ah = await make_user(db, user_type="admin")
    gate = asyncio.Event()

    async def fake_ffmpeg(url, audio=True):
        await gate.wait()
        log = "Duration: 00:33:00.00, start: 0\n" + "".join(
            f"pts:1 pts_time:{t}\n" for t in (660.0, 1320.0)) + "silence_end: 661.0 | silence_duration: 1.0\n"
        return 0, log
    monkeypatch.setattr(tv_parts, "_run_ffmpeg", fake_ffmpeg)
    monkeypatch.setattr(tv_parts, "_lowest_variant", lambda u: u)
    monkeypatch.setattr(tv_parts, "_fetch_text", lambda u: "")
    r = await client.post(f"{A}/titles/{tid}/parts/propose", headers=ah)
    assert r.status_code == 200
    other = await _title(db, "one-field", duration_minutes=40)
    assert (await client.post(f"{A}/titles/{other}/parts/propose", headers=ah)).status_code == 409
    gate.set()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not tv_parts.running():
            break
    p = (await client.get(f"{A}/titles/{tid}/parts", headers=ah)).json()["proposal"]
    assert p["state"] == "done" and [(x["start"], x["end"]) for x in p["parts"]] == [(0.0, 660.0), (660.0, 1320.0), (1320.0, 1980.0)]
    assert "scene changes" in p["used"]
    # Nothing is live until saved.
    assert (await db.tv_titles.find_one({"_id": ObjectId(tid)})).get("parts") is None


# ---------------------------------------------------------------- playback errors

async def test_W7_error_details_kept_shown_to_admin_only(client, db, spaces):
    tid = await _title(db, "middleton")
    _, h = await make_user(db)
    ev = {"session": "e" * 20, "kind": "error", "position": 3, "duration": 600,
          "detail": "  The operation couldn't be completed (CoreMediaErrorDomain error -12642.)  ", "platform": "ios"}
    assert (await client.post(f"{T}/titles/{tid}/events", json=ev, headers=h)).status_code == 200
    assert (await client.post(f"{T}/titles/{tid}/events", json={**ev, "session": "f" * 20}, headers=h)).status_code == 200
    old = {k: v for k, v in ev.items() if k not in ("detail", "platform")}          # app 2.3.62
    assert (await client.post(f"{T}/titles/{tid}/events", json={**old, "session": "g" * 20}, headers=h)).status_code == 200
    bad = await client.post(f"{T}/titles/{tid}/events", json={**ev, "platform": "nokia"}, headers=h)
    assert bad.status_code == 422
    _, ah = await make_user(db, user_type="admin")
    errs = (await client.get(f"{A}/playback-stats", headers=ah)).json()["recent_errors"]
    top = errs[0]
    assert top["count"] == 2 and top["platform"] == "ios" and top["detail"].startswith("The operation")
    assert any(e["detail"].startswith("(no message") for e in errs)


# ---------------------------------------------------------------- Clip it

def _clip_cf(cf):
    orig = cf.__call__

    def call(method, path, **kw):
        if path == "/clip":
            cf.calls.append((method, path, kw))
            return Resp(200, {"success": True, "result": {"uid": "c" * 32}})
        return orig(method, path, **kw)
    return call


async def test_W8_clip_it_rules(client, db, cf, monkeypatch):
    from app.services import tv_cf, video_posts

    async def no_recombee(*a, **k):
        return None
    monkeypatch.setattr(video_posts, "recombee_send", no_recombee)
    monkeypatch.setattr(tv_cf, "_request", _clip_cf(cf))
    base = dict(cf_uid=UID, cf_ready=True, video_url=f"https://videodelivery.net/{UID}/manifest/video.m3u8",
                duration_minutes=12, captions=[])
    tid = await _title(db, "middleton", clips_allowed=True, **base)
    rated = await _title(db, "geniuses", clips_allowed=True, age_rating="16+", **base)
    not_allowed = await _title(db, "one-field", **base)
    uid, h = await make_user(db)
    body = {"start": 120, "length": 30, "caption": "this part 😭"}
    assert (await client.post(f"{T}/titles/{tid}/clip", json=body, headers=h)).status_code == 404       # switch off
    await _on(db, "tv_clip_it", "tv_clips")
    assert (await client.post(f"{T}/titles/{tid}/clip", json=body)).status_code == 401
    assert (await client.post(f"{T}/titles/{rated}/clip", json=body, headers=h)).status_code == 403
    assert (await client.post(f"{T}/titles/{not_allowed}/clip", json=body, headers=h)).status_code == 403
    assert (await client.post(f"{T}/titles/{tid}/clip", json={**body, "length": 90}, headers=h)).status_code in (400, 422)
    assert (await client.post(f"{T}/titles/{tid}/clip", json={**body, "start": 12 * 60 + 10}, headers=h)).status_code == 400
    r = await client.post(f"{T}/titles/{tid}/clip", json=body, headers=h)
    assert r.status_code == 201, r.text
    v = await db.videos.find_one({"_id": ObjectId(r.json()["video_id"])})
    assert v["creator_id"] == ObjectId(uid) and v["isReadyToStream"] is False and v["stream_uid"] == "c" * 32
    assert "Middleton" in v["description"] and "this part" in v["description"] and "#WanoTV" in v["description"]
    sent = [kw["json"] for m, p, kw in cf.calls if p == "/clip"][0]
    assert sent["clippedFromVideoUID"] == UID and sent["startTimeSeconds"] == 120 and sent["endTimeSeconds"] == 150
    assert sent["requireSignedURLs"] is False
    clips = (await client.get("/api/v1/tv/more/clips", headers=h)).json()
    assert r.json()["video_id"] in (clips.get("clips") or clips)
    # Two per film per person (a film can't be rebuilt from clips), five a day.
    assert (await client.post(f"{T}/titles/{tid}/clip", json=body, headers=h)).status_code == 201
    r = await client.post(f"{T}/titles/{tid}/clip", json=body, headers=h)
    assert r.status_code == 429 and "this film" in r.json()["detail"]
    others = [await _title(db, "middleton", clips_allowed=True, **base) for _ in range(3)]
    for o in others:
        assert (await client.post(f"{T}/titles/{o}/clip", json=body, headers=h)).status_code == 201
    extra = await _title(db, "middleton", clips_allowed=True, **base)
    r = await client.post(f"{T}/titles/{extra}/clip", json=body, headers=h)
    assert r.status_code == 429 and "a day" in r.json()["detail"]
    # Not longer than 30 seconds, and never the ending (last 15%).
    _, h3 = await make_user(db)
    assert (await client.post(f"{T}/titles/{extra}/clip", json={**body, "length": 45}, headers=h3)).status_code in (400, 422)
    r = await client.post(f"{T}/titles/{extra}/clip", json={**body, "start": 10 * 60 + 20}, headers=h3)
    assert r.status_code == 400 and "ending" in r.json()["detail"]
    # A limited account can't post clips either.
    _, h2 = await make_user(db, mod_state={"state": "limited", "until": datetime.utcnow() + timedelta(days=1)})
    assert (await client.post(f"{T}/titles/{tid}/clip", json=body, headers=h2)).status_code == 403


async def test_W9_clips_allowed_flag_in_catalogue(client, db, spaces, cf):
    base = dict(cf_uid=UID, cf_ready=True, video_url=f"https://videodelivery.net/{UID}/manifest/video.m3u8", captions=[])
    tid = await _title(db, "middleton", clips_allowed=True, **base)
    _, h = await make_user(db)
    assert (await _get(client, h, tid))["clips_allowed"] is False
    await _on(db, "tv_clip_it")
    assert (await _get(client, h, tid))["clips_allowed"] is True


# ---------------------------------------------------------------- Finish the Story

async def test_W10_story_challenge_and_adult_winner(client, db, spaces):
    tid = await _title(db, "middleton")
    now = datetime.utcnow()
    ch = await db.challenges.insert_one({"hashtag": "finishmiddleton", "title": "What happens next?", "description": "",
                                         "starts_at": now - timedelta(days=1), "ends_at": now + timedelta(days=6)})
    _, ah = await make_user(db, user_type="admin")
    _, h = await make_user(db)
    r = await client.put(f"{A}/titles/{tid}/story", json={"challenge_id": str(ch.inserted_id)}, headers=ah)
    assert r.status_code == 200
    assert (await _get(client, h, tid))["story"] is None                              # switch off
    await _on(db, "tv_story")
    st = (await _get(client, h, tid))["story"]
    assert st["hashtag"] == "finishmiddleton" and st["live"] is True and st["winner_video_id"] is None
    teen, _ = await make_user(db, date_of_birth=_born(15))
    tv = await make_video(db, teen)
    r = await client.put(f"{A}/titles/{tid}/story", json={"challenge_id": str(ch.inserted_id), "winner_video_id": tv}, headers=ah)
    assert r.status_code == 400 and "18" in r.json()["detail"]
    adult, _ = await make_user(db, display_name="Amina K")
    av = await make_video(db, adult)
    r = await client.put(f"{A}/titles/{tid}/story", json={"challenge_id": str(ch.inserted_id), "winner_video_id": av}, headers=ah)
    assert r.status_code == 200
    st = (await _get(client, h, tid))["story"]
    assert st["winner_video_id"] == av and st["winner_name"] == "Amina K"
    assert (await client.put(f"{A}/titles/{tid}/story", json={"challenge_id": None}, headers=ah)).json() == {"story": None}
    assert (await _get(client, h, tid))["story"] is None
