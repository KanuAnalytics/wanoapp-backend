"""The points screen showed 00: it led with spendable points (0 until a month
closes) and gave no reason. /points/me now carries progress: earned this
month, today's estimate, and what's blocking earning. Cash isn't touched."""

from datetime import datetime

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


async def test_PP1_progress_explains_a_zero(client, db):
    u, h = await make_user(db)
    await make_video(db, u, isReadyToStream=True)                       # no category
    await make_video(db, u, isReadyToStream=True, category="music")
    p = (await client.get("/api/v1/points/me", headers=h)).json()["progress"]
    assert p["posts_without_category"] == 1 and p["posts_with_category"] == 1
    assert p["posted_today"] is True and p["earned_this_month"] == 0
    assert p["unlocks_at"].startswith(datetime.utcnow().strftime("%Y-%m")[:4])


async def test_PP2_earned_this_month_counts_pending_rows_only_mine(client, db):
    u, h = await make_user(db)
    other, _ = await make_user(db)
    month = datetime.utcnow().strftime("%Y-%m")
    await db.points_ledger.insert_many([
        {"_id": "a", "user_id": ObjectId(u), "kind": "earned", "state": "pending", "amount": 40, "month": month, "at": datetime.utcnow()},
        {"_id": "b", "user_id": ObjectId(u), "kind": "earned", "state": "pending", "amount": 2, "month": month, "at": datetime.utcnow()},
        {"_id": "c", "user_id": ObjectId(u), "kind": "earned", "state": "locked", "amount": 99, "month": "2020-01", "at": datetime.utcnow()},
        {"_id": "d", "user_id": ObjectId(other), "kind": "earned", "state": "pending", "amount": 500, "month": month, "at": datetime.utcnow()},
    ])
    me = (await client.get("/api/v1/points/me", headers=h)).json()
    assert me["progress"]["earned_this_month"] == 42
    assert me["earnings"]["cash_usd"] == 0                       # cash untouched


async def test_PP3_views_today_wait_until_you_post_today(client, db):
    u, h = await make_user(db)
    v = await make_video(db, u, isReadyToStream=True, category="music",
                         created_at=datetime(2026, 1, 1))
    day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    await db.post_daily.insert_one({"_id": f"{v}:x", "video_id": ObjectId(v), "creator_id": ObjectId(u), "day": day,
                                    "pviews": 7, "pfinished": 3})
    p = (await client.get("/api/v1/points/me", headers=h)).json()["progress"]
    assert p["posted_today"] is False and p["today_views_waiting"] == 10 and p["today_estimate"] == 0
