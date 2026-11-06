"""
Wano TV endpoints

app/api/v1/tv.py

The app downloads the whole catalogue and searches/filters it on the client,
so there is no server-side search or home layout here.
"""

import logging
from datetime import datetime
from typing import List, Optional
from bson import ObjectId
from pydantic import ValidationError
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from app.api.deps import get_current_active_user, get_optional_active_user
from app.core.database import get_database
from app.models.tv import TvEpisode, TvGenreResponse, TvMaker, TvSeason, TvTitleResponse, TvWatchResponse
from app.services import recommendations, tv_popularity, tv_stream

logger = logging.getLogger(__name__)

router = APIRouter()

# How many titles the History tab shows.
HISTORY_LIMIT = 50


def _sign(url, title_id: str, viewer: str, folder: Optional[str]):
    """(url to show, ok). Our Spaces files inside the title's folder get a signed
    link; Spaces files elsewhere are refused; other hosts pass through."""
    key = tv_stream.spaces_key(url)
    if not key:
        return url, True
    if not folder or tv_stream.title_folder(key) != folder:
        return url, False
    return (tv_stream.stream_url(title_id, viewer, key) or url), True


def _sign_captions(caps, title_id: str, viewer: str, folder: Optional[str]) -> list:
    out = []
    for c in caps or []:
        if isinstance(c, dict):
            url, ok = _sign(c.get("url"), title_id, viewer, folder)
            if ok:
                c = {**c, "url": url}
        out.append(c)
    return out


def _protect(data: dict, viewer: str) -> dict:
    """Films on our Spaces get an expiring signed link instead of the public file."""
    folder = tv_stream.own_folder(data)
    key = tv_stream.spaces_key(data.get("video_url"))
    if key:
        url, ok = _sign(data["video_url"], data["id"], viewer, folder)
        if ok:
            data["video_url"] = url
            data["captions"] = _sign_captions(data.get("captions"), data["id"], viewer, folder)
    return data


def _episodes(data: dict, viewer: str) -> None:
    """Series: valid episodes only, in order, with signed links; the title plays
    its first episode. A series with nothing playable raises ValueError."""
    if data.get("type") != "series" or (not data.get("seasons") and data.get("video_url")):
        data.pop("seasons", None)            # a movie, or a series still shipped as one film
        return
    folder = tv_stream.own_folder(data)
    seasons, seen = [], set()
    for season in data.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        eps = []
        for ep in season.get("episodes") or []:
            if not isinstance(ep, dict) or ep.get("id") in seen:
                continue
            url, ok = _sign(ep.get("video_url"), data["id"], viewer, folder)
            if not ok:
                logger.warning("Episode %s of %s points outside its title", ep.get("id"), data["id"])
                continue
            try:
                eps.append(TvEpisode(**{**ep, "video_url": url,
                                        "captions": _sign_captions(ep.get("captions"), data["id"], viewer, folder)}))
                seen.add(ep.get("id"))
            except (ValidationError, TypeError) as e:
                logger.warning("Skipping invalid episode %s of %s: %s", ep.get("id"), data["id"], e)
        if not eps:
            continue
        eps.sort(key=lambda e: e.number)
        try:
            seasons.append(TvSeason(number=season.get("number"), title=season.get("title"), episodes=eps))
        except (ValidationError, TypeError):
            continue
    seasons.sort(key=lambda s: s.number)
    if not seasons:
        raise ValueError("series with no playable episodes")
    data["seasons"] = seasons
    data["episode_count"] = sum(len(s.episodes) for s in seasons)
    data["video_url"] = seasons[0].episodes[0].video_url


# Credits shown per title (and looked up) at most.
MAX_CREDIT_PEOPLE = 200


def _credit_lookup(docs: List[dict]):
    """Every username / user id named in these titles' credits, for one query."""
    names, ids = set(), set()
    for doc in docs:
        credits = doc.get("credits") if isinstance(doc.get("credits"), dict) else {}
        for p in (credits.get("people") or [])[:MAX_CREDIT_PEOPLE]:
            if not isinstance(p, dict):
                continue
            if isinstance(p.get("username"), str) and 0 < len(p["username"].strip()) <= 64:
                # Usernames are stored as typed; match as written or lower case.
                names.update({p["username"].strip(), p["username"].strip().lower()})
            if isinstance(p.get("user_id"), str) and ObjectId.is_valid(p["user_id"]):
                ids.add(ObjectId(p["user_id"]))
        for m in (doc.get("makers") or [])[:MAX_CREDIT_PEOPLE]:   # stored makers: {user_id, role}
            uid = m.get("user_id") if isinstance(m, dict) else None
            if isinstance(uid, ObjectId):
                ids.add(uid)
            elif isinstance(uid, str) and ObjectId.is_valid(uid):
                ids.add(ObjectId(uid))
    return names, ids


async def _profiles(db, docs: List[dict]) -> dict:
    names, ids = _credit_lookup(docs)
    if not names and not ids:
        return {}
    ors = []
    if names:
        ors.append({"username": {"$in": sorted(names)}})
    if ids:
        ors.append({"_id": {"$in": list(ids)}})
    users = await db.users.find(
        {"$or": ors, "is_active": True},
        {"username": 1, "display_name": 1, "profile_picture": 1},
    ).to_list(length=None)
    out = {}
    for u in users:
        prof = {"id": str(u["_id"]), "username": u.get("username"),
                "display_name": u.get("display_name"), "profile_picture": u.get("profile_picture")}
        out["id:" + prof["id"]] = prof
        if isinstance(u.get("username"), str):
            out["x:" + u["username"]] = prof
            out.setdefault("u:" + u["username"].lower(), prof)
    return out


def _credits(data: dict, profiles: dict) -> None:
    credits = data.get("credits")
    if not isinstance(credits, dict):
        return
    people = []
    for p in (credits.get("people") or [])[:MAX_CREDIT_PEOPLE]:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not isinstance(p.get("role"), str):
            continue
        prof = None
        if isinstance(p.get("user_id"), str):
            prof = profiles.get("id:" + p["user_id"])
        if prof is None and isinstance(p.get("username"), str):
            name = p["username"].strip()
            prof = profiles.get("x:" + name) or profiles.get("u:" + name.lower())
        people.append({"name": p["name"], "role": p["role"], "profile": prof})
    data["credits"] = {**credits, "people": people}
    data["makers"] = [{"user_id": x["profile"]["id"], "username": x["profile"]["username"],
                       "display_name": x["profile"].get("display_name"),
                       "profile_picture": x["profile"].get("profile_picture"), "role": x["role"]}
                      for x in people if x["profile"]]


def _makers(doc: dict, profiles: dict) -> List[TvMaker]:
    """Makers stored on the title as {user_id, role}, filled in from their
    accounts; deactivated or missing accounts are left out."""
    out, seen = [], set()
    for m in (doc.get("makers") or [])[:MAX_CREDIT_PEOPLE]:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            continue
        prof = profiles.get("id:" + str(m.get("user_id")))
        if not prof or prof["id"] in seen:
            continue
        seen.add(prof["id"])
        out.append(TvMaker(user_id=prof["id"], username=(prof.get("username") or "").strip(),
                           display_name=prof.get("display_name"), profile_picture=prof.get("profile_picture"),
                           role=m["role"]))
    return out


def _title_response(doc: dict, viewer: str = "", profiles: Optional[dict] = None,
                    played: Optional[dict] = None) -> Optional[TvTitleResponse]:
    """The title as the app sees it, or None (logged) if the document is malformed,
    so one bad catalogue entry can't 500 the whole list."""
    raw_id = doc.get("_id")
    try:
        data = {**doc, "id": str(doc["_id"])}
        data.pop("_id", None)
        data = _protect(data, viewer)
        _episodes(data, viewer)
        data["makers"] = _makers(doc, profiles or {})
        _credits(data, profiles or {})
        if played is not None:
            data["popularity"] = int(round(tv_popularity.score(doc, played)))
        return TvTitleResponse(**data)
    except (ValidationError, KeyError, TypeError, ValueError) as e:
        logger.warning("Skipping invalid tv_titles document %s: %s", raw_id, e)
        return None


def _genre_response(doc: dict) -> Optional[TvGenreResponse]:
    try:
        return TvGenreResponse(name=doc["name"], image=doc["image"])
    except (ValidationError, KeyError, TypeError, ValueError) as e:
        logger.warning("Skipping invalid tv_genres document %s: %s", doc.get("_id"), e)
        return None


def _title_object_id(title_id: str) -> ObjectId:
    if not ObjectId.is_valid(title_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid title ID",
        )
    return ObjectId(title_id)


@router.get("/titles", response_model=List[TvTitleResponse])
async def get_titles(current_user: Optional[str] = Depends(get_optional_active_user)):
    """Every active title, most popular first. Logged out: send authenticated=false."""
    db = get_database()
    docs = await db.tv_titles.find({"is_active": True}).to_list(length=None)
    played = await tv_popularity.scores(db)
    docs.sort(key=lambda d: (-tv_popularity.score(d, played), str(d["_id"])))
    profiles = await _profiles(db, docs)
    titles = (_title_response(doc, current_user or "anon", profiles, played) for doc in docs)
    return [t for t in titles if t is not None]


@router.get("/stream/{token}/{path:path}", include_in_schema=False)
async def stream_playlist(token: str, path: str):
    """Signed film playback (see app/services/tv_stream.py). No login header:
    the token in the link is the permission, and it expires."""
    db = get_database()
    try:
        body = await tv_stream.serve(db, token, path)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This link has expired")
    except tv_stream.NotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    except Exception:
        logger.exception("Could not serve Wano TV playlist %s", path)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not load the video")
    return Response(body, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "private, max-age=60"})


@router.get("/genres", response_model=List[TvGenreResponse])
async def get_genres(current_user: Optional[str] = Depends(get_optional_active_user)):
    """Every genre in display order, including ones with no titles yet."""
    db = get_database()
    docs = await db.tv_genres.find({"is_active": True}).sort("order", 1).to_list(
        length=None
    )
    genres = (_genre_response(doc) for doc in docs)
    return [g for g in genres if g is not None]


@router.get("/history", response_model=List[TvTitleResponse])
async def get_history(current_user: str = Depends(get_current_active_user)):
    """Titles the user has started watching, most recent first."""
    db = get_database()
    rows = await db.tv_watch_history.find(
        {"user_id": ObjectId(current_user)}, {"title_id": 1, "last_episode": 1}
    ).sort("watched_at", -1).limit(HISTORY_LIMIT).to_list(length=None)

    title_ids = [row["title_id"] for row in rows]
    docs = await db.tv_titles.find(
        {"_id": {"$in": title_ids}, "is_active": True}
    ).to_list(length=None)
    by_id = {doc["_id"]: doc for doc in docs}
    profiles = await _profiles(db, docs)

    # Keep the history order; skip titles that have since been taken down.
    out = []
    for row in rows:
        doc = by_id.get(row["title_id"])
        t = _title_response(doc, current_user, profiles) if doc else None
        if t is not None:
            if isinstance(row.get("last_episode"), str) and any(
                    e.id == row["last_episode"] for season in t.seasons for e in season.episodes):
                t.last_episode = row["last_episode"]
            out.append(t)
    return out


def _has_episode(doc: dict, episode: str) -> bool:
    for season in doc.get("seasons") or []:
        if isinstance(season, dict):
            for ep in season.get("episodes") or []:
                if isinstance(ep, dict) and ep.get("id") == episode:
                    return True
    return False


@router.post("/history/{title_id}", response_model=TvWatchResponse)
async def record_watch(
    title_id: str,
    episode: Optional[str] = Query(None, pattern=r"^s\d{1,3}e\d{1,4}$"),
    current_user: str = Depends(get_current_active_user),
):
    """Called when the user starts playing a title (and, for a series, which
    episode); moves it to the top of History. Counts as a play."""
    db = get_database()
    object_id = _title_object_id(title_id)

    doc = await db.tv_titles.find_one(
        {"_id": object_id, "is_active": True}, {"_id": 1, "seasons": 1}
    )
    if not doc or (episode and not _has_episode(doc, episode)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Title not found",
        )

    now = datetime.utcnow()
    update = {"$set": {"watched_at": now}, "$setOnInsert": {"first_watched_at": now}}
    if episode:
        update["$set"]["last_episode"] = episode
    await db.tv_watch_history.update_one(
        {"user_id": ObjectId(current_user), "title_id": object_id}, update, upsert=True,
    )
    await tv_popularity.record_play(db, object_id, current_user, logged_in=True)
    return TvWatchResponse(title_id=title_id, watched_at=now, episode=episode)


@router.post("/titles/{title_id}/play")
async def record_play(
    title_id: str,
    request: Request,
    current_user: Optional[str] = Depends(get_optional_active_user),
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """Called when playback starts, logged in or not. Feeds popularity; a viewer
    counts once per title per day. Logged out with no device id: not counted."""
    from app.api.v1.videos import _client_ip
    db = get_database()
    object_id = _title_object_id(title_id)
    if not await db.tv_titles.find_one({"_id": object_id, "is_active": True}, {"_id": 1}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Title not found")
    viewer = current_user or recommendations.anon_id(x_device_id)
    if not viewer:
        return {"counted": False}
    counted = await tv_popularity.record_play(db, object_id, viewer, logged_in=bool(current_user),
                                              ip=_client_ip(request))
    return {"counted": counted}
