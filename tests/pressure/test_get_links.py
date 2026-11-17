"""Creator invite links: wanoafrica.com/get?by=<name> taps (Nov 2026)."""

from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.asyncio


async def test_GL1_one_tap_per_phone_per_link_per_day(client, db):
    def tap(by, platform, who):
        return client.post("/api/v1/get-links/tap", json={"by": by, "platform": platform, "who": who})
    assert (await tap("@Nalu_Comedy", "android", "a")).json() == {"counted": True}
    assert (await tap("nalu_comedy", "android", "a")).json() == {"counted": False}    # same phone, same day
    assert (await tap("nalu_comedy", "ios", "b")).json() == {"counted": True}
    assert (await tap("other", "android", "a")).json() == {"counted": True}           # same phone, other link
    assert (await tap("x<script>", "weird", "c")).json() == {"counted": True}
    from app.services import get_links
    rows = {r["by"]: r for r in await get_links.summary(db)}
    assert rows["nalu_comedy"]["android"] == 1 and rows["nalu_comedy"]["ios"] == 1 and rows["nalu_comedy"]["total"] == 2
    assert rows["xscript"]["other"] == 1
    assert list(rows)[0] == "nalu_comedy"


async def test_GL2_counts_again_the_next_day_and_old_days_drop_out(db):
    from app.services import get_links
    day1 = datetime(2026, 11, 1, 10)
    assert await get_links.record(db, "kato", "android", "p", now=day1)
    assert await get_links.record(db, "kato", "android", "p", now=day1 + timedelta(days=1))
    assert (await get_links.summary(db, days=30, now=day1 + timedelta(days=1)))[0]["total"] == 2
    assert await get_links.summary(db, days=30, now=day1 + timedelta(days=60)) == []


async def test_GL3_no_name_and_bad_input(client, db):
    r = await client.post("/api/v1/get-links/tap", json={})
    assert r.status_code == 200 and r.json()["counted"] is True
    from app.services import get_links
    assert (await get_links.summary(db))[0]["by"] == "none"
    assert get_links.clean_by("A" * 99) == "a" * 40
    assert get_links.clean_by(None) == ""


async def test_GL4_report_is_admin_only(client, db):
    from tests.pressure.conftest import make_user
    _, h = await make_user(db)
    assert (await client.get("/api/v1/admin/get-links", headers=h)).status_code in (401, 403)
    _, ha = await make_user(db, user_type="admin")
    r = await client.get("/api/v1/admin/get-links", headers=ha)
    assert r.status_code == 200 and r.json() == {"days": 30, "rows": []}
