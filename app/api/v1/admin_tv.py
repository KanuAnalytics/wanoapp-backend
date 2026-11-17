"""Admin > Wano TV titles and Wano TV numbers (see app/services/tv_admin.py)."""

from typing import Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt

from app.api.deps import require_admin
from app.core.database import get_database
from app.services import admin_log, tv_admin, tv_more, tv_strip

router = APIRouter()
EP = Query(None, pattern=r"^s\d{1,3}e\d{1,4}$")


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(..., max_length=20)


class FileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field("", max_length=200)
    size: int = Field(0, ge=0)


class CaptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(..., min_length=2, max_length=12)
    label: str = Field("", max_length=40)
    text: str = Field(..., min_length=1, max_length=2_000_000)


class ClipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video: str = Field(..., min_length=1, max_length=300)


class RulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    after_portion: Optional[Union[StrictFloat, StrictInt]] = None
    min_posts: Optional[StrictInt] = None
    gap_posts: Optional[StrictInt] = None
    max_per_session: Optional[StrictInt] = None
    show_seconds: Optional[StrictInt] = None
    closes_per_week: Optional[StrictInt] = None


def _bg(tasks: BackgroundTasks):
    return lambda fn, *a: tasks.add_task(fn, *a)


@router.get("/titles")
async def titles(admin: str = Depends(require_admin)):
    return await tv_admin.list_titles(get_database())


@router.post("/titles", status_code=201)
async def create(body: tv_admin.TitleIn, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.create_title(db, body)
    await admin_log.record(db, admin, "tv_title_created", {"title": out["id"], "slug": body.slug})
    return out


@router.get("/titles/{title_id}")
async def one(title_id: str, admin: str = Depends(require_admin)):
    return await tv_admin.get_title(get_database(), title_id)


@router.put("/titles/{title_id}")
async def update(title_id: str, body: tv_admin.TitleIn, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.update_title(db, title_id, body)
    await admin_log.record(db, admin, "tv_title_edited", {"title": title_id})
    return out


@router.post("/titles/{title_id}/status")
async def set_status(title_id: str, body: StatusIn, tasks: BackgroundTasks, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.set_status(db, title_id, body.status, _bg(tasks))
    await admin_log.record(db, admin, "tv_title_status", {"title": title_id, "status": body.status})
    return out


@router.post("/titles/{title_id}/notify")
async def notify(title_id: str, tasks: BackgroundTasks, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.notify_everyone(db, title_id, _bg(tasks))
    await admin_log.record(db, admin, "tv_title_notified", {"title": title_id})
    return out


@router.put("/titles/{title_id}/episodes")
async def save_episode(title_id: str, body: tv_admin.EpisodeIn, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.save_episode(db, title_id, body)
    await admin_log.record(db, admin, "tv_episode_saved", {"title": title_id, "episode": f"s{body.season}e{body.number}"})
    return out


@router.delete("/titles/{title_id}/episodes/{ep_id}")
async def delete_episode(title_id: str, ep_id: str, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.delete_episode(db, title_id, ep_id[:12])
    await admin_log.record(db, admin, "tv_episode_removed", {"title": title_id, "episode": ep_id[:12]})
    return out


@router.post("/titles/{title_id}/art-file")
async def art_file(title_id: str, file: UploadFile = File(...), admin: str = Depends(require_admin)):
    """A poster or picture, sent through the server (no storage settings needed in the browser)."""
    from app.services import tv_files
    data = await file.read(tv_files.MAX_ART_BYTES + 1)
    return await tv_admin.art_save(get_database(), title_id, file.filename or "", data)


@router.post("/titles/{title_id}/film")
async def film(title_id: str, body: FileIn, episode: Optional[str] = EP, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.film_upload(db, title_id, episode, body.filename, body.size)
    await admin_log.record(db, admin, "tv_film_upload", {"title": title_id, "episode": episode, "uid": out["uid"]})
    return out


@router.post("/titles/{title_id}/film/check")
async def film_check(title_id: str, episode: Optional[str] = EP, admin: str = Depends(require_admin)):
    return await tv_admin.check_film(get_database(), title_id, episode)


@router.post("/titles/{title_id}/offline")
async def offline(title_id: str, body: FileIn, episode: Optional[str] = EP, admin: str = Depends(require_admin)):
    return await tv_admin.offline_copy(get_database(), title_id, episode, body.filename, body.size)


@router.post("/titles/{title_id}/captions")
async def captions(title_id: str, body: CaptionsIn, episode: Optional[str] = EP, admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.add_captions(db, title_id, episode, body.language, body.label, body.text)
    await admin_log.record(db, admin, "tv_captions_added", {"title": title_id, "episode": episode,
                                                             "language": body.language})
    return out


@router.delete("/titles/{title_id}/captions/{language}")
async def captions_remove(title_id: str, language: str, episode: Optional[str] = EP,
                          admin: str = Depends(require_admin)):
    db = get_database()
    out = await tv_admin.remove_captions(db, title_id, episode, language[:12])
    await admin_log.record(db, admin, "tv_captions_removed", {"title": title_id, "language": language[:12]})
    return out


def _video_id(text: str) -> str:
    import re
    m = re.search(r"([0-9a-fA-F]{24})", text or "")
    if not m:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste a post link or ID")
    return m.group(1).lower()


@router.post("/titles/{title_id}/clips")
async def add_clip(title_id: str, body: ClipIn, admin: str = Depends(require_admin)):
    db = get_database()
    vid = _video_id(body.video)
    out = await tv_more.link_clip(db, vid, title_id)
    await admin_log.record(db, admin, "tv_clip_linked", {"title": title_id, "video": vid})
    return out


@router.delete("/clips/{video_id}")
async def remove_clip(video_id: str, admin: str = Depends(require_admin)):
    db = get_database()
    vid = _video_id(video_id)
    out = await tv_more.link_clip(db, vid, None)
    await admin_log.record(db, admin, "tv_clip_unlinked", {"video": vid})
    return out


@router.get("/stats")
async def stats(days: int = Query(30, ge=1, le=60), admin: str = Depends(require_admin)):
    return await tv_admin.stats(get_database(), days)


@router.get("/strip-rules")
async def strip_rules(admin: str = Depends(require_admin)):
    return {"rules": await tv_strip.rules(get_database()), "bounds": tv_strip.RULE_BOUNDS}


@router.post("/strip-rules")
async def set_strip_rules(body: RulesIn, admin: str = Depends(require_admin)):
    db = get_database()
    try:
        out = await tv_strip.set_rules(db, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    await admin_log.record(db, admin, "tv_strip_rules", body.model_dump(exclude_none=True))
    return {"rules": out}
