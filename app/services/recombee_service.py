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

_switch = {"on": False, "tune": False, "eu": False, "at": -1e9}
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


def eu_on() -> bool:
    """Last known value of "recombee_eu" (no database read): the new database
    answers, and the old one becomes the copy. Only when one is configured."""
    return _switch["eu"] and new_client is not None


def _clients():
    """(the database that answers, the one writes are copied to or None)."""
    if eu_on():
        return new_client, recombee_client
    return recombee_client, new_client


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
            _switch["eu"] = now.get("recombee_eu") is True
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

REGIONS = {"us-west": Region.US_WEST, "eu-west": Region.EU_WEST,
           "ca-east": Region.CA_EAST, "ap-se": Region.AP_SE}


def region_of(name) -> Region:
    key = str(name or "").strip().lower().replace("_", "-")
    if key not in REGIONS:
        logger.error("Unknown Recombee region %r; using us-west", name)
    return REGIONS.get(key, Region.US_WEST)


recombee_client = RecombeeClient(
    settings.RECOMBEE_DB_ID,
    settings.RECOMBEE_PRIVATE_TOKEN,
    region=region_of(getattr(settings, "RECOMBEE_REGION", "us-west")),
)


def _new_client():
    db_id = (getattr(settings, "RECOMBEE_NEW_DB_ID", "") or "").strip()
    token = (getattr(settings, "RECOMBEE_NEW_PRIVATE_TOKEN", "") or "").strip()
    if not db_id or not token or db_id == settings.RECOMBEE_DB_ID:
        return None
    return RecombeeClient(db_id, token, region=region_of(getattr(settings, "RECOMBEE_NEW_REGION", "eu-west")))


# The database being moved to (None when no move is going on).
new_client = _new_client()

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
            "region": str(getattr(settings, "RECOMBEE_REGION", "us-west")),
            "moving_to": (str(getattr(settings, "RECOMBEE_NEW_REGION", "")) if new_client is not None else None),
            "answering": (str(getattr(settings, "RECOMBEE_NEW_REGION", "")) if eu_on()
                          else str(getattr(settings, "RECOMBEE_REGION", "us-west"))),
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
    # staff_quiet: staff watching and liking (often reported videos) never
    # teaches the recommender anything.
    if await staff_skip(request):
        return None
    fast = await refresh_switch()
    main, copy = _clients()
    t = time.perf_counter()
    try:
        out = await _run(main, request, fast)
    except Exception as e:
        _count(_kind(request), False, (time.perf_counter() - t) * 1000, e)
        if main is recombee_client or _is_write(request):
            raise
        # recombee_eu: a read the new database couldn't answer is asked of the old one.
        t = time.perf_counter()
        try:
            out = await _run(recombee_client, request, fast)
        except Exception as e2:
            _count("fallback:" + _kind(request), False, (time.perf_counter() - t) * 1000, e2)
            raise
        _count("fallback:" + _kind(request), True, (time.perf_counter() - t) * 1000)
        return out
    _count(_kind(request), True, (time.perf_counter() - t) * 1000)
    if copy is not None and _is_write(request):
        _mirror(request, copy)
    return out


async def _run(client, request, fast: bool):
    if fast:
        return await asyncio.get_running_loop().run_in_executor(_pool, client.send, request)
    return await asyncio.to_thread(client.send, request)


# Requests that only read (recommendations, lists): never copied to the new database.
_READ_PREFIXES = ("Recommend", "List", "Get", "Search", "Composite")


async def staff_skip(request) -> bool:
    uid = getattr(request, "user_id", None)
    if not uid or _kind(request) not in _STAFF_QUIET_KINDS:
        return False
    try:
        from app.core.database import get_database
        from app.services import staff
        return await staff.quiet(get_database(), uid)
    except Exception:
        return False


_STAFF_QUIET_KINDS = ("SetViewPortion", "AddRating", "AddBookmark", "AddCartAddition", "AddPurchase",
                      "AddDetailView")


def _is_write(request) -> bool:
    from recombee_api_client.api_requests import Batch
    if isinstance(request, Batch):
        return any(_is_write(r) for r in request.requests)
    return not type(request).__name__.startswith(_READ_PREFIXES)


def _mirror(request, client=None) -> None:
    """Copy a write that worked on the database that answers to the other one,
    in the background, so both stay complete (during a move, and after
    recombee_eu, so turning it off loses nothing)."""
    client = client or new_client

    async def go():
        kind = "mirror:" + _kind(request)
        t = time.perf_counter()
        try:
            await asyncio.get_running_loop().run_in_executor(_pool, client.send, request)
            _count(kind, True, (time.perf_counter() - t) * 1000)
        except Exception as e:
            _count(kind, False, (time.perf_counter() - t) * 1000, e)
    task = asyncio.create_task(go())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def send_to_new(request):
    """Send straight to the new database (filling it). Counted as move:<kind>."""
    if new_client is None:
        raise RuntimeError("No new Recombee database configured")
    kind = "move:" + _kind(request)
    t = time.perf_counter()
    try:
        out = await asyncio.get_running_loop().run_in_executor(_pool, new_client.send, request)
    except Exception as e:
        _count(kind, False, (time.perf_counter() - t) * 1000, e)
        raise
    _count(kind, True, (time.perf_counter() - t) * 1000)
    return out


_background: set = set()

_real_send = recombee_send   # tests swap recombee_send out; this stays the real one


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
