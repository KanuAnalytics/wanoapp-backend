"""Episodes from one film ("tv_parts" switch, app 2.3.63).

A long film can also be watched as episodes. An episode is only a time range of
the same film (start/end seconds): no second copy is stored or uploaded, and
"Watch the full film" plays the same file from the start.

- Admin > Wano TV titles > "Episodes from one film": Propose, check, Save.
- Propose: the server looks for good places to cut, in the background, at the
  lowest quality and lowest priority (nice 19, one job at a time) so the live
  server isn't slowed:
    scene changes (ffmpeg scene detection), pauses in the sound (silencedetect),
    and gaps in the dialogue (the title's subtitles, when it has them).
  Then it picks cuts so episodes are about TARGET_S long (between MIN_S and
  MAX_S), preferring places where several signals agree.
- A proposal is only a suggestion: nothing shows in the app until someone Saves
  it (the parts on the title), and only with the switch on.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import HTTPException

logger = logging.getLogger(__name__)

SWITCH = "tv_parts"
TARGET_S = 11 * 60
MIN_S = 6 * 60
MAX_S = 16 * 60
MIN_FILM_S = 20 * 60            # shorter films stay whole
MAX_PARTS = 30
JOB_TIMEOUT_S = 25 * 60
SNAP_S = 6.0                    # signals this close count as the same moment

_job: Dict[str, Optional[asyncio.Task]] = {"task": None, "title": None}


# ---------------------------------------------------------------- checking saved parts

def has_episodes(doc: dict) -> bool:
    """A series with real episodes. A "series" that is still one long video
    (no episodes yet, e.g. Yaoundé) can be watched as episodes from one file."""
    if doc.get("type") != "series":
        return False
    return any(isinstance(s, dict) and s.get("episodes") for s in doc.get("seasons") or [])


def clean(parts, duration_s: float) -> List[dict]:
    """Validated, numbered parts. Raises 400 with a plain reason."""
    if not isinstance(parts, list) or not parts:
        raise HTTPException(400, "Add at least two episodes, or remove them all")
    if len(parts) < 2 or len(parts) > MAX_PARTS:
        raise HTTPException(400, f"Between 2 and {MAX_PARTS} episodes")
    out, prev_end = [], 0.0
    for i, p in enumerate(sorted(parts, key=lambda p: float(p.get("start", 0)))):
        try:
            start, end = float(p["start"]), float(p["end"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, f"Episode {i + 1}: start and end are needed")
        if start < 0 or end <= start:
            raise HTTPException(400, f"Episode {i + 1}: the end must be after the start")
        if end - start < 60:
            raise HTTPException(400, f"Episode {i + 1} is shorter than a minute")
        if start < prev_end - 0.5:
            raise HTTPException(400, f"Episode {i + 1} overlaps the one before")
        if duration_s and end > duration_s + 90:
            raise HTTPException(400, f"Episode {i + 1} ends after the film does")
        title = str(p.get("title") or "").strip()[:80] or f"Episode {i + 1}"
        out.append({"number": i + 1, "title": title, "start": round(start, 1), "end": round(end, 1)})
        prev_end = end
    return out


# ---------------------------------------------------------------- reading the signals

_SCENE = re.compile(r"pts_time:([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):([0-9.]+)")
_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


def parse_ffmpeg(stderr: str) -> Tuple[List[float], List[float], float]:
    """(scene change times, middles of pauses, film length) from ffmpeg's log."""
    scenes = [float(m.group(1)) for m in _SCENE.finditer(stderr)]
    pauses = []
    for m in _SILENCE_END.finditer(stderr):
        end, dur = float(m.group(1)), float(m.group(2))
        pauses.append(round(end - dur / 2, 2))
    d = _DURATION.search(stderr)
    length = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    return scenes, pauses, length


def _secs(h, m, s, ms) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def dialogue_gaps(vtt: str, min_gap: float = 2.5) -> List[float]:
    """Middles of the silences between subtitle lines (a scene often ends there)."""
    cues = []
    for m in _VTT_TIME.finditer(vtt or ""):
        g = m.groups()
        cues.append((_secs(*g[0:4]), _secs(*g[4:8])))
    cues.sort()
    gaps = []
    for (s1, e1), (s2, _e2) in zip(cues, cues[1:]):
        if s2 - e1 >= min_gap:
            gaps.append(round((e1 + s2) / 2, 2))
    return gaps


def candidates(scenes: List[float], pauses: List[float], gaps: List[float]) -> List[Tuple[float, int]]:
    """Cut points with a strength: 1 for each kind of signal found there."""
    pts: List[Tuple[float, int]] = []
    for kind, times in (("scene", scenes), ("pause", pauses), ("gap", gaps)):
        for t in times:
            for i, (pt, strength) in enumerate(pts):
                if abs(pt - t) <= SNAP_S:
                    if kind == "scene":
                        pts[i] = (t, strength + 1)      # the picture changes exactly at a scene cut
                    else:
                        pts[i] = (pt, strength + 1)
                    break
            else:
                pts.append((t, 1))
    return sorted(pts)


def choose(points: List[Tuple[float, int]], length: float) -> List[Tuple[float, float]]:
    """Episode ranges: cuts chosen so each episode is MIN_S..MAX_S, close to
    TARGET_S, preferring strong cut points. Dynamic programming over points."""
    if length < MIN_FILM_S:
        return []
    pts = [(0.0, 9)] + [(t, s) for t, s in points if MIN_S <= t <= length - MIN_S] + [(length, 9)]
    n = len(pts)
    INF = float("inf")
    best = [INF] * n
    prev = [-1] * n
    best[0] = 0.0
    for j in range(1, n):
        tj, sj = pts[j]
        for i in range(j - 1, -1, -1):
            ti = pts[i][0]
            seg = tj - ti
            if seg > MAX_S:
                break
            if seg < MIN_S or best[i] == INF:
                continue
            # Off-target minutes cost; a strong cut point pays some back.
            cost = best[i] + ((seg - TARGET_S) / 60.0) ** 2 + (0 if j == n - 1 else 3.0 / (1 + sj))
            if cost < best[j]:
                best[j], prev[j] = cost, i
    if best[-1] == INF:
        # No signal-based plan fits: equal parts close to the target.
        k = max(2, round(length / TARGET_S))
        step = length / k
        return [(round(i * step, 1), round((i + 1) * step if i < k - 1 else length, 1)) for i in range(k)]
    cuts, j = [], n - 1
    while j > 0:
        cuts.append(pts[j][0])
        j = prev[j]
    cuts = sorted(cuts)
    starts = [0.0] + cuts[:-1]
    return [(round(a, 1), round(b, 1)) for a, b in zip(starts, cuts)]


# ---------------------------------------------------------------- the background job

def _lowest_variant(master_url: str) -> str:
    """The smallest rendition in an HLS master playlist (or the URL itself)."""
    import requests
    from urllib.parse import urljoin
    try:
        text = requests.get(master_url, timeout=20).text
    except Exception:
        return master_url
    best, best_bw = None, None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            nxt = next((l for l in lines[i + 1:] if l and not l.startswith("#")), None)
            if m and nxt and (best_bw is None or int(m.group(1)) < best_bw):
                best, best_bw = urljoin(master_url, nxt), int(m.group(1))
    return best or master_url


def _fetch_text(url: str) -> str:
    import requests
    try:
        r = requests.get(url, timeout=20)
        return r.text if r.status_code < 300 else ""
    except Exception:
        return ""


async def _run_ffmpeg(url: str, audio: bool = True) -> Tuple[int, str]:
    if audio:
        graph = ["-filter_complex", "[0:v:0]fps=2,scale=160:-2,select='gt(scene,0.35)',showinfo[v];"
                                    "[0:a:0]silencedetect=noise=-35dB:d=1.0[a]", "-map", "[v]", "-map", "[a]"]
    else:          # e.g. Cloudflare keeps the sound in a separate playlist
        graph = ["-map", "0:v:0", "-vf", "fps=2,scale=160:-2,select='gt(scene,0.35)',showinfo"]
    args = ["nice", "-n", "19", "ffmpeg", "-hide_banner", "-nostats", "-threads", "1",
            "-protocol_whitelist", "https,http,tls,tcp,crypto", "-i", url,
            *graph, "-f", "null", "-"]
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=JOB_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("took too long")
    return proc.returncode or 0, (err or b"").decode("utf-8", "replace")


async def _source(db, doc: dict) -> Tuple[str, str]:
    """(a playable link the server can read, subtitles text or '')."""
    from app.api.v1 import tv as tv_api
    from app.services import tv_cf
    data = {**doc, "id": str(doc["_id"])}
    if tv_cf.valid_uid(doc.get("cf_uid")) and doc.get("cf_ready") is True:
        await asyncio.to_thread(tv_cf.token, doc["cf_uid"])
    data = tv_api._protect(data, "staff-proposal")
    url = data.get("video_url") or ""
    subs = ""
    for c in data.get("captions") or []:
        if isinstance(c, dict) and c.get("url"):
            subs = await asyncio.to_thread(_fetch_text, c["url"])
            if subs:
                break
    return url, subs


async def _propose(db, title_id: ObjectId) -> None:
    doc = await db.tv_titles.find_one({"_id": title_id})
    state = {"state": "running", "started_at": datetime.utcnow()}
    await db.tv_titles.update_one({"_id": title_id}, {"$set": {"parts_proposal": state}})
    try:
        url, subs = await _source(db, doc)
        if not url:
            raise RuntimeError("the title has no film")
        variant = await asyncio.to_thread(_lowest_variant, url)
        rc, log = await _run_ffmpeg(variant, audio=True)
        if rc != 0:
            rc, log = await _run_ffmpeg(variant, audio=False)
            if rc != 0:
                raise RuntimeError("couldn't read the film (" + log.strip().splitlines()[-1][:120] + ")"
                                   if log.strip() else "couldn't read the film")
        scenes, pauses, length = parse_ffmpeg(log)
        length = length or float(doc.get("duration_minutes") or 0) * 60
        if not length:
            raise RuntimeError("couldn't read the film's length")
        gaps = dialogue_gaps(subs)
        ranges = choose(candidates(scenes, pauses, gaps), length)
        used = [k for k, v in (("scene changes", scenes), ("pauses", pauses), ("subtitles", gaps)) if v]
        result = {"state": "done", "finished_at": datetime.utcnow(), "length": round(length, 1), "used": used,
                  "parts": [{"number": i + 1, "title": f"Episode {i + 1}", "start": a, "end": b}
                            for i, (a, b) in enumerate(ranges)],
                  "note": "" if ranges else "Under 20 minutes: better as one film."}
    except Exception as e:
        logger.warning("Proposing episodes for %s failed: %s", title_id, e)
        result = {"state": "failed", "finished_at": datetime.utcnow(), "error": str(e)[:200]}
    await db.tv_titles.update_one({"_id": title_id}, {"$set": {"parts_proposal": result}})


def running() -> bool:
    t = _job.get("task")
    return t is not None and not t.done()


async def start(db, title_id: str) -> dict:
    if not ObjectId.is_valid(title_id):
        raise HTTPException(400, "Invalid title ID")
    oid = ObjectId(title_id)
    doc = await db.tv_titles.find_one({"_id": oid}, {"_id": 1, "type": 1, "seasons": 1})
    if not doc:
        raise HTTPException(404, "Title not found")
    if has_episodes(doc):
        raise HTTPException(400, "This series already has episodes: episodes from one film are for one long video")
    if running():
        raise HTTPException(409, "Another title is being looked at. Try again in a few minutes.")
    _job["task"] = asyncio.create_task(_propose(db, oid))
    _job["title"] = title_id
    return {"started": True}


async def proposal(db, title_id: str) -> dict:
    if not ObjectId.is_valid(title_id):
        raise HTTPException(400, "Invalid title ID")
    doc = await db.tv_titles.find_one({"_id": ObjectId(title_id)}, {"parts_proposal": 1, "parts": 1})
    if not doc:
        raise HTTPException(404, "Title not found")
    p = doc.get("parts_proposal") or {}
    if p.get("state") == "running" and not running():
        p = {"state": "failed", "error": "interrupted (the server restarted): try again"}
    for k in ("started_at", "finished_at"):
        if isinstance(p.get(k), datetime):
            p[k] = p[k].isoformat() + "Z"
    return {"proposal": p, "parts": doc.get("parts") or []}


async def save(db, title_id: str, parts: Optional[list]) -> dict:
    """Save (approve) the episodes, or remove them with None / []."""
    if not ObjectId.is_valid(title_id):
        raise HTTPException(400, "Invalid title ID")
    doc = await db.tv_titles.find_one({"_id": ObjectId(title_id)}, {"type": 1, "duration_minutes": 1, "seasons": 1})
    if not doc:
        raise HTTPException(404, "Title not found")
    if has_episodes(doc):
        raise HTTPException(400, "This series already has episodes: episodes from one film are for one long video")
    if not parts:
        await db.tv_titles.update_one({"_id": doc["_id"]}, {"$unset": {"parts": ""}})
        return {"parts": []}
    cleaned = clean(parts, float(doc.get("duration_minutes") or 0) * 60)
    await db.tv_titles.update_one({"_id": doc["_id"]}, {"$set": {"parts": cleaned, "updated_at": datetime.utcnow()}})
    from app.services import tv_more
    tv_more.clear_cache()
    return {"parts": cleaned}
