"""Quick posts (quick_posts): Wano Voice notes and Status cards on the ring.

Attacks: switch off means nothing; nobody sees what they shouldn't (blocks,
bans, under-18s, logged out, expired, hidden); nothing but a short m4a voice
note gets stored; limits hold; reports can't be pumped by new accounts; views
can't be pumped; admins only on the review page; files get deleted."""

import struct
from datetime import datetime, timedelta

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user

pytestmark = pytest.mark.asyncio

Q = "/api/v1/quick"
OLD = datetime.utcnow() - timedelta(days=30)


def _box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def m4a(size=20_000, brand=b"M4A ", video=False, ms=12_000, moov_first=False, mvhd_v1=False) -> bytes:
    """A small MP4 laid out like a phone's recording: ftyp, mdat (the sound), moov
    (mvhd with the real length, one trak per stream with its hdlr)."""
    ftyp = _box(b"ftyp", brand + b"\0\0\0\0" + b"M4A isom")
    if mvhd_v1:
        mvhd = _box(b"mvhd", b"\1\0\0\0" + b"\0" * 16 + struct.pack(">IQ", 1000, ms) + b"\0" * 80)
    else:
        mvhd = _box(b"mvhd", b"\0\0\0\0" + b"\0" * 8 + struct.pack(">II", 44100, ms * 441 // 10) + b"\0" * 80)

    def trak(kind):
        hdlr = _box(b"hdlr", b"\0" * 8 + kind + b"\0" * 13)
        return _box(b"trak", _box(b"mdia", hdlr))
    moov = _box(b"moov", mvhd + trak(b"soun") + (trak(b"vide") if video else b""))
    pad = max(0, size - len(ftyp) - len(moov) - 8)
    mdat = _box(b"mdat", b"mvhd hdlr vide " * (pad // 15) + b"\0" * (pad % 15))   # tricky bytes inside the sound
    return ftyp + (moov + mdat if moov_first else mdat + moov)


@pytest.fixture
def store(monkeypatch):
    from app.services import quick_posts
    saved, deleted = {}, []
    state = {"fail_put": False, "fail_delete": False}

    def put(key, data):
        if state["fail_put"]:
            raise RuntimeError("spaces down")
        saved[key] = data

    def delete(key):
        if state["fail_delete"]:
            raise RuntimeError("spaces down")
        deleted.append(key)
    monkeypatch.setattr(quick_posts, "_put", put)
    monkeypatch.setattr(quick_posts, "_delete", delete)
    return {"saved": saved, "deleted": deleted, "state": state}


async def _on(db):
    await db.config.insert_one({"features": {"quick_posts": True}})


async def _card(client, h, text="Shooting in Yaba tomorrow 🎬 who's around?", color="green"):
    return await client.post(f"{Q}/card", json={"text": text, "color": color}, headers=h)


async def _voice(client, h, data=None, ms=12000):
    return await client.post(f"{Q}/voice", files={"file": ("v.m4a", data if data is not None else m4a(), "audio/mp4")},
                             data={"duration_ms": str(ms)}, headers=h)


async def _rings(client, h, *ids):
    r = await client.get(f"{Q}/rings", params={"ids": ",".join(ids)}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["rings"]


async def test_QP1_off_by_default_everything_closed(client, db, store):
    uid, h = await make_user(db)
    _, h2 = await make_user(db)
    assert (await _card(client, h)).status_code == 404
    assert (await _voice(client, h)).status_code == 404
    assert (await client.get(f"{Q}/rings", params={"ids": uid}, headers=h2)).status_code == 404
    assert (await client.get(f"{Q}/users/{uid}", headers=h2)).status_code == 404
    fake = str(ObjectId())
    assert (await client.post(f"{Q}/{fake}/seen", headers=h2)).status_code == 404
    assert (await client.post(f"{Q}/{fake}/report", json={"reason": "spam"}, headers=h2)).status_code == 404
    assert await db.quick_posts.count_documents({}) == 0 and store["saved"] == {}
    feats = (await client.get("/api/v1/config/features")).json()
    assert feats["quick_posts"] is False


async def test_QP2_card_ring_new_then_seen_and_views(client, db, store):
    await _on(db)
    owner, oh = await make_user(db, username="amaka")
    viewer, vh = await make_user(db)
    r = await _card(client, oh)
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["kind"] == "card" and item["color"] == "green" and item["views"] == 0
    assert await _rings(client, vh, owner) == {owner: {"n": 1, "new": True, "voice": False}}
    # Your own ring is never "new" to you.
    assert (await _rings(client, oh, owner))[owner]["new"] is False
    d = (await client.get(f"{Q}/users/{owner}", headers=vh)).json()
    assert d["user"]["username"] == "amaka" and d["mine"] is False
    assert [i["text"] for i in d["items"]] == ["Shooting in Yaba tomorrow 🎬 who's around?"]
    assert "views" not in d["items"][0] and d["items"][0]["seen"] is False
    for _ in range(5):                     # seen many times: one view
        assert (await client.post(f"{Q}/{item['id']}/seen", headers=vh)).status_code == 200
    await client.post(f"{Q}/{item['id']}/seen", headers=oh)      # the owner looking doesn't count
    assert (await _rings(client, vh, owner))[owner]["new"] is False
    mine = (await client.get(f"{Q}/users/{owner}", headers=oh)).json()
    assert mine["mine"] is True and mine["items"][0]["views"] == 1


async def test_QP3_card_text_attacks(client, db, store):
    await _on(db)
    _, h = await make_user(db)
    for bad in ("", "   ", "​‮", "x" * 141, "a\nb\nc\nd\ne\nf\ng",
                "join https://scam.example", "www.free-money.com now", "dm me on wa.me/256700", "win at bit.ly/x",
                "go to cashprize.ng"):
        r = await _card(client, h, bad)
        assert r.status_code == 400, (bad, r.text)
    for body in ({"text": "hi", "color": "red"}, {"text": 5}, {"text": ["hi"]}, {"text": "hi", "extra": 1},
                 {"text": {"$gt": ""}}):
        assert (await client.post(f"{Q}/card", json=body, headers=h)).status_code == 422, body
    # Hidden and direction-flipping characters are dropped; emoji kept.
    r = await _card(client, h, "Hello‮​ world 👨‍👩‍👧")
    assert r.status_code == 201 and r.json()["text"] == "Hello world 👨‍👩‍👧"
    assert await db.quick_posts.count_documents({}) == 1


async def test_QP4_three_at_a_time_and_ten_a_day(client, db, store):
    from app.services import quick_posts
    await _on(db)
    _, h = await make_user(db)
    ids = [(await _card(client, h, f"card {i}")).json()["id"] for i in range(3)]
    r = await _card(client, h, "fourth")
    assert r.status_code == 409 and "3 quick posts at a time" in r.json()["detail"]
    assert (await client.delete(f"{Q}/{ids[0]}", headers=h)).json() == {"deleted": True}
    assert (await _card(client, h, "now fits")).status_code == 201
    made = 4
    while made < quick_posts.PER_DAY:
        for i in (await client.get(f"{Q}/users/{(await db.quick_posts.find_one())['user_id']}", headers=h)).json()["items"]:
            await client.delete(f"{Q}/{i['id']}", headers=h)
        assert (await _card(client, h, f"again {made}")).status_code == 201
        made += 1
    for i in (await client.get(f"{Q}/users/{(await db.quick_posts.find_one())['user_id']}", headers=h)).json()["items"]:
        await client.delete(f"{Q}/{i['id']}", headers=h)
    r = await _card(client, h, "eleventh today")
    assert r.status_code == 429


async def test_QP5_voice_kept_only_when_it_is_a_short_m4a(client, db, store):
    from app.services import quick_posts
    await _on(db)
    uid, h = await make_user(db)
    _, vh = await make_user(db)
    r = await _voice(client, h, m4a(25_000, ms=24_000), 24000)
    assert r.status_code == 201, r.text
    item = r.json()
    (key, data), = store["saved"].items()
    assert key.startswith(f"quick-voice/{uid}/") and key.endswith(".m4a") and len(data) == 25_000
    assert item["audio_url"].endswith(key) and item["audio_url"].startswith("https://")
    assert item["duration_ms"] == 24000
    assert await _rings(client, vh, uid) == {uid: {"n": 1, "new": True, "voice": True}}
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 2000
    for data, ms, code in ((png, 5000, 400), (b"", 5000, 400), (m4a(video=True), 5000, 400),
                           (m4a(brand=b"qt  "), 5000, 400), (m4a(ms=500), 500, 400), (m4a(ms=31_500), 30_000, 400),
                           (m4a(ms=120_000, size=60_000), 30_000, 400),      # says 30 s, really 2 minutes
                           (m4a(), -1, 400), (m4a(), 200, 400),
                           (b"\0\0\0\x18ftypM4A " + b"\0" * 5000, 5000, 400),   # no moov at all
                           (m4a(quick_posts.VOICE_MAX_BYTES + 1), 5000, 413)):
        r = await _voice(client, h, data, ms)
        assert r.status_code == code, (len(data), ms, r.text)
    # The file's own length is what's kept, not the app's number.
    r = await _voice(client, h, m4a(ms=8_000, moov_first=True, mvhd_v1=True), 9_000)
    assert r.status_code == 201 and r.json()["duration_ms"] == 8_000
    r = await client.post(f"{Q}/voice", data={"duration_ms": "5000"}, headers=h)
    assert r.status_code == 422
    r = await client.post(f"{Q}/voice", files={"file": ("v.m4a", m4a(), "audio/mp4")}, data={"duration_ms": "x"}, headers=h)
    assert r.status_code == 422
    assert len(store["saved"]) == 2 and await db.quick_posts.count_documents({}) == 2
    # Storage down: a clear error, nothing half-made, and it doesn't use up one of today's 10.
    store["state"]["fail_put"] = True
    before = (await db.quick_limits.find_one({"_id": {"$regex": f"^make:{uid}"}}))["n"]
    r = await _voice(client, h)
    assert r.status_code == 502 and await db.quick_posts.count_documents({}) == 2
    assert (await db.quick_limits.find_one({"_id": {"$regex": f"^make:{uid}"}}))["n"] == before


async def test_QP6_under_18s_cards_only_and_followers_only(client, db, store):
    await _on(db)
    teen_dob = datetime.utcnow() - timedelta(days=15 * 365)
    teen, th = await make_user(db, date_of_birth=teen_dob)
    r = await _voice(client, th)
    assert r.status_code == 403 and "18" in r.json()["detail"] and store["saved"] == {}
    qid = (await _card(client, th, "exam tomorrow 📚")).json()["id"]
    stranger, sh = await make_user(db)
    follower, fh = await make_user(db, following=[ObjectId(teen)])
    assert await _rings(client, sh, teen) == {}
    assert (await client.get(f"{Q}/users/{teen}", headers=sh)).json() == {"user": None, "items": [], "mine": False}
    assert (await client.post(f"{Q}/{qid}/seen", headers=sh)).status_code == 404
    assert (await client.post(f"{Q}/{qid}/report", json={"reason": "spam"}, headers=sh)).status_code == 404
    assert (await _rings(client, fh, teen))[teen]["n"] == 1
    assert (await _rings(client, th, teen))[teen]["n"] == 1        # they still see their own


async def test_QP7_blocks_either_way_and_bans_hide_everything(client, db, store):
    await _on(db)
    owner, oh = await make_user(db)
    qid = (await _card(client, oh)).json()["id"]
    a, ah = await make_user(db, blocked_users=[ObjectId(owner)])
    b, bh = await make_user(db, blocked_by=[ObjectId(owner)])
    c, ch = await make_user(db)
    await db.users.update_one({"_id": ObjectId(owner)}, {"$push": {"blocked_users": ObjectId(c)}})
    for h in (ah, bh, ch):
        assert await _rings(client, h, owner) == {}
        assert (await client.get(f"{Q}/users/{owner}", headers=h)).json()["items"] == []
        assert (await client.post(f"{Q}/{qid}/seen", headers=h)).status_code == 404
    _, vh = await make_user(db)
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
    await db.users.update_one({"_id": ObjectId(owner)}, {"$set": {"is_active": False}})
    assert await _rings(client, vh, owner) == {}
    # A banned account can't post either.
    assert (await _card(client, oh)).status_code in (401, 403)


async def test_QP8_gone_after_24h_and_files_deleted(client, db, store):
    from app.services import quick_posts
    await _on(db)
    owner, oh = await make_user(db)
    _, vh = await make_user(db)
    v1 = (await _voice(client, oh)).json()["id"]
    v2 = (await _voice(client, oh)).json()["id"]
    await db.quick_posts.update_one({"_id": ObjectId(v1)}, {"$set": {"expires_at": datetime.utcnow() - timedelta(minutes=1)}})
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
    assert [i["id"] for i in (await client.get(f"{Q}/users/{owner}", headers=vh)).json()["items"]] == [v2]
    assert (await client.post(f"{Q}/{v1}/seen", headers=vh)).status_code == 404
    k1 = (await db.quick_posts.find_one({"_id": ObjectId(v1)}))["audio_key"]
    store["state"]["fail_delete"] = True
    await quick_posts.purge(db)
    assert store["deleted"] == [] and not (await db.quick_posts.find_one({"_id": ObjectId(v1)})).get("purged")
    store["state"]["fail_delete"] = False
    assert await quick_posts.purge(db) == 1
    assert store["deleted"] == [k1] and (await db.quick_posts.find_one({"_id": ObjectId(v1)}))["purged"] is True
    assert await quick_posts.purge(db) == 0                      # nothing twice
    # Deleting your own removes its file straight away.
    k2 = (await db.quick_posts.find_one({"_id": ObjectId(v2)}))["audio_key"]
    await client.delete(f"{Q}/{v2}", headers=oh)
    assert store["deleted"] == [k1, k2] and await _rings(client, vh, owner) == {}


async def test_QP9_reports_hide_after_three_established_accounts(client, db, store):
    await _on(db)
    owner, oh = await make_user(db, username="loud")
    _, vh = await make_user(db)
    qid = (await _voice(client, oh)).json()["id"]
    assert (await client.post(f"{Q}/{qid}/report", json={"reason": "spam"}, headers=oh)).status_code == 400
    assert (await client.post(f"{Q}/{qid}/report", json={"reason": "made_up"}, headers=vh)).status_code == 422
    # Brand-new accounts can report, but can't hide it.
    for _ in range(5):
        _, nh = await make_user(db)
        assert (await client.post(f"{Q}/{qid}/report", json={"reason": "hate"}, headers=nh)).json() == {"reported": True}
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
    trusted = [(await make_user(db, created_at=OLD))[1] for _ in range(3)]
    for h in trusted[:2]:
        for _ in range(3):              # the same person reporting again counts once
            await client.post(f"{Q}/{qid}/report", json={"reason": "harassment"}, headers=h)
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
    await client.post(f"{Q}/{qid}/report", json={"reason": "harassment"}, headers=trusted[2])
    assert await _rings(client, vh, owner) == {}
    _, ah = await make_user(db, user_type="admin")
    assert (await client.get("/api/v1/admin/quick-reports", headers=vh)).status_code == 403
    assert (await client.post(f"/api/v1/admin/quick-reports/{qid}", json={"action": "remove"}, headers=vh)).status_code == 403
    rows = (await client.get("/api/v1/admin/quick-reports", headers=ah)).json()["items"]
    assert len(rows) == 1 and rows[0]["hidden"] is True and rows[0]["reports"] == 8
    assert rows[0]["owner"]["username"] == "loud" and set(rows[0]["reasons"]) == {"hate", "harassment"}
    # Restore: back on the ring.
    assert (await client.post(f"/api/v1/admin/quick-reports/{qid}", json={"action": "restore"}, headers=ah)).status_code == 200
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
    assert (await client.get("/api/v1/admin/quick-reports", headers=ah)).json()["items"] == []
    # Reported again and removed: gone for good, file deleted.
    _, h4 = await make_user(db, created_at=OLD)
    await client.post(f"{Q}/{qid}/report", json={"reason": "scam"}, headers=h4)
    r = await client.post(f"/api/v1/admin/quick-reports/{qid}", json={"action": "remove"}, headers=ah)
    assert r.status_code == 200 and len(store["deleted"]) == 1
    assert await _rings(client, vh, owner) == {}
    assert (await client.post(f"/api/v1/admin/quick-reports/{ObjectId()}", json={"action": "remove"}, headers=ah)).status_code == 404


async def test_QP10_only_the_owner_deletes(client, db, store):
    await _on(db)
    owner, oh = await make_user(db)
    _, xh = await make_user(db)
    qid = (await _card(client, oh)).json()["id"]
    for bad in (qid, "nope", "$where", str(ObjectId())):
        assert (await client.delete(f"{Q}/{bad}", headers=xh)).status_code == 404
    assert (await client.delete(f"{Q}/{qid}", headers=oh)).status_code == 200
    assert (await client.delete(f"{Q}/{qid}", headers=oh)).status_code == 404
    # Works with the switch off too, so nothing gets stuck up.
    await _on(db)
    qid2 = (await _card(client, oh)).json()["id"]
    await db.config.insert_one({"features": {"quick_posts": False}})
    assert (await client.delete(f"{Q}/{qid2}", headers=oh)).status_code == 200


async def test_QP11_junk_ids_and_logged_out(client, db, store):
    await _on(db)
    owner, oh = await make_user(db)
    await _card(client, oh)
    _, h = await make_user(db)
    junk = ["", "x", "$where", "{\"$gt\":1}", "a" * 24, "0" * 25] + [str(ObjectId()) for _ in range(50)]
    assert await _rings(client, h, *junk) == {}
    assert await _rings(client, h, *(junk[:6] + [owner])) != {}
    r = await client.get(f"{Q}/rings", params={"ids": ",".join([str(ObjectId())] * 100)}, headers=h)
    assert r.status_code == 422                                   # too long to even read
    for bad in ("nope", "$where", str(ObjectId())):
        r = await client.get(f"{Q}/users/{bad}", headers=h)
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.json()["items"] == []
    for method, url, kw in (("GET", f"{Q}/rings", {"params": {"ids": owner}}), ("GET", f"{Q}/users/{owner}", {}),
                            ("POST", f"{Q}/card", {"json": {"text": "hi"}}), ("POST", f"{Q}/{owner}/seen", {})):
        assert (await client.request(method, url, **kw)).status_code == 401, url
        r = await client.request(method, url, params={**kw.get("params", {}), "authenticated": "false"},
                                 **{k: v for k, v in kw.items() if k != "params"})
        assert r.status_code == 401, url


async def test_QP12_rings_for_a_whole_screen_is_two_small_reads(client, db, store):
    """60 people on screen, each with 3 posts: one reply, only who has something."""
    await _on(db)
    _, vh = await make_user(db)
    ids = []
    for i in range(20):
        uid, h = await make_user(db)
        ids.append(uid)
        if i % 2 == 0:
            await _card(client, h, f"from {i}")
    rings = await _rings(client, vh, *ids)
    assert sorted(rings) == sorted(ids[::2]) and all(r["n"] == 1 and r["new"] for r in rings.values())


def test_QP13_m4a_check():
    from app.services.quick_posts import is_m4a
    assert is_m4a(m4a()) and is_m4a(m4a(brand=b"mp42")) and is_m4a(m4a(brand=b"isom"))
    assert not is_m4a(m4a(video=True)) and not is_m4a(b"ftyp") and not is_m4a(b"\0" * 100)
    assert not is_m4a(b"\0\0\0\x18ftypqt  " + b"\0" * 40)


async def test_QP14_sent_at_the_same_moment_still_three(client, db, store, monkeypatch):
    """Four sent together all pass the first count; only three stay, and the
    extra voice file is deleted rather than left in storage."""
    from app.services import quick_posts

    async def no_check(db_, me):
        return None
    await _on(db)
    monkeypatch.setattr(quick_posts, "_room", no_check)
    uid, h = await make_user(db)
    codes = [(await _card(client, h, f"c{i}")).status_code for i in range(3)]
    r = await _voice(client, h)
    assert codes == [201, 201, 201] and r.status_code == 409
    assert await db.quick_posts.count_documents({}) == 3
    assert len(store["saved"]) == 1 and store["deleted"] == list(store["saved"])


def test_QP15_real_recordings_and_lengths():
    """Files laid out the way iPhones (moov last or first) and Android phones write them."""
    from app.services.quick_posts import is_m4a, m4a_duration_ms
    assert m4a_duration_ms(m4a(ms=12_000)) == 12_000
    assert m4a_duration_ms(m4a(ms=8_000, moov_first=True)) == 8_000
    assert m4a_duration_ms(m4a(ms=29_000, mvhd_v1=True)) == 29_000
    assert is_m4a(m4a(moov_first=True)) and not is_m4a(m4a(video=True, moov_first=True))
    # Junk sizes inside the file don't crash or loop.
    for junk in (b"\0\0\0\x18ftypM4A \0\0\0\0M4A isom" + b"\xff\xff\xff\xffmoov" + b"\0" * 50,
                 b"\0\0\0\x18ftypM4A \0\0\0\0M4A isom" + b"\0\0\0\x01moov" + b"\0" * 4,
                 b"\0\0\0\x18ftypM4A \0\0\0\0M4A isom" + b"\0\0\0\x00moov" + b"\0\0\0\x05mvhd"):
        assert is_m4a(junk) is False and m4a_duration_ms(junk) is None


async def test_QP16_card_text_that_hides_or_spills(client, db, store):
    await _on(db)
    _, h = await make_user(db)
    for bad in ("‍" * 20, "‍‍ ‍", "́́́", "   \n\n  ",
                "check scam.cm", "pay at money.gh now", "visit bet9ja.bet", "promo.shop"):
        r = await _card(client, h, bad)
        assert r.status_code == 400, (repr(bad), r.text)
    # Ordinary sentences with full stops are fine.
    for ok in ("Done. In Lagos tomorrow", "We won 2.5 million!", "Mr. Shop owner said hi"):
        r = await _card(client, h, ok)
        assert r.status_code == 201, (ok, r.text)
        await client.delete(f"{Q}/{r.json()['id']}", headers=h)
    # "Glitch text": piles of accent marks are cut down so they can't spill off the card.
    zalgo = "H" + "̶̷̸́̂̃" * 10 + "i"
    r = await _card(client, h, zalgo)
    assert r.status_code == 201 and len(r.json()["text"]) == 1 + 3 + 1


async def test_QP17_a_hidden_one_doesnt_take_your_place(client, db, store):
    """Three reports hide one; its owner can't see it to delete it, so it mustn't
    stop them posting."""
    await _on(db)
    owner, oh = await make_user(db)
    ids = [(await _card(client, oh, f"c{i}")).json()["id"] for i in range(3)]
    for _ in range(3):
        _, th = await make_user(db, created_at=OLD)
        await client.post(f"{Q}/{ids[0]}/report", json={"reason": "spam"}, headers=th)
    mine = (await client.get(f"{Q}/users/{owner}", headers=oh)).json()["items"]
    assert [i["id"] for i in mine] == ids[1:]
    assert (await _card(client, oh, "new one")).status_code == 201


async def test_QP18_old_records_with_text_ids_still_follow_the_rules(client, db, store):
    """Some old users have ids stored as text in following / blocked lists."""
    await _on(db)
    teen, th = await make_user(db, date_of_birth=datetime.utcnow() - timedelta(days=16 * 365))
    await _card(client, th, "hi")
    fan, fh = await make_user(db, following=[teen])                  # text id
    assert (await _rings(client, fh, teen))[teen]["n"] == 1
    owner, oh = await make_user(db)
    await _card(client, oh, "hello")
    blocker, bh = await make_user(db, blocked_users=[owner])         # text id
    assert await _rings(client, bh, owner) == {}


async def test_QP19_admin_isnt_handed_a_dead_voice_link(client, db, store):
    from app.services import quick_posts
    await _on(db)
    owner, oh = await make_user(db)
    qid = (await _voice(client, oh)).json()["id"]
    _, vh = await make_user(db, created_at=OLD)
    await client.post(f"{Q}/{qid}/report", json={"reason": "hate"}, headers=vh)
    await db.quick_posts.update_one({"_id": ObjectId(qid)}, {"$set": {"expires_at": datetime.utcnow() - timedelta(hours=1)}})
    await quick_posts.purge(db)
    _, ah = await make_user(db, user_type="admin")
    row, = (await client.get("/api/v1/admin/quick-reports", headers=ah)).json()["items"]
    assert row["audio_url"] is None


async def test_QP20_viewer_following_thousands_is_still_one_cheap_read(client, db, store):
    await _on(db)
    owner, oh = await make_user(db)
    await _card(client, oh, "hey")
    big = [ObjectId() for _ in range(5000)]
    _, vh = await make_user(db, following=big)
    assert (await _rings(client, vh, owner))[owner]["n"] == 1
