"""Pressure-test harness: the real FastAPI app on an in-memory MongoDB
(mongomock-motor), with every outside service (Cloudflare, Spaces, Recombee,
SendGrid, Twilio, Expo, SQS) replaced by a recorder. Each test attacks one
finding from the code review; a failing test means the bug is still there."""

import os
import secrets

# Settings needs these to import; real values are never used here.
import re as _re
_cfg = open(os.path.join(os.path.dirname(__file__), "..", "..", "app", "core", "config.py")).read()
for _k in _re.findall(r"^\s+([A-Z_][A-Z0-9_]*):\s*str\s*$", _cfg, _re.M):
    os.environ.setdefault(_k, "test")
os.environ["DO_SPACES_CDN_URL"] = "https://wano-africadev.lon1.cdn.digitaloceanspaces.com"
os.environ["DO_SPACES_BUCKET_NAME"] = "wanoafrica-dospaces-key"
os.environ["DO_SPACES_ENDPOINT"] = "https://lon1.digitaloceanspaces.com"
os.environ.setdefault("SECRET_KEY", "pressure-test-secret")
os.environ.setdefault("DEBUG", "false")

from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core import database as database_mod
from app.core.security import create_access_token


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return None


@pytest.fixture
def calls():
    return {}


REAL_MONGO = os.environ.get("WANO_REAL_MONGO")   # e.g. mongodb://127.0.0.1:27018: run on a real server


@pytest_asyncio.fixture
async def db(monkeypatch, calls):
    client = None
    if REAL_MONGO:
        # A fresh database per test on a real MongoDB, with the app's real indexes
        # (unique, TTL, partial), so behaviour the in-memory mock fakes is tested for real.
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(REAL_MONGO, tz_aware=False)
        mdb = client[f"wano_pt_{ObjectId()}"]
        monkeypatch.setattr(database_mod.db, "db", mdb)
        monkeypatch.setattr(database_mod.db, "client", client, raising=False)
        await database_mod.create_indexes()
        await database_mod.create_comment_indexes(mdb)
        from app.main import ensure_indexes
        await ensure_indexes(mdb)
    else:
        mdb = AsyncMongoMockClient()["wano_pressure"]
        monkeypatch.setattr(database_mod.db, "db", mdb)

    # Outside services -> recorders.
    import app.services.upload_DO as upload_DO
    import app.services.video_deletion as video_deletion

    stream_deletes, spaces_deletes = Recorder(), Recorder()
    monkeypatch.setattr(upload_DO, "delete_stream_video", stream_deletes)
    monkeypatch.setattr(video_deletion, "delete_stream_video", stream_deletes)
    monkeypatch.setattr(upload_DO, "delete_from_spaces", spaces_deletes)
    if hasattr(video_deletion, "delete_from_spaces"):
        monkeypatch.setattr(video_deletion, "delete_from_spaces", spaces_deletes)
    calls["stream_deletes"] = stream_deletes
    calls["spaces_deletes"] = spaces_deletes

    async def _noop_async(*a, **k):
        return None

    import app.services.recombee_service as rs
    monkeypatch.setattr(rs, "recombee_send", _noop_async, raising=False)
    for mod_name in ("app.services.video_deletion", "app.api.v1.videos", "app.api.v1.comments",
                     "app.api.v1.users", "app.api.v1.feed", "app.api.v1.recombee"):
        mod = __import__(mod_name, fromlist=["x"])
        if hasattr(mod, "recombee_send"):
            monkeypatch.setattr(mod, "recombee_send", _noop_async)

    # Push senders are blocking functions in production (requests); the stand-in
    # is too, so code that awaits them breaks here the way it would live.
    import app.services.expo as expo
    pushes = []

    def _push_recorder(name):
        def record(*a, **k):
            pushes.append((name, a, k))
            return None
        return record
    for name in dir(expo):
        if name.startswith("send") and callable(getattr(expo, name)):
            monkeypatch.setattr(expo, name, _push_recorder(name))
    calls["pushes"] = pushes

    from app.services import trending
    trending.clear_cache()
    from app.services import creators
    creators.clear_cache()
    from app.services import for_you
    for_you.clear_cache()
    trending.reset_limits()
    from app.services.metrics_service import metrics_buffer
    metrics_buffer.views_buffer.clear()
    metrics_buffer.likes_buffer.clear()
    metrics_buffer.comments_buffer.clear()
    yield mdb
    if client is not None:
        await client.drop_database(mdb.name)
        client.close()


@pytest_asyncio.fixture
async def client(db):
    from app.main import app
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def make_user(db, *, user_type="standard", **extra):
    doc = {
        "username": extra.pop("username", f"u{ObjectId()}"),
        "email": extra.pop("email", f"{ObjectId()}@example.com"),
        "phone_number": extra.pop("phone_number", None),
        "display_name": "Person",
        "password_hash": extra.pop("password_hash", ""),
        "user_type": user_type,
        "localization": {"country": "ug"},
        "created_at": datetime.now(timezone.utc),
        "is_active": True,
        "is_verified": True,
        "updated_at": datetime.now(timezone.utc),
        "followers_count": 0, "following_count": 0, "videos_count": 0, "likes_count": 0,
        "followers": [], "following": [], "liked_videos": [], "bookmarked_videos": [],
        "blocked_users": [], "blocked_by": [], "features": {}, "theme": None, "tags": [],
        "date_of_birth": datetime(1990, 1, 1), "gender": "female",
    }
    doc.update(extra)
    res = await db.users.insert_one(doc)
    uid = str(res.inserted_id)
    return uid, {"Authorization": f"Bearer {create_access_token({'sub': uid})}"}


async def make_video(db, creator_id, **extra):
    doc = {
        "creator_id": ObjectId(creator_id),
        "title": "clip", "description": "", "video_type": "bits", "privacy": "public",
        "views_count": 0, "likes_count": 0, "comments_count": 0, "bookmarks_count": 0,
        "created_at": datetime.now(timezone.utc), "is_active": True, "is_ready": True,
        "status": "published", "media_type": "video", "images": [], "hashtags": [],
        "remoteUrl_CF": f"https://videodelivery.net/{secrets.token_hex(16)}/manifest/video.m3u8",
        "comments_enabled": True,
    }
    doc.update(extra)
    res = await db.videos.insert_one(doc)
    return str(res.inserted_id)
