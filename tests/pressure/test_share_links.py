"""Deep pressure tests for share links (/v/{id}, /embed, sitemaps, app-link files).

Attacks: leaking hidden posts, injecting HTML/JS through titles, names and
URLs, breaking out of JSON-LD, pointing the player at another host, poisoning
the sitemap, abusing app-link files, plus load and regression checks."""

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app.core.config import settings
from tests.pressure.conftest import make_user, make_video

pytestmark = pytest.mark.asyncio
UID = "d" * 32
CF = f"https://videodelivery.net/{UID}/manifest/video.m3u8"
SECRET = "TOPSECRETMARKER"


async def _post(db, creator=None, **kw):
    if creator is None:
        creator, _ = await make_user(db, display_name="Ama Creator")
    kw.setdefault("remoteUrl_CF", CF)
    kw.setdefault("title", "Kampala sunset")
    kw.setdefault("description", "Golden hour over the city")
    kw.setdefault("duration", 75)
    return creator, await make_video(db, creator, **kw)


def _scripts(body):
    return re.findall(r"<script[^>]*>", body, re.I)


# ------------------------------------------------------------ happy path

async def test_public_page_has_preview_player_and_google_data(client, db):
    _, vid = await _post(db)
    r = await client.get(f"/v/{vid}")
    assert r.status_code == 200
    b = r.text
    assert 'property="og:title" content="Kampala sunset"' in b
    assert 'property="og:image"' in b and f"{UID}/thumbnails/thumbnail.jpg" in b
    assert 'name="twitter:card" content="player"' in b
    assert f'src="https://iframe.videodelivery.net/{UID}"' in b
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', b, re.S).group(1)
    data = json.loads(ld)
    assert data["@type"] == "VideoObject" and data["duration"] == "PT1M15S" and data["uploadDate"]
    assert "noindex" not in b and "X-Robots-Tag" not in r.headers
    assert r.headers["cache-control"].startswith("public")


async def test_customer_subdomain_player(client, db):
    _, vid = await _post(db, remoteUrl_CF=f"https://customer-abc.cloudflarestream.com/{UID}/manifest/video.m3u8")
    r = await client.get(f"/v/{vid}")
    assert f'src="https://customer-abc.cloudflarestream.com/{UID}/iframe"' in r.text


async def test_photo_post_shows_images_no_player(client, db):
    imgs = ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"]
    _, vid = await _post(db, media_type="photo", images=imgs, remoteUrl_CF=None, urls={"thumbnail": imgs[0]})
    r = await client.get(f"/v/{vid}")
    assert r.status_code == 200
    assert imgs[0] in r.text and imgs[1] in r.text and "<iframe" not in r.text
    assert 'content="summary_large_image"' in r.text
    assert (await client.get(f"/embed/{vid}")).status_code == 404


async def test_head_requests_work(client, db):
    _, vid = await _post(db)
    assert (await client.head(f"/v/{vid}")).status_code == 200
    assert (await client.head(f"/embed/{vid}")).status_code == 200


# ------------------------------------------------------------ privacy

async def test_unlisted_plays_but_is_never_indexed(client, db):
    _, vid = await _post(db, privacy="unlisted")
    r = await client.get(f"/v/{vid}")
    assert r.status_code == 200 and "<iframe" in r.text
    assert 'name="robots" content="noindex, nofollow"' in r.text
    assert "noindex" in r.headers.get("x-robots-tag", "")
    assert "ld+json" not in r.text
    assert r.headers["cache-control"].startswith("private")
    sm = await client.get("/sitemaps/videos-1.xml")
    assert vid not in sm.text


@pytest.mark.parametrize("hidden", [
    {"privacy": "private"},
    {"is_active": False},
    {"status": "scheduled"},
    {"status": "publishing"},
    {"status": "cancelled"},
    {"isReadyToStream": False},
    {"remoteUrl_CF": None},
])
async def test_hidden_posts_look_exactly_like_unknown_ids(client, db, hidden):
    kw = {"title": SECRET, "description": SECRET, **hidden}
    _, vid = await _post(db, **kw)
    r = await client.get(f"/v/{vid}")
    unknown = await client.get(f"/v/{ObjectId()}")
    assert r.status_code == 404
    assert r.text == unknown.text, "a hidden post's page differs from a missing one"
    assert SECRET not in r.text and UID not in r.text and "Ama" not in r.text
    assert (await client.get(f"/embed/{vid}")).status_code == 404


async def test_banned_or_missing_creator_hides_post(client, db):
    banned, _ = await make_user(db, is_active=False)
    _, vid = await _post(db, creator=banned, title=SECRET)
    _, vid2 = await _post(db, creator=str(ObjectId()), title=SECRET)
    for v in (vid, vid2):
        r = await client.get(f"/v/{v}")
        assert r.status_code == 404 and SECRET not in r.text
    assert SECRET not in (await client.get("/sitemaps/videos-1.xml")).text


async def test_page_never_contains_creator_private_fields(client, db):
    creator, _ = await make_user(db, display_name="Ama", email="ama-secret@example.com",
                                 phone_number="+256700999888")
    _, vid = await _post(db, creator=creator)
    b = (await client.get(f"/v/{vid}")).text
    assert "ama-secret@example.com" not in b and "+256700999888" not in b and "1990" not in b


@pytest.mark.parametrize("bad", ["notanid", "../../etc/passwd", "%00", "a" * 5000,
                                 "6aea9b9ef7373ec728e6673", "' OR 1=1 --", "{\"$ne\":null}"])
async def test_garbage_ids_are_clean_404s(client, db, bad):
    r = await client.get(f"/v/{bad}")
    assert r.status_code == 404, (bad, r.status_code)
    r = await client.get(f"/embed/{bad}")
    assert r.status_code == 404


# ------------------------------------------------------------ injection

XSS = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


async def test_title_description_and_name_cannot_inject_html(client, db):
    """Parse the page like a browser: the only elements and attributes must be
    the ones the template writes; user text never becomes markup."""
    from html.parser import HTMLParser

    class Collect(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags, self.attrs = [], []
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.attrs += [k for k, _ in attrs]

    creator, _ = await make_user(db, display_name=XSS)
    _, vid = await _post(db, creator=creator, title=XSS, description=XSS + "</script><script>alert(3)</script>")
    b = (await client.get(f"/v/{vid}")).text
    p = Collect()
    p.feed(b)
    assert p.tags.count("script") == 1, "user text created a <script> tag"
    assert "img" not in p.tags, "user text created an <img> tag"
    assert not [a for a in p.attrs if a.startswith("on")], "event-handler attribute injected"
    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', b, re.S).group(1)
    assert "<" not in ld and ">" not in ld
    assert json.loads(ld)["name"] == XSS  # round-trips exactly, as data


@pytest.mark.parametrize("thumb", [
    "javascript:alert(1)",
    'https://evil.com/a.jpg" onerror="alert(1)',
    "https://user:pw@evil.com/a.jpg",
    "http://insecure.com/a.jpg",
    "data:text/html,<script>alert(1)</script>",
    "https://evil.com/a.jpg><script>alert(1)</script>",
])
async def test_bad_thumbnail_urls_are_dropped(client, db, thumb):
    _, vid = await _post(db, urls={"thumbnail": thumb})
    b = (await client.get(f"/v/{vid}")).text
    assert thumb not in b and "alert(1)" not in b
    assert f"{UID}/thumbnails/thumbnail.jpg" in b  # falls back to the real Stream thumbnail


async def test_photo_post_bad_image_urls_dropped(client, db):
    imgs = ["javascript:alert(1)", "https://ok.example.com/a.jpg", 'https://x.com/"onload=alert(1)']
    _, vid = await _post(db, media_type="photo", images=imgs, remoteUrl_CF=None)
    b = (await client.get(f"/v/{vid}")).text
    assert "javascript:" not in b and "onload=alert" not in b and "https://ok.example.com/a.jpg" in b


async def test_photo_post_with_only_bad_images_is_404(client, db):
    _, vid = await _post(db, media_type="photo", images=["javascript:alert(1)"], remoteUrl_CF=None)
    assert (await client.get(f"/v/{vid}")).status_code == 404


@pytest.mark.parametrize("url", [
    f"https://evil.com/videodelivery.net/{UID}/manifest/video.m3u8",
    f"https://videodelivery.net.evil.com/{UID}/manifest/video.m3u8",
    f"https://evilcloudflarestream.com/{UID}/manifest/video.m3u8",
    f"http://videodelivery.net/{UID}/manifest/video.m3u8",
])
async def test_player_never_points_at_foreign_host(client, db, url):
    _, vid = await _post(db, remoteUrl_CF=url)
    r = await client.get(f"/v/{vid}")
    srcs = re.findall(r'<iframe src="([^"]+)"', r.text)
    assert all(s.startswith(("https://iframe.videodelivery.net/", "https://customer-")) for s in srcs), srcs
    assert "evil" not in r.text


async def test_control_chars_and_huge_titles(client, db):
    _, vid = await _post(db, title="Bad\x00title\x07\n\r" + "x" * 5000, description="\x0b" * 50 + "d" * 9000)
    r = await client.get(f"/v/{vid}")
    assert r.status_code == 200 and "\x00" not in r.text and "\x07" not in r.text
    title = re.search(r'property="og:title" content="([^"]*)"', r.text).group(1)
    assert len(title) <= 100
    ET.fromstring((await client.get("/sitemaps/videos-1.xml")).content)  # still valid XML


async def test_hostile_app_scheme_setting_is_sanitized(client, db, monkeypatch):
    monkeypatch.setattr(settings, "APP_SCHEME", 'javascript:alert(1)//"><b')
    _, vid = await _post(db)
    b = (await client.get(f"/v/{vid}")).text
    assert "javascript:alert" not in b and '"><b' not in b


# ------------------------------------------------------------ headers

async def test_security_headers(client, db):
    _, vid = await _post(db)
    r = await client.get(f"/v/{vid}")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "script-src" not in csp and "default-src 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    e = await client.get(f"/embed/{vid}")
    assert "frame-ancestors *" in e.headers["content-security-policy"]
    assert "x-frame-options" not in e.headers
    assert _scripts(e.text) == []


# ------------------------------------------------------------ sitemap & robots

async def test_sitemap_lists_only_public_indexable_videos(client, db, monkeypatch):
    from app.routes import share
    monkeypatch.setattr(share, "SITEMAP_PAGE_SIZE", 2)
    pub = [(await _post(db, title=f"pub{i}"))[1] for i in range(5)]
    hidden = [
        (await _post(db, privacy="unlisted"))[1], (await _post(db, privacy="private"))[1],
        (await _post(db, is_active=False))[1], (await _post(db, status="scheduled"))[1],
        (await _post(db, media_type="photo", images=["https://c.example.com/a.jpg"], remoteUrl_CF=None))[1],
    ]
    idx = await client.get("/sitemap.xml")
    root = ET.fromstring(idx.content)
    locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert len(locs) == 3
    seen = []
    for n in (1, 2, 3):
        page = await client.get(f"/sitemaps/videos-{n}.xml")
        assert page.status_code == 200 and page.headers["content-type"].startswith("application/xml")
        r = ET.fromstring(page.content)
        seen += [e.text.rsplit("/", 1)[1] for e in r.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert sorted(seen) == sorted(pub), "missing, duplicated or hidden entries"
    assert not set(hidden) & set(seen)
    for bad in (0, -1, 4, 999999):
        assert (await client.get(f"/sitemaps/videos-{bad}.xml")).status_code == 404


async def test_sitemap_escapes_hostile_titles(client, db):
    await _post(db, title="<evil>&\"'</video:title><x>", description="]]><!--")
    page = await client.get("/sitemaps/videos-1.xml")
    root = ET.fromstring(page.content)
    titles = [e.text for e in root.iter("{http://www.google.com/schemas/sitemap-video/1.1}title")]
    assert titles == ["<evil>&\"'</video:title><x>"]


async def test_robots_txt(client, db):
    r = await client.get("/robots.txt")
    assert "Disallow: /api/" in r.text and "Sitemap: " in r.text and "Allow: /v/" in r.text


# ------------------------------------------------------------ app-link files

async def test_app_link_files_absent_until_configured(client, db, monkeypatch):
    monkeypatch.setattr(settings, "IOS_APP_IDS", "")
    monkeypatch.setattr(settings, "ANDROID_SHA256_FINGERPRINTS", "")
    assert (await client.get("/.well-known/apple-app-site-association")).status_code == 404
    assert (await client.get("/.well-known/assetlinks.json")).status_code == 404


async def test_app_link_files_when_configured(client, db, monkeypatch):
    monkeypatch.setattr(settings, "IOS_APP_IDS", "ABCDE12345.com.wanoafrica.app, bad id, <x>")
    monkeypatch.setattr(settings, "ANDROID_PACKAGE", "com.wanoafrica.app")
    fp = ":".join(["AB"] * 32)
    monkeypatch.setattr(settings, "ANDROID_SHA256_FINGERPRINTS", f"{fp},nope")
    r = await client.get("/.well-known/apple-app-site-association")
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    d = r.json()["applinks"]["details"]
    assert d[0]["appIDs"] == ["ABCDE12345.com.wanoafrica.app"] and d[0]["components"][0] == {"/": "/v/*"}
    assert {"/": "/u/*"} in d[0]["components"] and {"/": "/tv/*"} in d[0]["components"]
    a = await client.get("/.well-known/assetlinks.json")
    t = a.json()[0]["target"]
    assert t["package_name"] == "com.wanoafrica.app" and t["sha256_cert_fingerprints"] == [fp]


async def test_bad_android_package_not_served(client, db, monkeypatch):
    monkeypatch.setattr(settings, "ANDROID_PACKAGE", '"; drop')
    monkeypatch.setattr(settings, "ANDROID_SHA256_FINGERPRINTS", ":".join(["AB"] * 32))
    assert (await client.get("/.well-known/assetlinks.json")).status_code == 404


# ------------------------------------------------------------ unlisted by link (API)

async def test_unlisted_opens_by_link_in_api_but_stays_out_of_lists(client, db):
    owner, _ = await make_user(db)
    _, h = await make_user(db)
    vid = await make_video(db, owner, privacy="unlisted", title="unlistedclip")
    priv = await make_video(db, owner, privacy="private", title="privclip")
    assert (await client.get(f"/api/v1/videos/{vid}", headers=h)).status_code == 200
    assert (await client.get(f"/api/v1/videos/{vid}?authenticated=false")).status_code == 200
    assert (await client.get(f"/api/v1/videos/{priv}", headers=h)).status_code == 404
    assert (await client.post(f"/api/v1/videos/{vid}/like", headers=h)).status_code == 200
    for p in ("/api/v1/videos/", "/api/v1/feed/", f"/api/v1/feed/?user_id={owner}"):
        assert "unlistedclip" not in (await client.get(p, headers=h)).text, p
    g = await client.post("/graphql", json={"query": f'{{ video(videoId: "{vid}") {{ title }} }}'})
    assert "unlistedclip" in g.text
    g = await client.post("/graphql", json={"query": f'{{ video(videoId: "{priv}") {{ title }} }}'})
    assert "privclip" not in g.text
    g = await client.post("/graphql", json={"query": "{ videos { edges { node { title } } } }"})
    assert "unlistedclip" not in g.text


# ------------------------------------------------------------ side effects & load

async def test_share_page_counts_no_views(client, db):
    _, vid = await _post(db)
    for _ in range(20):
        await client.get(f"/v/{vid}")
        await client.get(f"/embed/{vid}")
    from app.services.metrics_service import metrics_buffer
    assert metrics_buffer.views_buffer.get(vid, 0) == 0


async def test_burst_of_crawler_requests(client, db):
    ids = [(await _post(db, title=f"t{i}"))[1] for i in range(10)]
    urls = [f"/v/{i}" for i in ids] * 30 + [f"/v/{ObjectId()}" for _ in range(50)] + ["/sitemap.xml"] * 10
    rs = await asyncio.gather(*(client.get(u, headers={"User-Agent": "WhatsApp/2.23"}) for u in urls))
    codes = {r.status_code for r in rs}
    assert codes <= {200, 404}, codes
    assert sum(r.status_code == 200 for r in rs) == 310
