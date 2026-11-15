"""Talking to Recombee (the For You recommender).

Every call goes through recombee_send, which also counts it: how many of each
kind worked or failed, how long they took, and the last errors, for
GET /api/v1/recombee/health. Before Nov 2026 failures were swallowed silently,
so nobody could tell whether views and likes were reaching Recombee.

"recombee_fast" switch (off by default):
  - Keep-alive connections. Recombee's client calls requests.post() for every
    call, so each one opened a new HTTPS connection to Recombee's US West
    servers (several round trips before the request is even sent). Measured
    Nov 15 2026: the Recombee step was 600-1050 ms of every For You page.
    With the switch on, calls reuse a pool of open connections.
  - Recombee calls run on their own threads, so a burst of view signals can't
    queue up behind (or hold up) the server's other background work.
  - View signals (SetViewPortion) are sent in the background: the app's
    /watch call no longer waits for Recombee to answer.
The switch is read from the database at most every REFRESH_SECONDS (and at
once when an admin flips it); if that read fails the last value stays.
"""

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from recombee_api_client import api_client as _rc_module
from recombee_api_client.api_client import RecombeeClient, Region

from app.core.config import settings

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 30.0
POOL_SIZE = 32


# ------------------------------------------------------------------ the switch

_switch = {"on": False, "tune": False, "at": -1e9}
_switch_lock = asyncio.Lock()


def forget() -> None:
    """Re-read the switch on the next call (called when an admin flips one)."""
    _switch["at"] = -1e9


def fast_on() -> bool:
    """Last known value of the switch (no database read)."""
    return _switch["on"]


async def tune_on() -> bool:
    """The "recombee_tune" switch (For You asks Recombee differently)."""
    await refresh_switch()
    return _switch["tune"]


async def refresh_switch() -> bool:
    if time.monotonic() - _switch["at"] < REFRESH_SECONDS:
        return _switch["on"]
    async with _switch_lock:
        if time.monotonic() - _switch["at"] < REFRESH_SECONDS:
            return _switch["on"]
        try:
            from app.core.database import get_database
            from app.services import features
            now = await features.current(get_database())
            _switch["on"] = now.get("recombee_fast") is True
            _switch["tune"] = now.get("recombee_tune") is True
        except Exception:
            logger.warning("recombee_fast: couldn't read the switch, keeping %s", _switch["on"])
        _switch["at"] = time.monotonic()
    return _switch["on"]


# ------------------------------------------------------------------ connections

class _Transport:
    """Stands in for the `requests` module inside Recombee's client: the same
    put/get/post/delete, through a pool of kept-alive connections when the
    switch is on, exactly as before (a new connection each call) when off."""

    exceptions = requests.exceptions

    def __init__(self):
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=POOL_SIZE, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _via(self):
        return self.session if fast_on() else requests

    def put(self, *a, **kw):
        return self._via().put(*a, **kw)

    def get(self, *a, **kw):
        return self._via().get(*a, **kw)

    def post(self, *a, **kw):
        return self._via().post(*a, **kw)

    def delete(self, *a, **kw):
        return self._via().delete(*a, **kw)


transport = _Transport()
# The client looks `requests` up in its own module on every call.
_rc_module.requests = transport

recombee_client = RecombeeClient(
    settings.RECOMBEE_DB_ID,
    settings.RECOMBEE_PRIVATE_TOKEN,
    region=Region.US_WEST
)

_pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="recombee")


# ------------------------------------------------------------------ counting

_started = time.time()
_stats: dict = {}            # kind -> {"ok", "failed", "ms", "max_ms"}
_errors: deque = deque(maxlen=20)


def _kind(request) -> str:
    return type(request).__name__


def _count(kind: str, ok: bool, ms: float, err: Exception = None) -> None:
    s = _stats.setdefault(kind, {"ok": 0, "failed": 0, "ms": 0.0, "max_ms": 0.0})
    s["ok" if ok else "failed"] += 1
    s["ms"] += ms
    s["max_ms"] = max(s["max_ms"], ms)
    if err is not None:
        _errors.append({"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "kind": kind,
                        "status": getattr(err, "status_code", None),
                        "error": f"{type(err).__name__}: {str(err)[:240]}"})


def stats() -> dict:
    kinds = {}
    for kind, s in sorted(_stats.items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["failed"])):
        n = s["ok"] + s["failed"]
        kinds[kind] = {"ok": s["ok"], "failed": s["failed"],
                       "fail_rate": round(s["failed"] / n, 4) if n else 0,
                       "avg_ms": round(s["ms"] / n) if n else None, "max_ms": round(s["max_ms"])}
    return {"since": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(_started)), "fast": fast_on(),
            "calls": kinds, "last_errors": list(_errors)[::-1]}


def reset_stats() -> None:
    _stats.clear()
    _errors.clear()


# ------------------------------------------------------------------ sending

async def recombee_send(request):
    """
    Send a Recombee request without blocking the event loop.

    RecombeeClient.send() is synchronous (uses `requests` under the hood),
    so it must run in a worker thread when called from async endpoints.
    """
    fast = await refresh_switch()
    t = time.perf_counter()
    try:
        if fast:
            out = await asyncio.get_running_loop().run_in_executor(_pool, recombee_client.send, request)
        else:
            out = await asyncio.to_thread(recombee_client.send, request)
    except Exception as e:
        _count(_kind(request), False, (time.perf_counter() - t) * 1000, e)
        raise
    _count(_kind(request), True, (time.perf_counter() - t) * 1000)
    return out


_real_send = recombee_send   # tests swap recombee_send out; this stays the real one

_background: set = set()


async def recombee_signal(request) -> None:
    """For signals nobody waits on (a view): sent in the background when the
    switch is on, otherwise awaited as before. Never raises."""
    async def go():
        try:
            await recombee_send(request)
        except Exception:
            pass   # counted in recombee_send; see /recombee/health
    if await refresh_switch():
        task = asyncio.create_task(go())
        _background.add(task)
        task.add_done_callback(_background.discard)
    else:
        await go()


async def ping(times: int = 3) -> list:
    """Time a few tiny calls one after another (ms). With keep-alive on, the
    calls after the first skip the new-connection cost."""
    from recombee_api_client.api_requests import ListScenarios
    out = []
    for _ in range(times):
        t = time.perf_counter()
        try:
            req = ListScenarios()
            req.timeout = 5000
            await recombee_send(req)
            out.append(round((time.perf_counter() - t) * 1000))
        except Exception as e:
            out.append(f"failed: {type(e).__name__}")
    return out
