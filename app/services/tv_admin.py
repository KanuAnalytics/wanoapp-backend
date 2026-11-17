"""Admin > Wano TV titles and Admin > Wano TV numbers.

Before this, adding or fixing a title meant editing scripts/wano_tv_seed.json
and running a script on the server. Now the admin page can:
- add and edit titles (details, credits, artwork, trailer), as Hidden first;
- upload a film or episode straight to Cloudflare Stream (resumable), or keep
  using a film already on Spaces by pasting its master.m3u8 link;
- add subtitles (.srt or .vtt) in any language, to either kind of film;
- make an offline copy for downloads;
- set each title Live, Hidden or Coming soon (going Live from Coming soon
  pushes everyone who tapped Remind me), and "Tell everyone" once per title;
- link feed posts (clips) to a title.
A title can only go Live when the app would show it (problems() lists why not).
Numbers: plays, people who started and finished, how far they got, My List,
hearts, reminders, downloads and the feed strip, per title.
"""

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from typing import List, Literal, Optional

from bson import ObjectId
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from app.services import tv_cf, tv_files, tv_more, tv_stream

logger = logging.getLogger(__name__)

STATUSES = ("live", "hidden", "coming_soon")
SLUG = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _https_or_blank(v):
    if v is None or v == "":
        return ""
    if not isinstance(v, str) or not v.startswith("https://") or len(v) > 2048 or any(c in v for c in " \n\"'<>"):
        raise ValueError("must be an https:// link")
    return v


def _date_or_none(v):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or not _DATE.match(v):
        raise ValueError("must be a date like 2026-12-01")
    date.fromisoformat(v)
    return v


class TrailerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_url: str = ""
    thumbnail: str = ""
    duration_seconds: int = Field(0, ge=0, le=36000)
    _u = field_validator("video_url", "thumbnail")(_https_or_blank)


class PersonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=80)
    role: str = Field(..., min_length=1, max_length=60)
    username: str = Field("", max_length=40)


class CreditsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    director: str = Field("", max_length=120)
    cast: List[str] = Field(default_factory=list, max_length=40)
    production: str = Field("", max_length=120)
    distributor: str = Field("Wano TV", max_length=120)
    people: List[PersonIn] = Field(default_factory=list, max_length=60)


class TitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(..., min_length=2, max_length=80, pattern=SLUG)
    title: str = Field(..., min_length=1, max_length=120)
    type: Literal["movie", "series"] = "movie"
    genres: List[str] = Field(default_factory=list, max_length=8)
    year: int = Field(..., ge=1900, le=2100)
    release_date: Optional[str] = None
    duration_minutes: int = Field(0, ge=0, le=1000)
    age_rating: str = Field("All", min_length=1, max_length=10)
    description: str = Field("", max_length=300)
    synopsis: str = Field("", max_length=5000)
    poster: str = ""
    backdrop: str = ""
    trailer: TrailerIn = Field(default_factory=TrailerIn)
    credits: CreditsIn = Field(default_factory=CreditsIn)
    video_url: str = ""
    featured: StrictBool = False
    popularity: int = Field(0, ge=0, le=1000)
    coming_at: Optional[str] = None

    _u = field_validator("poster", "backdrop", "video_url")(_https_or_blank)
    _d = field_validator("release_date", "coming_at")(_date_or_none)

    @field_validator("genres")
    @classmethod
    def _genres(cls, v):
        out = []
        for g in v:
            g = (g or "").strip()[:30]
            if g and g.lower() not in ("series", "movie", "movies", "film", "films") and g not in out:
                out.append(g)
        return out


class EpisodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    season: int = Field(..., ge=1, le=99)
    number: int = Field(..., ge=1, le=999)
    title: str = Field(..., min_length=1, max_length=120)
    synopsis: str = Field("", max_length=2000)
    duration_minutes: int = Field(0, ge=0, le=600)
    thumbnail: str = ""
    video_url: str = ""
    _u = field_validator("thumbnail", "video_url")(_https_or_blank)


# ---------------------------------------------------------------- helpers

def _oid(x: str) -> ObjectId:
    if not isinstance(x, str) or not ObjectId.is_valid(x):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid title ID")
    return ObjectId(x)


async def _get(db, title_id: str) -> dict:
    doc = await db.tv_titles.find_one({"_id": _oid(title_id)})
    if not doc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Title not found")
    return doc


def status_of(doc: dict) -> str:
    s = doc.get("status")
    if s in STATUSES:
        return s
    return "live" if doc.get("is_active") is True else "hidden"


def _episodes(doc: dict) -> List[dict]:
    out = []
    for s in doc.get("seasons") or []:
        for e in (s.get("episodes") or []) if isinstance(s, dict) else []:
            if isinstance(e, dict):
                out.append(e)
    return out


def _find_episode(doc: dict, ep_id: Optional[str]) -> Optional[dict]:
    if not ep_id:
        return None
    for e in _episodes(doc):
        if e.get("id") == ep_id:
            return e
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Episode not found")


def _hls_folder(url) -> Optional[str]:
    """wano-tv/<this>/master.m3u8 -> '<this>' for a Spaces HLS film."""
    key = tv_stream.spaces_key(url)
    if not key or not key.endswith("/master.m3u8"):
        return None
    return key[len(tv_stream.PREFIX):-len("/master.m3u8")]


def problems(doc: dict) -> List[str]:
    """Why the app wouldn't show this title yet (empty = ready to go live)."""
    from app.api.v1 import tv as tv_api
    out = []
    if not tv_files.SLUG.match(doc.get("slug") or ""):
        out.append("Needs a web address (slug)")
    for f, label in (("poster", "a poster"), ("backdrop", "a landscape picture")):
        if not str(doc.get(f) or "").startswith("https://"):
            out.append(f"Needs {label}")
    tr = doc.get("trailer") if isinstance(doc.get("trailer"), dict) else {}
    if not str(tr.get("thumbnail") or "").startswith("https://"):
        out.append("Needs a trailer picture")
    if not (doc.get("synopsis") or "").strip():
        out.append("Needs a story (synopsis)")
    if not (doc.get("credits") or {}).get("director"):
        out.append("Needs a director in the credits")
    if not doc.get("release_date"):
        out.append("Needs a release date")
    eps = _episodes(doc)
    if doc.get("type") == "series" and eps:
        for e in eps:
            if tv_cf.valid_uid(e.get("cf_uid")) and e.get("cf_ready") is not True:
                out.append(f"Episode {e.get('id')}: film still processing (press Check)")
            elif not e.get("video_url"):
                out.append(f"Episode {e.get('id')}: no film")
    else:
        if tv_cf.valid_uid(doc.get("cf_uid")) and doc.get("cf_ready") is not True:
            out.append("Film still processing at Cloudflare (press Check film)")
        elif not doc.get("video_url"):
            out.append("Needs the film")
    if not out:
        # The same check the catalogue makes, with films treated as playable.
        probe = {**doc, "is_active": True}
        probe.pop("cf_uid", None)
        probe["seasons"] = [{**s, "episodes": [{k: v for k, v in e.items() if k != "cf_uid"}
                                               for e in (s.get("episodes") or []) if isinstance(e, dict)]}
                            for s in (doc.get("seasons") or []) if isinstance(s, dict)]
        resp = tv_api._title_response(probe, "admin-check", {})
        if resp is None:
            out.append("Something in this title is malformed: check every field")
        elif doc.get("type") == "series" and eps and resp.episode_count < len(eps):
            shown = {e.id for s in resp.seasons for e in s.episodes}
            for e in eps:
                if e.get("id") not in shown:
                    out.append(f"Episode {e.get('id')}: its film must be in this title's own folder")
    return out


def row(doc: dict) -> dict:
    eps = _episodes(doc)
    return {"id": str(doc["_id"]), "slug": doc.get("slug"), "title": doc.get("title"), "type": doc.get("type"),
            "status": status_of(doc), "poster": doc.get("poster"), "age_rating": doc.get("age_rating"),
            "coming_at": doc.get("coming_at"), "episodes": len(eps), "captions": len(doc.get("captions") or []),
            "film": "cloudflare" if tv_cf.valid_uid(doc.get("cf_uid")) else ("spaces" if doc.get("video_url") else ""),
            "downloadable": tv_more.downloadable(doc), "notified_at": doc.get("notified_at"),
            "problems": problems(doc)}


def detail(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k not in ("_id", "makers")}
    out["id"] = str(doc["_id"])
    out["status"] = status_of(doc)
    out["problems"] = problems(doc)
    out["downloadable"] = tv_more.downloadable(doc)
    for k, v in list(out.items()):
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
    for s in out.get("seasons") or []:
        s["episodes"] = sorted([e for e in s.get("episodes") or [] if isinstance(e, dict)],
                               key=lambda e: e.get("number") or 0)
    return out


# ---------------------------------------------------------------- titles

async def list_titles(db) -> dict:
    docs = await db.tv_titles.find({}).sort("_id", -1).to_list(500)
    clips = {}
    async for c in db.tv_clips.find({}):
        clips.setdefault(str(c["title_id"]), []).append(c["_id"])
    rows = [row(d) for d in docs]
    for r in rows:
        r["clips"] = clips.get(r["id"], [])
    return {"items": rows, "statuses": list(STATUSES)}


async def get_title(db, title_id: str) -> dict:
    doc = await _get(db, title_id)
    out = detail(doc)
    out["clips"] = [c["_id"] async for c in db.tv_clips.find({"title_id": doc["_id"]})]
    return out


def _fields(body: TitleIn) -> dict:
    d = body.model_dump()
    d["release_date"] = d["release_date"] or None
    d["credits"]["cast"] = [c.strip()[:80] for c in d["credits"]["cast"] if isinstance(c, str) and c.strip()]
    d["credits"]["people"] = [{k: v for k, v in p.items() if v} for p in d["credits"]["people"]]
    return d


async def create_title(db, body: TitleIn) -> dict:
    if await db.tv_titles.find_one({"slug": body.slug}, {"_id": 1}):
        raise HTTPException(status.HTTP_409_CONFLICT, "Another title already uses that web address")
    doc = {**_fields(body), "status": "hidden", "is_active": False, "captions": [], "seasons": [],
           "created_at": datetime.utcnow()}
    res = await db.tv_titles.insert_one(doc)
    return await get_title(db, str(res.inserted_id))


async def update_title(db, title_id: str, body: TitleIn) -> dict:
    doc = await _get(db, title_id)
    other = await db.tv_titles.find_one({"slug": body.slug, "_id": {"$ne": doc["_id"]}}, {"_id": 1})
    if other:
        raise HTTPException(status.HTTP_409_CONFLICT, "Another title already uses that web address")
    f = _fields(body)
    # A pasted Spaces link replaces a Cloudflare film; an untouched field keeps it.
    if f["video_url"] and f["video_url"] != doc.get("video_url"):
        unset = {"cf_uid": "", "cf_ready": "", "download_ready": "", "download_cf_uid": "", "download_asked": ""}
    else:
        unset = {}
        if tv_cf.valid_uid(doc.get("cf_uid")):
            f["video_url"] = doc.get("video_url") or ""
    if not f["video_url"] and doc.get("type") == "series" and _episodes(doc):
        f.pop("video_url")
    upd = {"$set": {**f, "updated_at": datetime.utcnow()}}
    if unset:
        upd["$unset"] = unset
    await db.tv_titles.update_one({"_id": doc["_id"]}, upd)
    if status_of(doc) == "live":
        fresh = await _get(db, title_id)
        if problems(fresh):
            # Edits can't break a live title: put the old version back.
            await db.tv_titles.replace_one({"_id": doc["_id"]}, doc)
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "That would stop the title showing in the app: " + "; ".join(problems(fresh)))
    tv_more.clear_cache()
    return await get_title(db, title_id)


async def set_status(db, title_id: str, new: str, background) -> dict:
    if new not in STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Status must be live, hidden or coming_soon")
    doc = await _get(db, title_id)
    old = status_of(doc)
    if new == "live":
        p = problems(doc)
        if p:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not ready to go live: " + "; ".join(p))
    if new == "coming_soon" and not str(doc.get("poster") or "").startswith("https://"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add a poster before showing it as Coming soon")
    await db.tv_titles.update_one({"_id": doc["_id"]}, {"$set": {"status": new, "is_active": new == "live",
                                                                 "status_at": datetime.utcnow()}})
    reminders = 0
    if new == "live" and old == "coming_soon":
        reminders = await db.tv_reminders.count_documents({"title_id": doc["_id"], "sent": {"$ne": True}})
        if reminders:
            background(tv_more.send_reminders, db, {**doc, "status": "live"})
    tv_more.clear_cache()
    out = await get_title(db, title_id)
    out["reminders_sent_to"] = reminders
    return out


async def notify_everyone(db, title_id: str, background) -> dict:
    doc = await _get(db, title_id)
    if status_of(doc) != "live":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Make the title Live first")
    res = await db.tv_titles.update_one({"_id": doc["_id"], "notified_at": {"$exists": False}},
                                        {"$set": {"notified_at": datetime.utcnow()}})
    if not res.modified_count:
        raise HTTPException(status.HTTP_409_CONFLICT, "Everyone was already told about this title")
    background(tv_more.notify_everyone, db, doc)
    return {"sending": True}


# ---------------------------------------------------------------- episodes

async def save_episode(db, title_id: str, body: EpisodeIn) -> dict:
    doc = await _get(db, title_id)
    if doc.get("type") != "series":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only a series has episodes")
    ep_id = f"s{body.season}e{body.number}"
    seasons = [s for s in (doc.get("seasons") or []) if isinstance(s, dict)]
    old = None
    for s in seasons:
        for e in s.get("episodes") or []:
            if isinstance(e, dict) and e.get("id") == ep_id:
                old = e
    ep = {**(old or {}), "id": ep_id, "season": body.season, "number": body.number, "title": body.title,
          "synopsis": body.synopsis, "duration_minutes": body.duration_minutes, "thumbnail": body.thumbnail or None}
    if body.video_url and body.video_url != (old or {}).get("video_url"):
        ep["video_url"] = body.video_url
        for k in ("cf_uid", "cf_ready", "download_ready", "download_cf_uid", "download_asked"):
            ep.pop(k, None)
    ep.setdefault("captions", [])
    ep.setdefault("video_url", "")
    for s in seasons:
        s["episodes"] = [e for e in s.get("episodes") or [] if not (isinstance(e, dict) and e.get("id") == ep_id)]
    season = next((s for s in seasons if s.get("number") == body.season), None)
    if season is None:
        season = {"number": body.season, "title": f"Season {body.season}", "episodes": []}
        seasons.append(season)
    season["episodes"].append(ep)
    seasons = [s for s in seasons if s.get("episodes")]
    seasons.sort(key=lambda s: s.get("number") or 0)
    await _save_seasons(db, doc, seasons)
    return await get_title(db, title_id)


async def _save_seasons(db, doc: dict, seasons: list) -> None:
    upd = {"seasons": seasons, "updated_at": datetime.utcnow()}
    playable = [e for s in seasons for e in s.get("episodes") or [] if e.get("video_url")]
    if playable:
        # The title plays its first episode; an old one-film upload no longer stands in.
        upd["video_url"] = None
    await db.tv_titles.update_one({"_id": doc["_id"]}, {"$set": upd})
    if status_of(doc) == "live":
        fresh = await db.tv_titles.find_one({"_id": doc["_id"]})
        if problems(fresh):
            await db.tv_titles.replace_one({"_id": doc["_id"]}, doc)
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "That would stop the title showing in the app: " + "; ".join(problems(fresh)))


async def delete_episode(db, title_id: str, ep_id: str) -> dict:
    doc = await _get(db, title_id)
    _find_episode(doc, ep_id)
    seasons = []
    for s in doc.get("seasons") or []:
        if isinstance(s, dict):
            s = {**s, "episodes": [e for e in s.get("episodes") or [] if not (isinstance(e, dict) and e.get("id") == ep_id)]}
            if s["episodes"]:
                seasons.append(s)
    if status_of(doc) == "live" and not seasons and not doc.get("video_url"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A live series needs at least one episode; hide it first")
    await db.tv_titles.update_one({"_id": doc["_id"]}, {"$set": {"seasons": seasons}})
    return await get_title(db, title_id)


# ---------------------------------------------------------------- files

async def _set_on(db, doc: dict, ep_id: Optional[str], fields: dict, unset: Optional[list] = None) -> None:
    """Sets fields on the title, or on one of its episodes."""
    if not ep_id:
        upd = {"$set": fields}
        if unset:
            upd["$unset"] = {k: "" for k in unset}
        await db.tv_titles.update_one({"_id": doc["_id"]}, upd)
        return
    seasons = doc.get("seasons") or []
    for s in seasons:
        for e in s.get("episodes") or []:
            if isinstance(e, dict) and e.get("id") == ep_id:
                e.update(fields)
                for k in unset or []:
                    e.pop(k, None)
    await db.tv_titles.update_one({"_id": doc["_id"]}, {"$set": {"seasons": seasons}})


async def art_save(db, title_id: str, filename: str, data: bytes) -> dict:
    doc = await _get(db, title_id)
    try:
        url = await asyncio.to_thread(tv_files.save_art, doc.get("slug") or "", filename, data)
    except tv_files.FileError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return {"url": url}


async def film_upload(db, title_id: str, ep_id: Optional[str], filename: str, size: int) -> dict:
    doc = await _get(db, title_id)
    if ep_id:
        _find_episode(doc, ep_id)
    elif status_of(doc) == "live":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hide the title before replacing its film")
    name = f"{doc.get('slug')}{'-' + ep_id if ep_id else ''}-{re.sub(r'[^A-Za-z0-9._-]', '', filename or '')[:60]}"
    try:
        up = await asyncio.to_thread(tv_cf.upload_url, name, size)
    except tv_cf.CfError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    await _set_on(db, doc, ep_id, {"cf_uid": up["uid"], "video_url": up["video_url"], "cf_ready": False,
                                   "download_ready": False}, unset=["download_cf_uid"])
    return {"upload_url": up["upload_url"], "uid": up["uid"]}


async def check_film(db, title_id: str, ep_id: Optional[str]) -> dict:
    """Asks Cloudflare whether the uploaded film is ready, and the offline copy."""
    doc = await _get(db, title_id)
    src = _find_episode(doc, ep_id) if ep_id else doc
    uid, dl_uid = src.get("cf_uid"), src.get("download_cf_uid")
    if not tv_cf.valid_uid(uid) and not tv_cf.valid_uid(dl_uid):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This film isn't on Cloudflare")
    fields, out = {}, {"ready": None, "download": None}
    try:
        if tv_cf.valid_uid(uid):
            st = await asyncio.to_thread(tv_cf.status, uid)
            out.update(st)
            fields["cf_ready"] = st["ready"]
            if st["ready"] and st["duration_minutes"] and not src.get("duration_minutes"):
                fields["duration_minutes"] = st["duration_minutes"]
            if src.get("download_asked"):
                out["download"] = await asyncio.to_thread(tv_cf.download_status, uid)
        elif tv_cf.valid_uid(dl_uid):
            st = await asyncio.to_thread(tv_cf.status, dl_uid)
            out["download"] = {"ready": False, "pct": st.get("pct")}
            if st["ready"]:
                out["download"] = await asyncio.to_thread(tv_cf.enable_download, dl_uid)
    except tv_cf.CfError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if out["download"] is not None:
        fields["download_ready"] = bool(out["download"]["ready"])
    await _set_on(db, doc, ep_id, fields)
    return out


async def offline_copy(db, title_id: str, ep_id: Optional[str], filename: Optional[str], size: int = 0) -> dict:
    """Cloudflare film: ask Stream for the MP4. Spaces film: upload an MP4 of it to
    Stream (resumable), which then makes the downloadable copy."""
    doc = await _get(db, title_id)
    src = _find_episode(doc, ep_id) if ep_id else doc
    try:
        if tv_cf.valid_uid(src.get("cf_uid")):
            dl = await asyncio.to_thread(tv_cf.enable_download, src["cf_uid"])
            await _set_on(db, doc, ep_id, {"download_asked": True, "download_ready": dl["ready"]})
            return {"kind": "cloudflare", **dl}
        if not src.get("video_url"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add the film first")
        if not (filename or "").lower().endswith((".mp4", ".mov")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick an .mp4 of the film (720p is plenty)")
        name = f"{doc.get('slug')}{'-' + ep_id if ep_id else ''}-offline"
        up = await asyncio.to_thread(tv_cf.upload_url, name, size)
    except tv_cf.CfError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    await _set_on(db, doc, ep_id, {"download_cf_uid": up["uid"], "download_ready": False})
    return {"kind": "upload", "upload_url": up["upload_url"], "uid": up["uid"]}


async def add_captions(db, title_id: str, ep_id: Optional[str], language: str, label: str, text: str) -> dict:
    doc = await _get(db, title_id)
    src = _find_episode(doc, ep_id) if ep_id else doc
    try:
        if tv_cf.valid_uid(src.get("cf_uid")):
            await asyncio.to_thread(tv_cf.put_captions, src["cf_uid"], language, tv_files.full_vtt(text))
            url = f"{tv_cf.HOST}/{src['cf_uid']}/captions/{language}"
        else:
            folder = _hls_folder(src.get("video_url"))
            if not folder:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add the film first")
            url = await asyncio.to_thread(tv_files.add_spaces_subtitles, folder, text, language, label)
    except (tv_files.FileError, tv_cf.CfError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    caps = [c for c in src.get("captions") or [] if isinstance(c, dict) and c.get("language") != language]
    caps.append({"language": language, "label": (label or language).strip()[:40], "url": url})
    await _set_on(db, doc, ep_id, {"captions": caps})
    return await get_title(db, title_id)


async def remove_captions(db, title_id: str, ep_id: Optional[str], language: str) -> dict:
    doc = await _get(db, title_id)
    src = _find_episode(doc, ep_id) if ep_id else doc
    try:
        if tv_cf.valid_uid(src.get("cf_uid")):
            await asyncio.to_thread(tv_cf.delete_captions, src["cf_uid"], language)
        elif _hls_folder(src.get("video_url")):
            await asyncio.to_thread(tv_files.remove_spaces_subtitles, _hls_folder(src.get("video_url")), language)
    except (tv_files.FileError, tv_cf.CfError) as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    caps = [c for c in src.get("captions") or [] if isinstance(c, dict) and c.get("language") != language]
    await _set_on(db, doc, ep_id, {"captions": caps})
    return await get_title(db, title_id)


# ---------------------------------------------------------------- numbers

async def stats(db, days: int = 30, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    today = datetime(now.year, now.month, now.day)
    since = today - timedelta(days=days - 1)
    titles = await db.tv_titles.find({}, {"title": 1, "status": 1, "is_active": 1, "poster": 1}).to_list(500)
    ids = [t["_id"] for t in titles]

    async def group(coll, match, key="$title_id", extra=None):
        pipe = [{"$match": {**match, "title_id": {"$in": ids}}},
                {"$group": {"_id": key, "n": {"$sum": 1}, **(extra or {})}}]
        return {r["_id"]: r async for r in db[coll].aggregate(pipe)}

    plays = {r["_id"]: r async for r in db.tv_title_daily.aggregate([
        {"$match": {"day": {"$gte": since}, "title_id": {"$in": ids}}},
        {"$group": {"_id": "$title_id", "plays": {"$sum": "$plays"}}}])}
    daily = {r["_id"]: int(r["plays"]) async for r in db.tv_title_daily.aggregate([
        {"$match": {"day": {"$gte": since}}}, {"$group": {"_id": "$day", "plays": {"$sum": "$plays"}}}])}
    started = await group("tv_watch_history", {})
    prog = await group("tv_progress", {}, extra={
        "done": {"$sum": {"$cond": ["$done", 1, 0]}},
        "pct": {"$avg": {"$cond": [{"$gt": ["$duration", 0]}, {"$divide": ["$position", "$duration"]}, 0]}}})
    lists = await group("tv_list", {})
    loves = await group("tv_loves", {})
    rem = await group("tv_reminders", {"sent": {"$ne": True}})
    dls = await group("tv_downloads", {})
    strip = {r["_id"]: r async for r in db.tv_strip_daily.aggregate([
        {"$match": {"day": {"$gte": since}}},
        {"$group": {"_id": "$title_id", "shown": {"$sum": "$shown"}, "tap": {"$sum": "$tap"}}}])}
    rows = []
    for t in titles:
        i = t["_id"]
        p = prog.get(i) or {}
        rows.append({"id": str(i), "title": t.get("title"), "status": status_of(t), "poster": t.get("poster"),
                     "plays": int((plays.get(i) or {}).get("plays") or 0),
                     "started": int((started.get(i) or {}).get("n") or 0),
                     "watching": int(p.get("n") or 0), "finished": int(p.get("done") or 0),
                     "avg_pct": round(float(p["pct"]) * 100) if p.get("pct") is not None else None,
                     "my_list": int((lists.get(i) or {}).get("n") or 0), "loves": int((loves.get(i) or {}).get("n") or 0),
                     "reminders": int((rem.get(i) or {}).get("n") or 0), "downloads": int((dls.get(i) or {}).get("n") or 0),
                     "strip_shown": int((strip.get(i) or {}).get("shown") or 0),
                     "strip_taps": int((strip.get(i) or {}).get("tap") or 0)})
    rows.sort(key=lambda r: (-r["plays"], -r["started"], r["title"] or ""))
    by_day = [{"day": (since + timedelta(days=k)).date().isoformat(), "plays": daily.get(since + timedelta(days=k), 0)}
              for k in range(days)]
    return {"days": days, "items": rows, "by_day": by_day,
            "total_plays": sum(r["plays"] for r in rows), "people_started": len(await db.tv_watch_history.distinct("user_id"))}
