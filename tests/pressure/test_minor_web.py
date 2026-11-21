"""A young person's post never reaches the open web: share page, embed player,
the server's video sitemap, and the website's video page and video sitemap
(the /public endpoints). Adults' posts still show; unlisted ones say so."""

from datetime import datetime

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio


def _born(years):
    now = datetime.utcnow()
    return datetime(now.year - years - 1, 1, 1)


async def _teen_and_adult(db):
    teen, _ = await make_user(db, username="teenkid", display_name="Teen Kid", date_of_birth=_born(14))
    adult, _ = await make_user(db, username="grownup", display_name="Grown Up")
    tv = await make_video(db, teen, title="my dance", description="my dance #fun")
    av = await make_video(db, adult, title="market day", description="market day")
    return teen, tv, av


def _shows_teen(text):
    return "teenkid" in text or "Teen Kid" in text or "my dance" in text


async def test_MW1_share_page_and_embed_hide_a_young_persons_post(client, db):
    _, tv, av = await _teen_and_adult(db)
    r = await client.get(f"/v/{tv}")
    assert r.status_code == 200 and not _shows_teen(r.text)
    assert "noindex" in r.text and "Open this post in Wano" in r.text
    assert r.headers.get("X-Robots-Tag", "").startswith("noindex")
    assert (await client.get(f"/embed/{tv}")).status_code == 404
    r = await client.get(f"/v/{av}")
    assert r.status_code == 200 and "market day" in r.text


async def test_MW2_server_video_sitemap_lists_adults_only(client, db):
    _, tv, av = await _teen_and_adult(db)
    r = await client.get("/sitemaps/videos-1.xml")
    assert r.status_code == 200
    assert tv not in r.text and av in r.text


async def test_MW3_website_video_endpoint(client, db):
    teen, tv, av = await _teen_and_adult(db)
    r = await client.get(f"/api/v1/public/videos/{tv}")
    assert r.status_code == 200 and r.json() == {"_id": tv, "limited": True}
    out = (await client.get(f"/api/v1/public/videos/{av}")).json()
    assert out["limited"] is False and out["user"]["username"] == "grownup" and out["title"] == "market day"
    assert "email" not in str(out) and "date_of_birth" not in out["user"]
    adult = (await db.videos.find_one({"_id": ObjectId(av)}))["creator_id"]
    priv = await make_video(db, str(adult), privacy="private")
    assert (await client.get(f"/api/v1/public/videos/{priv}")).status_code == 404
    unl = await make_video(db, str(adult), privacy="unlisted")
    assert (await client.get(f"/api/v1/public/videos/{unl}")).json()["unlisted"] is True
    assert (await client.get("/api/v1/public/videos/nope")).status_code == 404
    # a banned creator's post is gone
    await db.users.update_one({"_id": adult}, {"$set": {"is_banned": True}})
    assert (await client.get(f"/api/v1/public/videos/{av}")).status_code == 404


async def test_MW4_website_video_sitemap_adults_only_and_hides_email_handles(client, db):
    _, tv, av = await _teen_and_adult(db)
    mail, _ = await make_user(db, username="someone@mail.com", display_name="someone@mail.com")
    mv = await make_video(db, mail, title="hello")
    items = (await client.get("/api/v1/public/sitemap-videos", params={"limit": 50})).json()["items"]
    ids = {i["id"] for i in items}
    assert tv not in ids and av in ids and mv in ids
    assert "@mail.com" not in str(items)
    assert (await client.get("/api/v1/public/sitemap-videos", params={"limit": 500})).status_code == 422
