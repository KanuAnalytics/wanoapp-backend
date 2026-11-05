"""Pressure tests for the For You feed (Recombee + Wano's own mixing).

A fake Recombee records every request and serves recommendations from a pool
the test controls, so these run without the real service. Each test names the
problem it guards against."""

from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from recombee_api_client.api_requests import (
    AddBookmark, AddCartAddition, Batch, DeleteBookmark, MergeUsers,
    RecommendItemsToUser, RecommendNextItems, SetItemValues, SetUserValues, SetViewPortion,
)

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


class FakeRecombee:
    def __init__(self):
        self.requests = []
        self.pool = []          # item ids served by RecommendItemsToUser, in order
        self.down = False
        self._chains = {}
        self._n = 0

    def of(self, cls):
        out = []
        for r in self.requests:
            if isinstance(r, Batch):
                out += [x for x in r.requests if isinstance(x, cls)]
            elif isinstance(r, cls):
                out.append(r)
        return out

    async def send(self, req):
        self.requests.append(req)
        if self.down:
            raise RuntimeError("recombee down")
        if isinstance(req, RecommendItemsToUser):
            self._n += 1
            rid = f"r{self._n}"
            items = self.pool[: req.count]
            self._chains[rid] = list(items)
            return {"recommId": rid, "recomms": [{"id": i} for i in items]}
        if isinstance(req, RecommendNextItems):
            served = self._chains.get(req.recomm_id, [])
            items = [i for i in self.pool if i not in served][: req.count]
            served += items
            return {"recommId": req.recomm_id, "recomms": [{"id": i} for i in items]}
        if isinstance(req, Batch):
            return [{"code": 200, "json": "ok"} for _ in req.requests]
        return "ok"


@pytest.fixture
def rb(monkeypatch, db):
    fake = FakeRecombee()
    import sys
    for name, mod in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(mod, "recombee_send"):
            monkeypatch.setattr(mod, "recombee_send", fake.send)
    return fake


@pytest.fixture(autouse=True)
def mix_on(monkeypatch):
    """The mixed feed for everyone unless a test sets the rollout itself."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 100, raising=False)


def ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


async def _catalog(db, n=30, creators=3, **kw):
    """n older public videos spread over `creators` creators (Recombee's pool)."""
    cs = [(await make_user(db))[0] for _ in range(creators)]
    ids = []
    for i in range(n):
        ids.append(await make_video(db, cs[i % creators], created_at=ago(days=20 + i),
                                    views_count=1000, isReadyToStream=True, **kw))
    return cs, ids


async def _feed(client, headers=None, **params):
    q = "&".join(f"{k}={v}" for k, v in {"scenario": "for-you", **params}.items())
    return await client.get(f"/api/v1/feed/v2?{q}", headers=headers or {})


# ------------------------------------------------------------ bug 1: anonymous viewers

async def test_B1_anonymous_viewers_get_their_own_profile(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    await _feed(client, {"X-Device-Id": "devA"}, authenticated="false")
    await _feed(client, {"X-Device-Id": "devB"}, authenticated="false")
    users = {r.user_id for r in rb.of(RecommendItemsToUser)}
    assert "temp-user" not in users
    assert len(users) == 2, users


async def test_B1_anonymous_watch_is_reported(client, db, rb):
    _, ids = await _catalog(db, n=2)
    r = await client.post(f"/api/v1/videos/{ids[0]}/watch?authenticated=false",
                          headers={"X-Device-Id": "devA"}, json={"portion": 0.8})
    assert r.status_code == 200, r.text
    views = rb.of(SetViewPortion)
    assert views and views[0].user_id.startswith("anon-"), [v.user_id for v in views]


async def test_B1_device_history_merges_on_login(client, db, rb):
    from app.core.security import get_password_hash
    await make_user(db, username="ama1", password_hash=get_password_hash("Password1"))
    r = await client.post("/api/v1/auth/login/json", headers={"X-Device-Id": "devA"},
                          json={"username": "ama1", "password": "Password1"})
    assert r.status_code == 200, r.text
    merges = rb.of(MergeUsers)
    assert merges and merges[0].source_user_id == "anon-devA"


async def test_B1_hostile_device_id_is_sanitized(client, db, rb):
    _, ids = await _catalog(db, n=3)
    rb.pool = ids
    await _feed(client, {"X-Device-Id": "../../x\"; DROP" + "z" * 500}, authenticated="false")
    uid = rb.of(RecommendItemsToUser)[0].user_id
    assert uid.startswith("anon-") and len(uid) <= 80 and all(c.isalnum() or c in "-_" for c in uid)


# ------------------------------------------------------------ bug 2: invisible videos

async def test_B2_sync_marks_legacy_streamable_videos_ready(client, db, rb):
    creator, _ = await make_user(db)
    legacy = await make_video(db, creator)            # no isReadyToStream field at all
    await db.videos.update_one({"_id": ObjectId(legacy)}, {"$unset": {"isReadyToStream": ""}})
    photo = await make_video(db, creator, media_type="photo", remoteUrl_CF=None,
                             images=["https://c.example.com/a.jpg"])
    private = await make_video(db, creator, privacy="private")
    _, h = await make_user(db, user_type="admin")
    r = await client.post("/api/v1/recombee/sync-items", headers=h)
    assert r.status_code == 200, r.text
    sent = {s.item_id: s.values for s in rb.of(SetItemValues)}
    assert sent[legacy]["is_ready_to_stream"] is True
    assert sent[photo]["is_ready_to_stream"] is True and sent[photo]["media_type"] == "photo"
    assert sent[private]["privacy"] == "private"  # synced so Recombee's copy is accurate


async def test_B2_sync_is_admin_only(client, db, rb):
    _, h = await make_user(db)
    assert (await client.post("/api/v1/recombee/sync-items", headers=h)).status_code == 403


# ------------------------------------------------------------ bug 3: popularity never updated

async def test_B3_engagement_counts_reach_recombee(client, db, rb):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    await client.post(f"/api/v1/videos/{vid}/like", headers=h)
    await client.post(f"/api/v1/videos/{vid}/watch", headers=h, json={"portion": 1})
    from app.services.metrics_service import metrics_buffer
    await metrics_buffer.flush_all()
    from app.services import recommendations
    await recommendations.sync_dirty_item_stats()
    stats = [s for s in rb.of(SetItemValues) if s.item_id == vid and "likes_count" in s.values]
    assert stats and stats[-1].values["likes_count"] >= 1 and stats[-1].values["views_count"] >= 1


# ------------------------------------------------------------ bug 4: variety and repeats

async def test_B4_no_creator_back_to_back_and_max_two_per_page(client, db, rb):
    cs, _ = await _catalog(db, n=0, creators=6)   # 6 creators x 2 = 12 >= 10 slots
    ids = []
    for i in range(20):  # Recombee keeps recommending the same creator
        ids.append(await make_video(db, cs[0], created_at=ago(days=15 + i), isReadyToStream=True))
    for c in cs[1:]:
        for i in range(5):
            ids.append(await make_video(db, c, created_at=ago(days=15 + i), isReadyToStream=True))
    rb.pool = ids
    _, h = await make_user(db)
    r = await _feed(client, h, limit=10)
    creators = [v["creator_id"] for v in r.json()]
    assert len(creators) == 10, r.text
    assert all(a != b for a, b in zip(creators, creators[1:])), creators
    assert max(creators.count(c) for c in set(creators)) <= 2, creators


async def test_B4_already_watched_videos_are_not_served_again(client, db, rb):
    _, ids = await _catalog(db, n=20)
    rb.pool = ids
    uid, h = await make_user(db)
    for v in ids[:5]:
        await client.post(f"/api/v1/videos/{v}/watch", headers=h, json={"portion": 0.9})
    r = await _feed(client, h, limit=10)
    served = {v["id"] for v in r.json()}
    assert not served & set(ids[:5]), served & set(ids[:5])
    assert len(served) == 10


async def test_B4_recombee_asked_for_diversity(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    _, h = await make_user(db)
    await _feed(client, h)
    req = rb.of(RecommendItemsToUser)[0]
    assert isinstance(req.diversity, float) and req.diversity > 0


# ------------------------------------------------------------ bug 5: wrong signals

async def test_B5_comment_is_not_reported_as_bookmark(client, db, rb):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    r = await client.post("/api/v1/", headers=h, json={"video_id": vid, "content": "nice"})
    assert r.status_code == 200, r.text
    assert not rb.of(AddBookmark), "comment sent as a bookmark"
    assert rb.of(AddCartAddition), "comment engagement not reported"
    cid = r.json()["_id"] if "_id" in r.json() else r.json()["id"]
    await client.delete(f"/api/v1/{cid}", headers=h)
    assert not rb.of(DeleteBookmark), "deleting a comment deleted a real bookmark"


async def test_B5_share_and_not_interested_are_reported(client, db, rb):
    creator, _ = await make_user(db)
    vid = await make_video(db, creator)
    _, h = await make_user(db)
    r = await client.post(f"/api/v1/videos/{vid}/share", headers=h)
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/videos/{vid}/not-interested", headers=h)
    assert r.status_code == 200, r.text
    from recombee_api_client.api_requests import AddPurchase, AddRating
    assert rb.of(AddPurchase)
    assert any(x.rating < 0 for x in rb.of(AddRating))


# ------------------------------------------------------------ bug 6: cold start

async def test_B6_interest_tags_reach_recombee_and_shape_new_users(client, db, rb):
    _, ids = await _catalog(db)
    rb.pool = ids
    uid, h = await make_user(db)
    r = await client.put("/api/v1/users/me/tags", headers=h, json={"tags": ["music", "comedy"]})
    assert r.status_code == 200, r.text
    profiles = [s for s in rb.of(SetUserValues) if s.user_id == uid]
    assert profiles and set(profiles[-1].values["tags"]) == {"music", "comedy"}
    await _feed(client, h)
    req = rb.of(RecommendItemsToUser)[-1]
    assert isinstance(req.booster, str) and "tags" in req.booster


# ------------------------------------------------------------ the mixed For You feed

async def _world(db):
    """Recombee pool + a brand-new video + a trending video + a followed creator's post."""
    cs, ids = await _catalog(db, n=40, creators=8)
    fresh_creator, _ = await make_user(db)
    fresh = await make_video(db, fresh_creator, created_at=ago(hours=3), views_count=4, isReadyToStream=True,
                             title="freshclip")
    trend_creator, _ = await make_user(db)
    trending = await make_video(db, trend_creator, created_at=ago(hours=20), views_count=9000,
                                likes_count=2500, comments_count=400, isReadyToStream=True, title="trendclip")
    followed, _ = await make_user(db)
    fol = await make_video(db, followed, created_at=ago(days=2), isReadyToStream=True, title="followclip")
    return ids, fresh, trending, followed, fol


async def test_MIX_page_blends_all_four_sources(client, db, rb):
    ids, fresh, trending, followed, fol = await _world(db)
    rb.pool = ids
    _, h = await make_user(db, following=[ObjectId(followed)])
    r = await _feed(client, h, limit=20)
    page = r.json()
    got = {v["id"] for v in page}
    assert {fresh, trending, fol} <= got, got
    sources = {v.get("source") for v in page}
    assert {"personal", "fresh", "trending", "following"} <= sources, sources
    assert len(page) == 20 and len(got) == 20


async def test_MIX_no_repeats_across_pages(client, db, rb):
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    seen, nxt = [], None
    for _ in range(3):
        params = {"limit": 10, **({"next_recomm_id": nxt} if nxt else {})}
        page = (await _feed(client, h, **params)).json()
        assert page, "page ran dry"
        seen += [v["id"] for v in page]
        nxt = page[-1]["recomm_id"]
    assert len(seen) == len(set(seen)), "a video repeated across pages"


async def test_MIX_survives_recombee_outage(client, db, rb):
    ids, fresh, trending, followed, fol = await _world(db)
    rb.down = True
    _, h = await make_user(db, following=[ObjectId(followed)])
    r = await _feed(client, h, limit=10)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 10, "feed went empty when Recombee failed"


async def test_MIX_never_shows_hidden_videos(client, db, rb):
    creator, _ = await make_user(db)
    hidden = [
        await make_video(db, creator, privacy="private", created_at=ago(hours=1), title="SECRET"),
        await make_video(db, creator, is_active=False, created_at=ago(hours=1), title="SECRET"),
        await make_video(db, creator, privacy="unlisted", created_at=ago(hours=1), title="SECRET"),
        await make_video(db, creator, isReadyToStream=False, created_at=ago(hours=1), title="SECRET"),
    ]
    ids, *_ = await _world(db)
    rb.pool = hidden + ids  # even if Recombee's copy is stale
    _, h = await make_user(db, following=[ObjectId(creator)])
    for _ in range(3):
        r = await _feed(client, h, limit=20)
        assert "SECRET" not in r.text


async def test_MIX_anonymous_and_empty_database_ok(client, db, rb):
    r = await _feed(client, {"X-Device-Id": "devZ"}, authenticated="false")
    assert r.status_code == 200 and r.json() == []


async def test_MIX_featured_video_still_first(client, db, rb):
    ids, fresh, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    page = (await _feed(client, h, video_id=ids[7], limit=10)).json()
    assert page[0]["id"] == ids[7] and len(page) == 10


# ------------------------------------------------------------ A/B rollout and measurement

async def test_AB_rollout_zero_keeps_old_feed(client, db, rb, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 0)
    ids, fresh, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    assert {v.get("variant") for v in page} == {"control"}
    assert all(v.get("source") == "personal" for v in page)


async def test_AB_variant_is_sticky_per_user(client, db, rb, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FOR_YOU_MIX_ROLLOUT", 50)
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    v = {(await _feed(client, h, limit=5)).json()[0]["variant"] for _ in range(4)}
    assert len(v) == 1


async def test_MEASURE_watch_events_feed_the_report(client, db, rb):
    ids, *_ = await _world(db)
    rb.pool = ids
    _, h = await make_user(db)
    page = (await _feed(client, h, limit=10)).json()
    for v in page[:6]:
        await client.post(f"/api/v1/videos/{v['id']}/watch", headers=h,
                          json={"portion": 0.95, "recomm_id": v["recomm_id"]})
    for v in page[6:]:
        await client.post(f"/api/v1/videos/{v['id']}/watch", headers=h,
                          json={"portion": 0.05, "recomm_id": v["recomm_id"]})
    _, ah = await make_user(db, user_type="admin")
    rep = await client.get("/api/v1/metrics/for-you?days=1", headers=ah)
    assert rep.status_code == 200, rep.text
    body = rep.json()
    row = body["variants"][page[0]["variant"]]
    assert row["impressions"] == 10 and row["watched"] == 10
    assert abs(row["completion_rate"] - 0.6) < 0.01 and abs(row["skip_rate"] - 0.4) < 0.01
    assert "by_source" in row
    _, sh = await make_user(db)
    assert (await client.get("/api/v1/metrics/for-you", headers=sh)).status_code == 403
