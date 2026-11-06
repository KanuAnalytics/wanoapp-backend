"""Monthly creator cash pool: max(floor, percent of revenue), shared by each creator's
locked-in points that month. Cash accumulates; payout at a minimum, admin-approved.
Attacks written before the code."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user
from tests.pressure.test_points import _month, clock  # noqa: F401

pytestmark = pytest.mark.asyncio

AFTER_OCT = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)


async def _close_october(db, clock, creators):
    """creators: {user_id: views_per_day} each posting 20 days in October."""
    from app.services import points
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    for c, v in creators.items():
        await _month(db, c, 20, views_per_day=v)
    clock["now"] = AFTER_OCT
    await points.settle(db)


async def _cash(db, u):
    from app.services import points
    return (await points.summary(db, ObjectId(u)))["earnings"]["cash_usd"]


async def test_PL1_floor_pool_is_shared_by_points(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    await _close_october(db, clock, {a: 30, b: 10})        # 600 and 200 locked points
    res = await points.distribute(db, "2026-10")
    assert res["pool_usd"] == 200 and res["total_points"] == 800 and res["creators"] == 2
    assert await _cash(db, a) == 150 and await _cash(db, b) == 50


async def test_PL2_revenue_percent_beats_the_floor_only_when_bigger(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.set_pool(db, "2026-10", revenue_usd=500)              # 20% = $100 < $200 floor
    assert (await points.pool_preview(db, "2026-10"))["pool_usd"] == 200
    await points.set_pool(db, "2026-10", revenue_usd=5000)             # 20% = $1,000
    assert (await points.pool_preview(db, "2026-10"))["pool_usd"] == 1000
    await points.set_pool(db, "2026-10", percent=10, floor_usd=50)
    assert (await points.pool_preview(db, "2026-10"))["pool_usd"] == 500


async def test_PL3_cents_add_up_exactly(client, db, clock):
    from app.services import points
    users = [(await make_user(db))[0] for _ in range(7)]
    await _close_october(db, clock, {u: 1 + i for i, u in enumerate(users)})
    await points.distribute(db, "2026-10")
    total = sum([(await db.earnings_wallet.find_one({"_id": ObjectId(u)}))["cents"] for u in users])
    assert total == 20000


async def test_PL4_distribute_once_even_in_parallel(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await asyncio.gather(*[points.distribute(db, "2026-10") for _ in range(5)], return_exceptions=True)
    assert await _cash(db, a) == 200
    with pytest.raises(points.PointsError):
        await points.distribute(db, "2026-10")


async def test_PL5_not_before_the_month_is_closed_and_no_one_eligible(client, db, clock):
    from app.services import points
    clock["now"] = datetime(2026, 11, 15, tzinfo=timezone.utc)
    with pytest.raises(points.PointsError):
        await points.distribute(db, "2026-11")                         # still running
    clock["now"] = AFTER_OCT
    await points.settle(db)
    res = await points.distribute(db, "2026-10")                       # nobody qualified
    assert res["creators"] == 0 and res["paid_usd"] == 0
    with pytest.raises(points.PointsError):
        await points.set_pool(db, "2026-10", revenue_usd=100)          # already distributed


async def test_PL6_expired_months_and_spending_dont_change_the_share(client, db, clock):
    from app.services import points
    a, ah = await make_user(db)
    short, _ = await make_user(db)
    clock["now"] = datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc)
    await _month(db, a, 20, views_per_day=10)
    await _month(db, short, 19, views_per_day=1000)                    # expired: gets nothing
    clock["now"] = AFTER_OCT
    await points.settle(db)
    await db.points_wallet.update_one({"_id": ObjectId(a)}, {"$inc": {"balance": -150}})   # spent on a boost
    await points.distribute(db, "2026-10")
    assert await _cash(db, a) == 200 and await _cash(db, short) == 0


async def test_PL7_automatic_on_day_five(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.settle(db)
    assert await _cash(db, a) == 0                                     # Nov 2: admin still has time
    clock["now"] = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)
    await points.settle(db)
    await points.settle(db)
    assert await _cash(db, a) == 200


async def test_PL8_cash_payout_minimum_and_review(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    rich, rh = await make_user(db)
    poor, ph = await make_user(db)
    await _close_october(db, clock, {rich: 99, poor: 1})               # $198 and $2
    await points.distribute(db, "2026-10")
    assert (await client.post("/api/v1/points/payouts", headers=ph)).status_code == 400   # under $5
    rs = await asyncio.gather(*[client.post("/api/v1/points/payouts", headers=rh) for _ in range(4)])
    ok = [r for r in rs if r.status_code == 200]
    assert len(ok) == 1 and ok[0].json()["usd"] == 198
    assert await _cash(db, rich) == 0
    pid = ok[0].json()["id"]
    lst = (await client.get("/api/v1/points/admin/payouts", headers=ah)).json()
    assert lst[0]["usd"] == 198
    assert (await client.post(f"/api/v1/points/admin/payouts/{pid}/reject", headers=ah)).status_code == 200
    assert await _cash(db, rich) == 198
    assert (await client.post(f"/api/v1/points/admin/payouts/{pid}/approve", headers=ah)).status_code == 409


async def test_PL9_admin_endpoints_and_public_pool(client, db, clock):
    _, ah = await make_user(db, user_type="admin")
    _, uh = await make_user(db)
    assert (await client.put("/api/v1/points/admin/pool/2026-10", headers=uh, json={"revenue_usd": 9})).status_code == 403
    for bad in ({"revenue_usd": -1}, {"percent": 150}, {"floor_usd": -5}):
        assert (await client.put("/api/v1/points/admin/pool/2026-10", headers=ah, json=bad)).status_code == 422
    assert (await client.put("/api/v1/points/admin/pool/2026-13", headers=ah, json={"revenue_usd": 9})).status_code == 422
    r = await client.put("/api/v1/points/admin/pool/2026-11", headers=ah, json={"revenue_usd": 2000})
    assert r.status_code == 200 and r.json()["pool_usd"] == 400
    pub = (await client.get("/api/v1/points/pool", params={"authenticated": "false"})).json()
    assert pub["this_month"]["pool_usd"] == 400 and pub["this_month"]["month"] == "2026-11"
    assert "revenue_usd" not in str(pub)


async def test_PL10_my_page_shows_cash_and_last_share(client, db, clock):
    from app.services import points
    a, h = await make_user(db)
    await _close_october(db, clock, {a: 10})
    await points.distribute(db, "2026-10")
    e = (await client.get("/api/v1/points/me", headers=h)).json()["earnings"]
    assert e["cash_usd"] == 200 and e["last_share"] == {"month": "2026-10", "points": 200, "usd": 200}
    assert e["payout_minimum_usd"] == 5 and e["can_request_payout"] is True


# ---------------------------------------------------------------- round 2

async def test_PR1_posts_marked_not_original_before_sharing_out_dont_get_cash(client, db, clock):
    from app.services import points
    _, ah = await make_user(db, user_type="admin")
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    await _close_october(db, clock, {a: 10, b: 10})
    for doc in await db.videos.find({"creator_id": ObjectId(a)}).to_list(None):
        await client.post(f"/api/v1/points/admin/videos/{doc['_id']}/not-original", headers=ah)
    await points.distribute(db, "2026-10")
    assert await _cash(db, a) == 0 and await _cash(db, b) == 200


async def test_PR2_banned_creators_get_no_share(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    await _close_october(db, clock, {a: 10, b: 10})
    await db.users.update_one({"_id": ObjectId(a)}, {"$set": {"is_active": False}})
    await points.distribute(db, "2026-10")
    assert await _cash(db, a) == 0 and await _cash(db, b) == 200


async def test_PR3_cash_keeps_building_across_months(client, db, clock):
    from app.services import points
    a, _ = await make_user(db)
    b, _ = await make_user(db)
    await _close_october(db, clock, {a: 10, b: 30})
    await points.distribute(db, "2026-10")
    clock["now"] = datetime(2026, 12, 1, 2, 0, tzinfo=timezone.utc)
    await db.points_runs.delete_one({"_id": "day:2026-11-01"})   # 1 Nov was settled before its test data existed
    await _month(db, a, 20, views_per_day=10, month=11)
    clock["now"] = datetime(2026, 12, 2, 4, 0, tzinfo=timezone.utc)
    await points.settle(db)
    await points.distribute(db, "2026-11")
    assert await _cash(db, a) == 250 and await _cash(db, b) == 150
