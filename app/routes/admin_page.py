"""The admin page: /admin (HTML), /admin/app.js, /admin/logo.png.

Only these three fixed files are served. The page holds no data or secrets;
everything it shows comes from /api/v1/admin/* and /api/v1/points/admin/*,
which need an admin login."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(include_in_schema=False)

_DIR = Path(__file__).resolve().parent.parent / "static" / "admin"
_FILES = {"": ("index.html", "text/html; charset=utf-8"),
          "app.js": ("app.js", "application/javascript; charset=utf-8"),
          "logo.png": ("logo.png", "image/png")}

HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' https: data:; connect-src 'self'; base-uri 'none'; "
                                "form-action 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


def _serve(name: str) -> Response:
    fname, ctype = _FILES[name]
    return Response((_DIR / fname).read_bytes(), media_type=ctype, headers=HEADERS)


@router.get("/admin")
async def admin_page():
    return _serve("")


@router.get("/admin/app.js")
async def admin_js():
    return _serve("app.js")


@router.get("/admin/logo.png")
async def admin_logo():
    return _serve("logo.png")
