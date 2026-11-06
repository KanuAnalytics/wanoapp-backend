"""Video and comment search. Atlas Search when the index exists, a safe
word match otherwise. Attacks written before the code."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def _search(client, q, **params):
    r = await client.get("/api/v1/videos/search", params={"q": q, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_SE1_finds_words_anywhere_title_and_hashtags_any_case(client, db):
    c, _ = await make_user(db, username="kampala_eats")
    a = await make_video(db, c, description="Best rolex in KAMPALA tonight", title="food")
    b = await make_video(db, c, description="nothing here", title="Kampala sunset")
    h = await make_video(db, c, description="look", hashtags=["Kampala"])
    await make_video(db, c, description="Kampalan? no: kampalaxyz", title="x")      # not the word
    got = {v["_id"] for v in await _search(client, "kampala")}
    assert {a, b, h} <= got
    assert len(got) == 3


async def test_SE2_hidden_posts_never_show(client, db):
    from app.services import moderation
    c, _ = await make_user(db)
    ok = await make_video(db, c, description="matatu ride")
    await make_video(db, c, description="matatu private", privacy="private")
    await make_video(db, c, description="matatu gone", is_active=False)
    await make_video(db, c, description="matatu uploading", isReadyToStream=False)
    await make_video(db, c, description="matatu later", status="scheduled")
    await make_video(db, c, description="matatu future", created_at=datetime.now(timezone.utc) + timedelta(days=30))
    await make_video(db, c, description="matatu held", privacy="private", moderation={"state": moderation.HELD})
    assert [v["_id"] for v in await _search(client, "matatu")] == [ok]


async def test_SE3_user_input_is_never_a_pattern(client, db):
    c, _ = await make_user(db)
    await make_video(db, c, description="price is 5+5 (cheap)")
    for q in ["(.*", "a+", "[", "\\", "$where", "{\"$gt\": \"\"}", ".*", "a" * 200, "  ", "🔥🔥"]:
        r = await client.get("/api/v1/videos/search", params={"q": q})
        assert r.status_code in (200, 422), (q, r.status_code)
        if r.status_code == 200:
            assert all("5+5" not in v.get("description", "") for v in r.json()) or q in ("5+5",)
    assert len(await _search(client, "5+5")) == 1


async def test_SE4_response_shape_is_unchanged(client, db):
    c, _ = await make_user(db, username="maker", display_name="Maker", profile_picture="https://x/p.jpg",
                           email="hide@x.com")
    vid = await make_video(db, c, description="shape check", urls={"thumbnail": "https://x/t.jpg"})
    r = await client.get("/api/v1/videos/search", params={"q": "shape"})
    v = r.json()[0]
    assert set(v) == {"_id", "creator_id", "description", "created_at", "thumbnail", "user"}
    assert v["_id"] == vid and v["user"] == {"username": "maker", "display_name": "Maker",
                                              "profile_picture": "https://x/p.jpg"}
    assert v["created_at"].endswith("Z") and "hide@x.com" not in r.text


async def test_SE5_more_watched_first_and_paging(client, db):
    c, _ = await make_user(db)
    ids = [await make_video(db, c, description=f"boda {i}", views_count=i * 10) for i in range(30)]
    first = await _search(client, "boda", limit=10)
    second = await _search(client, "boda", limit=10, skip=10)
    assert [v["_id"] for v in first] == ids[::-1][:10]
    assert [v["_id"] for v in second] == ids[::-1][10:20]
    assert await _search(client, "boda", skip=100000) == []
    assert (await client.get("/api/v1/videos/search", params={"q": "boda", "limit": 500})).status_code == 422


async def test_SE6_atlas_search_failure_falls_back_and_backs_off(client, db, monkeypatch):
    from app.services import search
    c, _ = await make_user(db)
    vid = await make_video(db, c, description="fallback works")
    monkeypatch.setattr(search, "atlas_enabled", lambda: True)
    search.reset()
    tries = []
    real = search._atlas_videos

    async def counting(*a, **k):
        tries.append(1)
        return await real(*a, **k)
    monkeypatch.setattr(search, "_atlas_videos", counting)
    for _ in range(5):
        assert [v["_id"] for v in await _search(client, "fallback")] == [vid]
    assert len(tries) == 1                 # no index here: tried once, then waits before trying again


async def test_SE7_atlas_pipeline_is_safe_and_filtered(client, db):
    from app.services import search
    p = search.atlas_video_stages("  Kampala   Nights  ")
    s = p[0]["$search"]
    assert s["index"] == search.VIDEO_INDEX
    assert "Kampala Nights" in str(s)
    match = p[1]["$match"]
    assert match["privacy"] == "public" and match["is_active"] is True
    assert search.atlas_video_stages("x" * 1000)[0]["$search"] and len(str(search.atlas_video_stages("x" * 1000))) < 3000


async def test_SE8_atlas_results_used_when_available(client, db, monkeypatch):
    from app.services import search
    c, _ = await make_user(db)
    a = await make_video(db, c, description="one")
    b = await make_video(db, c, description="two")
    monkeypatch.setattr(search, "atlas_enabled", lambda: True)
    search.reset()

    async def fake_atlas(db_, term, skip, limit, tail):
        assert term == "tw"
        rows = await db_.videos.aggregate([{"$match": {"_id": ObjectId(b)}}] + tail).to_list(length=None)
        return rows
    monkeypatch.setattr(search, "_atlas_videos", fake_atlas)
    assert [v["_id"] for v in await _search(client, "tw")] == [b]       # fuzzy/prefix is Atlas's job
    assert a


async def _comment(db, video_id, user_id, content, **extra):
    doc = {"video_id": ObjectId(video_id), "user_id": ObjectId(user_id), "content": content,
           "user_display_name": "Someone", "parent_id": None, "is_active": True,
           "likes_count": 0, "replies_count": 0, "created_at": datetime.utcnow(),
           "updated_at": datetime.utcnow(), **extra}
    return str((await db.comments.insert_one(doc)).inserted_id)


async def test_SC1_comment_search_hides_comments_on_hidden_posts(client, db):
    c, _ = await make_user(db)
    u, h = await make_user(db)
    pub = await make_video(db, c, description="p")
    held = await make_video(db, c, description="h", privacy="private", moderation={"state": "held"})
    gone = await make_video(db, c, description="g", is_active=False)
    ok = await _comment(db, pub, u, "great nyama choma")
    await _comment(db, held, u, "great nyama choma too")
    await _comment(db, gone, u, "great nyama choma three")
    await _comment(db, pub, u, "deleted nyama choma", is_active=False)
    r = await client.get("/api/v1/search", params={"q": "nyama"}, headers=h)
    assert r.status_code == 200 and [x.get("id") or x.get("_id") for x in r.json()] == [ok]


async def test_SC2_comment_search_on_one_post_and_literal(client, db):
    c, _ = await make_user(db)
    u, h = await make_user(db)
    v1 = await make_video(db, c)
    v2 = await make_video(db, c)
    a = await _comment(db, v1, u, "rate 10/10 (.*)")
    await _comment(db, v2, u, "rate 10/10 (.*) again")
    r = await client.get("/api/v1/search", params={"q": "(.*)", "video_id": v1}, headers=h)
    assert [x.get("id") or x.get("_id") for x in r.json()] == [a]
    r = await client.get("/api/v1/search", params={"q": "(.*)", "video_id": "bad"}, headers=h)
    assert r.status_code == 400


async def test_SC3_comment_search_fills_the_page_despite_hidden_ones(client, db):
    c, _ = await make_user(db)
    u, h = await make_user(db)
    pub = await make_video(db, c)
    hidden = await make_video(db, c, privacy="private")
    for i in range(30):
        await _comment(db, hidden, u, f"sukuma {i}", created_at=datetime.utcnow() + timedelta(seconds=i))
    for i in range(10):
        await _comment(db, pub, u, f"sukuma ok {i}", created_at=datetime.utcnow() - timedelta(hours=1, seconds=i))
    r = await client.get("/api/v1/search", params={"q": "sukuma", "limit": 5}, headers=h)
    assert len(r.json()) == 5


async def test_SG1_graphql_search_uses_the_same_rules(client, db):
    c, _ = await make_user(db)
    ok = await make_video(db, c, description="gqlword here")
    await make_video(db, c, description="gqlword hidden", privacy="private", moderation={"state": "held"})
    await make_video(db, c, description="gqlword uploading", isReadyToStream=False)
    q = '{ searchVideos(query: "gqlword") { id } }'
    r = await client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    data = r.json().get("data") or {}
    assert [v["id"] for v in data.get("searchVideos") or []] == [ok], r.text


async def test_SI1_index_script_definitions(client, db):
    from scripts import create_search_indexes as s
    names = {d["name"] for d in s.DEFINITIONS}
    from app.services import search
    assert {search.VIDEO_INDEX, search.COMMENT_INDEX} == names
    vid = next(d for d in s.DEFINITIONS if d["name"] == search.VIDEO_INDEX)
    fields = vid["definition"]["mappings"]["fields"]
    assert {"title", "description", "hashtags", "views_count"} <= set(fields)


# ---------------------------------------------------------------- round 2

from tests.pressure.conftest import REAL_MONGO  # noqa: E402

real_only = pytest.mark.skipif(not REAL_MONGO, reason="needs a real MongoDB (WANO_REAL_MONGO)")


async def test_SR1_hashtag_and_several_words(client, db):
    c, _ = await make_user(db)
    both = await make_video(db, c, description="rolex stand in #Kampala", hashtags=["Kampala"])
    await make_video(db, c, description="rolex in Nairobi")
    assert [v["_id"] for v in await _search(client, "#kampala")] == [both]
    assert [v["_id"] for v in await _search(client, "Rolex  KAMPALA")] == [both]


async def test_SR2_creator_finds_comments_on_own_private_post_others_dont(client, db):
    c, ch = await make_user(db)
    u, uh = await make_user(db)
    priv = await make_video(db, c, privacy="private")
    cid = await _comment(db, priv, u, "secret plantain")
    mine = (await client.get("/api/v1/search", params={"q": "plantain"}, headers=ch)).json()
    theirs = (await client.get("/api/v1/search", params={"q": "plantain"}, headers=uh)).json()
    assert [x.get("id") or x.get("_id") for x in mine] == [cid] and theirs == []


@real_only
async def test_SR3_slow_search_answers_empty_not_500(client, db, monkeypatch):
    from app.services import search
    c, _ = await make_user(db)
    v = await make_video(db, c)
    await db.comments.insert_many([{"video_id": ObjectId(v), "user_id": ObjectId(c), "content": f"x {i}",
                                    "is_active": True, "created_at": datetime.utcnow(), "updated_at": datetime.utcnow()}
                                   for i in range(30000)])
    await db.videos.insert_many([{"creator_id": ObjectId(c), "description": f"y {i}", "is_active": True,
                                  "privacy": "public", "created_at": datetime.utcnow(), "isReadyToStream": True}
                                 for i in range(30000)])
    monkeypatch.setattr(search, "QUERY_MS", 1)
    _, h = await make_user(db)
    r = await client.get("/api/v1/search", params={"q": "nomatchatall"}, headers=h)
    assert r.status_code == 200 and r.json() == []
    r = await client.get("/api/v1/videos/search", params={"q": "nomatchatall"})
    assert r.status_code == 200 and r.json() == []


@real_only
async def test_SR4_word_search_over_a_big_catalogue_is_quick(client, db):
    import time
    c, _ = await make_user(db)
    await db.videos.insert_many([{"creator_id": ObjectId(c), "description": f"clip number {i} #fun",
                                  "title": "t", "hashtags": ["fun"], "views_count": i, "is_active": True,
                                  "privacy": "public", "created_at": datetime.utcnow(), "isReadyToStream": True}
                                 for i in range(20000)])
    t0 = time.perf_counter()
    got = await _search(client, "fun", limit=20)
    assert len(got) == 20 and time.perf_counter() - t0 < 2


async def test_SR5_atlas_comment_failure_falls_back(client, db, monkeypatch):
    from app.services import search
    c, _ = await make_user(db)
    u, h = await make_user(db)
    v = await make_video(db, c)
    cid = await _comment(db, v, u, "kachumbari fresh")
    monkeypatch.setattr(search, "atlas_enabled", lambda: True)
    search.reset()
    for _ in range(3):
        r = await client.get("/api/v1/search", params={"q": "kachumbari"}, headers=h)
        assert [x.get("id") or x.get("_id") for x in r.json()] == [cid]
    assert search.COMMENT_INDEX in search._down_until
