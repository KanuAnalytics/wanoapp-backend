"""Compressed API replies, behind the "gzip_api" switch (off by default).

JSON compresses to about a fifth of its size (a New feed page: 34 KB -> 7 KB),
so this is the cheapest data saving there is for people on mobile data. Every
phone already sends "Accept-Encoding: gzip" and unpacks the reply on its own,
so it works for every installed app version, with no app update.

Only /api/ and /graphql replies of at least MIN_SIZE bytes are compressed, and
only for clients that ask for it; uploads, the admin page, share pages, Wano TV
film playlists and anything already compressed are left exactly as they are. The switch is read
from the database at most every REFRESH_SECONDS (and straight away when an
admin flips it); if that read fails, the last known value stays.
"""

import asyncio
import logging
import time

from starlette.middleware.gzip import GZipMiddleware

logger = logging.getLogger(__name__)

MIN_SIZE = 1000
LEVEL = 6
REFRESH_SECONDS = 30.0
PATHS = ("/api/", "/graphql")
# Film playlists go to the video player, not the app code: left exactly as they were.
SKIP = ("/api/v1/tv/stream/",)

_state = {"on": False, "at": -1e9}
_lock = asyncio.Lock()


def forget() -> None:
    """Re-read the switch on the next request (called when an admin flips one)."""
    _state["at"] = -1e9


async def _on() -> bool:
    now = time.monotonic()
    if now - _state["at"] < REFRESH_SECONDS:
        return _state["on"]
    async with _lock:
        if time.monotonic() - _state["at"] < REFRESH_SECONDS:
            return _state["on"]
        try:
            from app.core.database import get_database
            from app.services import features
            _state["on"] = (await features.current(get_database())).get("gzip_api") is True
        except Exception:
            logger.warning("gzip_api: couldn't read the switch, keeping %s", _state["on"])
        _state["at"] = time.monotonic()
    return _state["on"]


def _wants_gzip(scope) -> bool:
    for k, v in scope.get("headers") or []:
        if k == b"accept-encoding" and b"gzip" in v.lower():
            return True
    return False


class SwitchedGZip:
    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=MIN_SIZE, compresslevel=LEVEL)

    async def __call__(self, scope, receive, send):
        path = str(scope.get("path", ""))
        if (scope.get("type") == "http" and path.startswith(PATHS) and not path.startswith(SKIP)
                and _wants_gzip(scope) and await _on()):
            await self.gzip(scope, receive, send)
            return
        await self.app(scope, receive, send)
