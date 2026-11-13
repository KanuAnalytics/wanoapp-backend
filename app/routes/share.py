"""Public share links for posts, like YouTube's watch pages.

    GET /v/{video_id}          watch page: plays the post, link-preview tags
                               (WhatsApp, iMessage, Facebook, X), Google video
                               data, and "Open in Wano" / store buttons
    GET /embed/{video_id}      bare player, for X's player card and embeds
    GET /u/{user_id}           profile link: name, photo, "Open in Wano" (no photo
                               or name for under-18s; never indexed)
    GET /tv/{title_id}         Wano TV title link: poster, title, "Open in Wano"
    GET /app                   plain "Get Wano" page (Home Ground, milestones)
    GET /pages/feed?videoId=…  links older apps sent: sent on to the pages above
    GET /sitemap.xml           sitemap index
    GET /sitemaps/videos-{n}.xml   Google video sitemap pages (public posts only)
    GET /robots.txt
    GET /.well-known/apple-app-site-association   iOS Universal Links
    GET /.well-known/assetlinks.json              Android App Links

Privacy:
  public    preview + plays + listed on Google
  unlisted  preview + plays for anyone with the link, never indexed
  private / deleted / scheduled / still processing / banned creator
            the same generic "not available" page as an unknown ID, so a link
            never reveals whether a hidden post exists or anything about it.

Everything that came from users (titles, descriptions, names, URLs) is
escaped before it reaches HTML, XML or JSON-LD. The pages run no scripts.
"""

import html
import json
import math
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from bson import ObjectId
from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from app.core.config import settings
from app.core.database import get_database
from app.services.scheduled_publisher import CANCELLED, PUBLISHING, SCHEDULED
from app.services.upload_DO import extract_stream_uid
from app.services.video_posts import DEFAULT_THUMBNAIL

router = APIRouter(tags=["share links"])

SITEMAP_PAGE_SIZE = 5000
_HIDDEN_STATUSES = (SCHEDULED, PUBLISHING, CANCELLED)
_STREAM_HOSTS = ("videodelivery.net",)
_STREAM_HOST_SUFFIX = ".cloudflarestream.com"
_UID_RE = re.compile(r"^[a-f0-9]{32}$")
_TITLE_MAX, _DESC_MAX = 100, 200


# ------------------------------------------------------------------ helpers

def _base() -> str:
    return settings.SHARE_BASE_URL.rstrip("/")


def share_url(video_id: str) -> str:
    """The link the app's Share button should copy."""
    return f"{_base()}/v/{video_id}"


def _e(value) -> str:
    """HTML-escape any value, quotes included (safe inside attributes)."""
    return html.escape("" if value is None else str(value), quote=True)


def _clean(text: Optional[str], limit: int) -> str:
    """One line of plain text, control characters dropped, length capped."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _safe_https(url: Optional[str]) -> Optional[str]:
    """Only well-formed https URLs with no credentials; anything else is dropped."""
    if not url or not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        return None
    if any(c in url for c in ('"', "'", "<", ">", " ", "\\", "\n", "\r")):
        return None
    return url


def _stream(video: dict):
    """(host, uid) of the post's Cloudflare Stream video, or (None, None)."""
    for url in (video.get("remoteUrl_CF"), video.get("remoteUrl")):
        uid = extract_stream_uid(url or "")
        if not uid or not _UID_RE.match(uid):
            continue
        host = (urlparse(url).hostname or "").lower()
        if host in _STREAM_HOSTS or host.endswith(_STREAM_HOST_SUFFIX):
            return host, uid
    return None, None


def _player_url(host: str, uid: str) -> str:
    if host.endswith(_STREAM_HOST_SUFFIX):
        return f"https://{host}/{uid}/iframe"
    return f"https://iframe.videodelivery.net/{uid}"


def _stream_thumbnail(host: str, uid: str) -> str:
    return f"https://{host}/{uid}/thumbnails/thumbnail.jpg?time=1s&height=720"


def _is_photo(video: dict) -> bool:
    return video.get("media_type") == "photo" or (bool(video.get("images")) and not _stream(video)[1])


def _iso(dt) -> Optional[str]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _iso_duration(seconds) -> Optional[str]:
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        return None
    if s <= 0:
        return None
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + f"{sec}S"


def _json_ld(data: dict) -> str:
    """JSON for a <script type=application/ld+json> block that can't break out."""
    raw = json.dumps(data, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


async def _shareable(db, video_id: str):
    """(video, creator) when the post may be shown by link, else (None, None)."""
    if not ObjectId.is_valid(video_id):
        return None, None
    video = await db.videos.find_one({
        "_id": ObjectId(video_id),
        "is_active": True,
        "privacy": {"$in": ["public", "unlisted"]},
    })
    if not video or video.get("status") in _HIDDEN_STATUSES:
        return None, None
    photo = _is_photo(video)
    if not photo and (video.get("isReadyToStream") is False or not _stream(video)[1]):
        return None, None
    if photo and not any(_safe_https(u) for u in (video.get("images") or [])):
        return None, None
    creator = None
    if video.get("creator_id"):
        creator = await db.users.find_one(
            {"_id": video["creator_id"]},
            {"display_name": 1, "username": 1, "is_active": 1},
        )
    if not creator or creator.get("is_active") is False:
        return None, None
    return video, creator


def _meta(video: dict, creator: dict) -> dict:
    vid = str(video["_id"])
    name = _clean(creator.get("display_name") or creator.get("username"), 60) or "Wano"
    desc_src = video.get("description") or ""
    title = _clean(video.get("title"), _TITLE_MAX) or _clean(desc_src, _TITLE_MAX) or f"{name} on Wano"
    description = _clean(desc_src, _DESC_MAX) or f"Watch {name} on Wano"
    host, uid = _stream(video)
    photo = _is_photo(video)
    images = [u for u in (_safe_https(x) for x in (video.get("images") or [])) if u][:10]
    thumb = _safe_https((video.get("urls") or {}).get("thumbnail"))
    if photo:
        thumb = images[0] if images else thumb
    elif not thumb or thumb == DEFAULT_THUMBNAIL:
        thumb = _stream_thumbnail(host, uid)
    return {
        "id": vid, "title": title, "description": description, "creator": name,
        "photo": photo, "images": images, "thumbnail": thumb,
        "player": None if photo else _player_url(host, uid),
        "unlisted": video.get("privacy") == "unlisted",
        "url": share_url(vid), "embed": f"{_base()}/embed/{vid}",
        "uploaded": _iso(video.get("published_at") or video.get("created_at")),
        "duration": None if photo else _iso_duration(video.get("duration")),
    }


def _headers(*, cache: str, noindex: bool, frameable: bool, player_host: Optional[str] = None) -> dict:
    frame_src = "https://iframe.videodelivery.net https://*.cloudflarestream.com"
    csp = (
        "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; "
        f"frame-src {frame_src}; base-uri 'none'; form-action 'none'; "
        + ("frame-ancestors *" if frameable else "frame-ancestors 'none'")
    )
    h = {
        "Cache-Control": cache,
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    if not frameable:
        h["X-Frame-Options"] = "DENY"
    if noindex:
        h["X-Robots-Tag"] = "noindex, nofollow"
    return h


_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#0b0b0d;color:#f2f2f2;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:480px;margin:0 auto;padding:16px}
.frame{position:relative;height:min(64vh,calc((100vw - 32px)*16/9),796px);aspect-ratio:9/16;margin:0 auto;background:#000;border-radius:12px;overflow:hidden}
.frame iframe,.frame img{position:absolute;inset:0;width:100%;height:100%;border:0;object-fit:contain}
.photos{display:flex;gap:8px;overflow-x:auto;scroll-snap-type:x mandatory}
.photos .frame{flex:0 0 auto;scroll-snap-align:center}
h1{font-size:18px;line-height:1.3;margin:14px 0 4px}p{margin:0;color:#b9b9c0;font-size:14px;line-height:1.4}
.by{color:#8d8d96;font-size:13px;margin-top:6px}
.cta{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.cta a{flex:1 1 140px;text-align:center;padding:12px 14px;border-radius:10px;
text-decoration:none;font-weight:600;font-size:15px;background:#26262b;color:#fff}
.cta a.primary{background:#ff3b30}
.gone{text-align:center;padding:96px 16px}
"""


def _cta(video_id: Optional[str], path: Optional[str] = None) -> str:
    links = []
    if path is None:
        path = f"v/{video_id}" if video_id else ""
    scheme = re.sub(r"[^a-z0-9+.-]", "", (settings.APP_SCHEME or "").lower())
    if scheme:
        links.append(f'<a class="primary" href="{_e(f"{scheme}://{path}")}">Open in Wano</a>')
    ios = _safe_https(settings.IOS_APP_STORE_URL)
    android = _safe_https(settings.ANDROID_PLAY_STORE_URL)
    if ios:
        links.append(f'<a href="{_e(ios)}">App Store</a>')
    if android:
        links.append(f'<a href="{_e(android)}">Google Play</a>')
    return f'<div class="cta">{"".join(links)}</div>' if links else ""


def _smart_banner(url: Optional[str]) -> str:
    app_id = re.sub(r"\D", "", settings.IOS_APP_STORE_ID or "")
    if not app_id:
        return ""
    arg = f", app-argument={_e(url)}" if url else ""
    return f'<meta name="apple-itunes-app" content="app-id={app_id}{arg}">'


def _not_available() -> Response:
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wano</title><meta name="robots" content="noindex, nofollow">
<meta property="og:site_name" content="Wano"><meta property="og:title" content="Wano">
<meta property="og:description" content="This post isn't available.">
{_smart_banner(None)}<style>{_STYLE}</style></head>
<body><main class="gone"><h1>This post isn't available</h1>
<p>It may be private or removed.</p>{_cta(None)}</main></body></html>"""
    return Response(body, status_code=404, media_type="text/html; charset=utf-8",
                    headers=_headers(cache="public, max-age=60", noindex=True, frameable=False))


# ------------------------------------------------------------------ pages

@router.api_route("/v/{video_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def watch_page(video_id: str):
    db = get_database()
    video, creator = await _shareable(db, video_id)
    if not video:
        return _not_available()
    m = _meta(video, creator)

    if m["photo"]:
        media = '<div class="photos">' + "".join(
            f'<div class="frame"><img src="{_e(u)}" alt=""></div>' for u in m["images"]) + "</div>"
    else:
        media = (f'<div class="frame"><iframe src="{_e(m["player"])}" loading="eager" '
                 'allow="accelerometer; gyroscope; autoplay; encrypted-media; picture-in-picture;" '
                 'allowfullscreen title="Video player"></iframe></div>')

    og = [
        ("og:site_name", "Wano"), ("og:url", m["url"]), ("og:title", m["title"]),
        ("og:description", m["description"]), ("og:image", m["thumbnail"]),
        ("og:image:secure_url", m["thumbnail"]),
    ]
    tw = [("twitter:title", m["title"]), ("twitter:description", m["description"]),
          ("twitter:image", m["thumbnail"])]
    if m["photo"]:
        og.append(("og:type", "website"))
        tw.insert(0, ("twitter:card", "summary_large_image"))
    else:
        og += [("og:type", "video.other"), ("og:video:url", m["embed"]),
               ("og:video:secure_url", m["embed"]), ("og:video:type", "text/html"),
               ("og:video:width", "720"), ("og:video:height", "1280")]
        tw = [("twitter:card", "player"), ("twitter:player", m["embed"]),
              ("twitter:player:width", "360"), ("twitter:player:height", "640")] + tw
    tags = "\n".join(f'<meta property="{k}" content="{_e(v)}">' for k, v in og if v)
    tags += "\n" + "\n".join(f'<meta name="{k}" content="{_e(v)}">' for k, v in tw if v)

    ld = ""
    if not m["photo"] and not m["unlisted"]:
        data = {
            "@context": "https://schema.org", "@type": "VideoObject",
            "name": m["title"], "description": m["description"],
            "thumbnailUrl": [m["thumbnail"]], "embedUrl": m["embed"], "url": m["url"],
            "author": {"@type": "Person", "name": m["creator"]},
        }
        if m["uploaded"]:
            data["uploadDate"] = m["uploaded"]
        if m["duration"]:
            data["duration"] = m["duration"]
        ld = f'<script type="application/ld+json">{_json_ld(data)}</script>'

    robots = '<meta name="robots" content="noindex, nofollow">' if m["unlisted"] else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(m["title"])} · Wano</title>
<meta name="description" content="{_e(m["description"])}">
<link rel="canonical" href="{_e(m["url"])}">
{robots}
{tags}
{_smart_banner(m["url"])}
{ld}
<style>{_STYLE}</style></head>
<body><main>{media}
<h1>{_e(m["title"])}</h1>
<p>{_e(m["description"])}</p>
<div class="by">{_e(m["creator"])} · Wano</div>
{_cta(m["id"])}</main></body></html>"""
    cache = "private, max-age=60" if m["unlisted"] else "public, max-age=300"
    return Response(body, media_type="text/html; charset=utf-8",
                    headers=_headers(cache=cache, noindex=m["unlisted"], frameable=False))


@router.api_route("/embed/{video_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def embed_page(video_id: str):
    db = get_database()
    video, creator = await _shareable(db, video_id)
    if not video or _is_photo(video):
        return _not_available()
    m = _meta(video, creator)
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(m["title"])} · Wano</title><meta name="robots" content="noindex">
<link rel="canonical" href="{_e(m["url"])}">
<style>html,body{{margin:0;height:100%;background:#000}}iframe{{border:0;width:100%;height:100%}}</style>
</head><body><iframe src="{_e(m["player"])}" allow="accelerometer; gyroscope; autoplay; encrypted-media; picture-in-picture;" allowfullscreen title="Video player"></iframe></body></html>"""
    return Response(body, media_type="text/html; charset=utf-8",
                    headers=_headers(cache="public, max-age=300", noindex=True, frameable=True))


# ------------------------------------------------------------------ crawlers

_SITEMAP_QUERY = {
    "is_active": True, "privacy": "public",
    "status": {"$nin": list(_HIDDEN_STATUSES)},
    "isReadyToStream": {"$ne": False},
    "media_type": {"$ne": "photo"},
}


def _x(value) -> str:
    """XML-escape text (also strips characters XML 1.0 forbids)."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]", "", "" if value is None else str(value))
    return html.escape(text, quote=True)


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse(
        "User-agent: *\nAllow: /v/\nDisallow: /api/\nDisallow: /graphql\n"
        "Disallow: /video/\nDisallow: /embed/\nDisallow: /debug/\n"
        f"Sitemap: {_base()}/sitemap.xml\n",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_index():
    db = get_database()
    total = await db.videos.count_documents(_SITEMAP_QUERY)
    pages = max(1, math.ceil(total / SITEMAP_PAGE_SIZE))
    items = "".join(f"<sitemap><loc>{_x(_base())}/sitemaps/videos-{n}.xml</loc></sitemap>"
                    for n in range(1, pages + 1))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>')
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/sitemaps/videos-{page}.xml", include_in_schema=False)
async def sitemap_videos(page: int):
    if page < 1 or page > 10000:
        return Response(status_code=404)
    db = get_database()
    cursor = (db.videos.find(_SITEMAP_QUERY)
              .sort("_id", 1).skip((page - 1) * SITEMAP_PAGE_SIZE).limit(SITEMAP_PAGE_SIZE))
    videos = await cursor.to_list(length=SITEMAP_PAGE_SIZE)
    creator_ids = list({v["creator_id"] for v in videos if v.get("creator_id")})
    active = set()
    if creator_ids:
        async for u in db.users.find({"_id": {"$in": creator_ids}, "is_active": {"$ne": False}},
                                     {"display_name": 1, "username": 1}):
            active.add(u["_id"])
    entries = []
    for v in videos:
        host, uid = _stream(v)
        if not uid or v.get("creator_id") not in active:
            continue
        m = _meta(v, {"display_name": ""})
        parts = [
            f"<loc>{_x(m['url'])}</loc><video:video>",
            f"<video:thumbnail_loc>{_x(m['thumbnail'])}</video:thumbnail_loc>",
            f"<video:title>{_x(m['title'])}</video:title>",
            f"<video:description>{_x(m['description'])}</video:description>",
            f"<video:player_loc>{_x(m['embed'])}</video:player_loc>",
        ]
        try:
            secs = int(round(float(v.get("duration") or 0)))
        except (TypeError, ValueError, OverflowError):
            secs = 0
        if 1 <= secs <= 28800:
            parts.append(f"<video:duration>{secs}</video:duration>")
        if m["uploaded"]:
            parts.append(f"<video:publication_date>{_x(m['uploaded'])}</video:publication_date>")
        parts.append("<video:family_friendly>yes</video:family_friendly></video:video>")
        entries.append("<url>" + "".join(parts) + "</url>")
    if page > 1 and not videos:
        return Response(status_code=404)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
           'xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">'
           + "".join(entries) + "</urlset>")
    return Response(xml, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


# ------------------------------------------------------------------ app links

# Paths the app opens itself when it's installed (the rest stay web pages).
APP_PATHS = ("/v/*", "/u/*", "/tv/*", "/pages/feed", "/pages/feed/*", "/pages/v2/wano-tv/details")


def _csv(value: str) -> list:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


@router.get("/.well-known/apple-app-site-association", include_in_schema=False)
@router.get("/apple-app-site-association", include_in_schema=False)
async def apple_app_site_association():
    app_ids = [a for a in _csv(settings.IOS_APP_IDS) if re.fullmatch(r"[A-Z0-9]{10}\.[A-Za-z0-9.\-]+", a)]
    if not app_ids:
        return JSONResponse({"detail": "Not configured"}, status_code=404)
    return JSONResponse(
        {"applinks": {
            "apps": [],
            "details": [
                {"appIDs": app_ids, "components": [{"/": p} for p in APP_PATHS]},
                *({"appID": a, "paths": list(APP_PATHS)} for a in app_ids),
            ],
        }},
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def android_asset_links():
    package = settings.ANDROID_PACKAGE.strip()
    prints = [f.upper() for f in _csv(settings.ANDROID_SHA256_FINGERPRINTS)
              if re.fullmatch(r"([0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2}", f)]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+", package) or not prints:
        return JSONResponse({"detail": "Not configured"}, status_code=404)
    return JSONResponse(
        [{"relation": ["delegate_permission/common.handle_all_urls"],
          "target": {"namespace": "android_app", "package_name": package,
                     "sha256_cert_fingerprints": prints}}],
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ------------------------------------------------------------------ profiles, Wano TV, app

def _simple_page(*, title: str, description: str, image: Optional[str], url: str, path: str,
                 heading: str, sub: str, round_image: bool = False, noindex: bool = True,
                 cache: str = "public, max-age=300") -> Response:
    og = [("og:site_name", "Wano"), ("og:url", url), ("og:title", title), ("og:description", description),
          ("og:type", "website"), ("og:image", image), ("og:image:secure_url", image)]
    tags = "\n".join(f'<meta property="{k}" content="{_e(v)}">' for k, v in og if v)
    tags += "\n" + "\n".join(f'<meta name="{k}" content="{_e(v)}">' for k, v in (
        ("twitter:card", "summary_large_image" if image and not round_image else "summary"),
        ("twitter:title", title), ("twitter:description", description), ("twitter:image", image)) if v)
    robots = '<meta name="robots" content="noindex, nofollow">' if noindex else ""
    pic = ""
    if image:
        style = ("width:120px;height:120px;border-radius:60px;object-fit:cover;display:block;margin:24px auto 0"
                 if round_image else "width:100%;max-width:320px;border-radius:12px;display:block;margin:16px auto 0")
        pic = f'<img src="{_e(image)}" alt="" style="{style}">'
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(description)}">
<link rel="canonical" href="{_e(url)}">
{robots}
{tags}
{_smart_banner(url)}
<style>{_STYLE}</style></head>
<body><main style="text-align:center">{pic}
<h1>{_e(heading)}</h1>
<p>{_e(sub)}</p>
{_cta(None, path)}</main></body></html>"""
    return Response(body, media_type="text/html; charset=utf-8",
                    headers=_headers(cache=cache, noindex=noindex, frameable=False))


def _gone(what: str) -> Response:
    r = _not_available()
    r.body = r.body.replace(b"This post isn't available", f"This {what} isn't available".encode())
    r.headers["content-length"] = str(len(r.body))
    return r


@router.api_route("/u/{user_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def profile_page(user_id: str):
    from app.services.home_ground import is_minor
    if not ObjectId.is_valid(user_id):
        return _gone("profile")
    db = get_database()
    u = await db.users.find_one({"_id": ObjectId(user_id)},
                                {"display_name": 1, "username": 1, "profile_picture": 1, "is_active": 1,
                                 "date_of_birth": 1, "is_banned": 1, "deleted_at": 1})
    if not u or u.get("is_active") is False or u.get("is_banned") is True or u.get("deleted_at"):
        return _gone("profile")
    uid = str(u["_id"])
    url = f"{_base()}/u/{uid}"
    if is_minor(u):
        # Never put a young person's name or face on an open web page.
        return _simple_page(title="Wano", description="Watch on Wano", image=None, url=url, path=f"u/{uid}",
                            heading="Open this profile in Wano", sub="Watch and follow on the Wano app.")
    name = _clean(u.get("display_name") or u.get("username"), 60) or "Wano"
    handle = _clean(u.get("username"), 40)
    return _simple_page(
        title=f"{name} on Wano", description=f"Watch {name}" + (f" (@{handle})" if handle else "") + " on Wano",
        image=_safe_https(u.get("profile_picture")), url=url, path=f"u/{uid}", round_image=True,
        heading=name, sub=(f"@{handle} · " if handle else "") + "Watch and follow on Wano")


@router.api_route("/tv/{title_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def tv_page(title_id: str):
    if not ObjectId.is_valid(title_id):
        return _gone("title")
    db = get_database()
    t = await db.tv_titles.find_one({"_id": ObjectId(title_id), "is_active": True},
                                    {"title": 1, "description": 1, "poster": 1, "backdrop": 1, "year": 1})
    if not t:
        return _gone("title")
    tid = str(t["_id"])
    name = _clean(t.get("title"), _TITLE_MAX) or "Wano TV"
    desc = _clean(t.get("description"), _DESC_MAX) or f"Watch {name} on Wano TV"
    return _simple_page(title=f"{name} · Wano TV", description=desc,
                        image=_safe_https(t.get("poster")) or _safe_https(t.get("backdrop")),
                        url=f"{_base()}/tv/{tid}", path=f"tv/{tid}", heading=name, sub=desc, noindex=False)


@router.api_route("/app", methods=["GET", "HEAD"], include_in_schema=False)
async def app_page():
    return _simple_page(title="Wano", description="Watch, post and share African videos on Wano.",
                        image=None, url=f"{_base()}/app", path="", heading="Wano",
                        sub="Watch, post and share African videos.", noindex=False,
                        cache="public, max-age=3600")


@router.api_route("/pages/feed", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/pages/feed/", methods=["GET", "HEAD"], include_in_schema=False)
async def legacy_feed_link(request: Request):
    """Links shared by app versions up to 2.3.27."""
    q = request.query_params
    vid, uid = q.get("videoId") or "", q.get("userId") or ""
    if ObjectId.is_valid(vid):
        to = f"/v/{vid}"
    elif ObjectId.is_valid(uid):
        to = f"/u/{uid}"
    else:
        to = "/app"
    return RedirectResponse(to, status_code=301, headers={"Cache-Control": "public, max-age=86400"})


@router.api_route("/pages/v2/wano-tv/details", methods=["GET", "HEAD"], include_in_schema=False)
async def legacy_tv_link(request: Request):
    tid = request.query_params.get("id") or ""
    return RedirectResponse(f"/tv/{tid}" if ObjectId.is_valid(tid) else "/app", status_code=301,
                            headers={"Cache-Control": "public, max-age=86400"})
