"""
Stuck-upload rescue.

A post becomes visible when Cloudflare's "video ready" webhook arrives. If that
message is missed (Cloudflare retries give up, the server was restarting, the
signature secret was wrong for a while), the post stays "processing" forever
and never shows anywhere. Every LOOP_SECONDS this asks Cloudflare directly
about posts that have been processing for over STUCK_AFTER_MINUTES:

- ready at Cloudflare: marked ready exactly as the webhook would
  (upload_video.mark_stream_ready: thumbnail, Recombee, creator notified once);
- failed at Cloudflare, missing there, or still processing after GIVE_UP_HOURS:
  marked failed and the creator is told once to upload again (the post stays
  hidden; a late webhook still brings it back);
- still processing: checked again later, waiting longer each time
  (BACKOFF_MINUTES), so Cloudflare isn't hammered.

Only posts with a Cloudflare video id are touched. At most MAX_PER_RUN checks
per run. Each post is claimed before it is checked, so several servers running
this at once check it once. If Cloudflare's API is down nothing is failed.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

import requests
from pymongo import ReturnDocument

from app.core.config import settings

logger = logging.getLogger(__name__)

STUCK_AFTER_MINUTES = 10
GIVE_UP_HOURS = 24
LOOKBACK_DAYS = 7
MAX_PER_RUN = 100
LOOP_SECONDS = 300
BACKOFF_MINUTES = (5, 15, 30, 60)
UID_RE = re.compile(r"^[a-f0-9]{32}$")

FAILED = "failed"
CHECKING = "checking"
RESCUED = "rescued"

FAIL_MESSAGE = "Your video couldn't be processed. Please try uploading it again."


def fetch_state(uid: str) -> dict:
    """Cloudflare's view of a Stream video: {"exists", "ready", "state", "error"}.
    Raises if Cloudflare can't be asked (so nothing is failed on our side)."""
    if not UID_RE.match(uid or ""):
        raise ValueError("bad uid")
    account, token = settings.CLOUDFLARE_ACCOUNT_ID, settings.CLOUDFLARE_STREAM_API_TOKEN
    if not account or not token:
        raise RuntimeError("Cloudflare Stream credentials not configured")
    resp = requests.get(f"https://api.cloudflare.com/client/v4/accounts/{account}/stream/{uid}",
                        headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code == 404:
        return {"exists": False, "ready": False, "state": None, "error": None}
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"Cloudflare Stream API error: {body.get('errors')}")
    result = body.get("result") or {}
    status = result.get("status") or {}
    return {"exists": True, "ready": bool(result.get("readyToStream")),
            "state": status.get("state"), "error": status.get("errorReasonCode") or status.get("errorReasonText")}


def _uid(video: dict) -> Optional[str]:
    u = video.get("stream_uid")
    if isinstance(u, str) and UID_RE.match(u):
        return u
    from app.services.thumbnails import stream_ref
    return stream_ref(video)[1]


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=BACKOFF_MINUTES[min(max(attempts, 1), len(BACKOFF_MINUTES)) - 1])


async def _claim(db, video_id, now: datetime) -> Optional[dict]:
    """Take this post for one check; None if another run has it."""
    return await db.videos.find_one_and_update(
        {"_id": video_id, "isReadyToStream": False, "upload_check.state": {"$ne": FAILED},
         "$or": [{"upload_check.next_at": {"$exists": False}}, {"upload_check.next_at": {"$lte": now}}]},
        {"$set": {"upload_check.state": CHECKING, "upload_check.next_at": now + timedelta(minutes=BACKOFF_MINUTES[0]),
                  "upload_check.last_at": now},
         "$inc": {"upload_check.attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )


async def _fail(db, video: dict, reason: str, detail: Optional[str]) -> bool:
    now = datetime.utcnow()
    done = await db.videos.find_one_and_update(
        {"_id": video["_id"], "isReadyToStream": False, "upload_check.state": {"$ne": FAILED}},
        {"$set": {"upload_check.state": FAILED, "upload_check.reason": reason,
                  "upload_check.detail": (str(detail)[:200] if detail else None), "upload_check.failed_at": now}},
    )
    if not done:
        return False
    logger.warning("Upload %s failed (%s: %s)", video["_id"], reason, detail)
    if video.get("status") in ("scheduled", "publishing"):
        return True     # never announced; the scheduled post won't publish without a video
    try:
        await db.notifications.insert_one({"recipient_id": video["creator_id"], "type": "video_failed",
                                           "post_id": video["_id"], "date": now})
        creator = await db.users.find_one({"_id": video["creator_id"]}, {"expo_push_tokens": 1})
        from app.services.expo import send_push_message
        for token in (creator or {}).get("expo_push_tokens") or []:
            try:
                # send_push_message is a blocking HTTP call.
                await asyncio.to_thread(send_push_message, token, FAIL_MESSAGE, {"screen": "profile_v2"}, None, None)
            except Exception:
                logger.warning("Push for failed upload %s did not send", video["_id"])
    except Exception:
        logger.exception("Could not notify creator of failed upload %s", video["_id"])
    return True


async def rescue_once(db) -> dict:
    out = {"checked": 0, "ready": 0, "failed": 0, "still_processing": 0, "errors": 0}
    now = datetime.utcnow()
    q = {"is_active": {"$in": [True, False]}, "isReadyToStream": False, "media_type": {"$ne": "photo"},
         "created_at": {"$lte": now - timedelta(minutes=STUCK_AFTER_MINUTES),
                        "$gte": now - timedelta(days=LOOKBACK_DAYS)},
         "upload_check.state": {"$ne": FAILED},
         "$or": [{"upload_check.next_at": {"$exists": False}}, {"upload_check.next_at": {"$lte": now}}]}
    # Live posts, plus scheduled ones (saved hidden on purpose). Deleted posts stay deleted.
    q["$and"] = [{"$or": [{"is_active": True}, {"status": {"$in": ["scheduled", "publishing"]}}]}]
    candidates = await db.videos.find(q).sort([("created_at", -1)]).limit(MAX_PER_RUN * 3).to_list(MAX_PER_RUN * 3)

    from app.routes.upload_video import mark_stream_ready
    for video in candidates:
        if out["checked"] >= MAX_PER_RUN:
            break
        uid = _uid(video)
        if not uid:
            continue
        claimed = await _claim(db, video["_id"], now)
        if not claimed:
            continue
        out["checked"] += 1
        attempts = (claimed.get("upload_check") or {}).get("attempts", 1)
        try:
            state = await asyncio.to_thread(fetch_state, uid)
        except Exception as e:
            out["errors"] += 1
            logger.warning("Could not ask Cloudflare about %s: %s", uid, e)
            await db.videos.update_one({"_id": video["_id"]},
                                       {"$set": {"upload_check.next_at": now + _backoff(attempts)}})
            continue
        if state.get("ready"):
            await mark_stream_ready(uid)
            out["ready"] += 1
            continue
        created = video.get("created_at")
        created = created.replace(tzinfo=None) if isinstance(created, datetime) and created.tzinfo else created
        too_long = isinstance(created, datetime) and created < now - timedelta(hours=GIVE_UP_HOURS)
        if not state.get("exists"):
            out["failed"] += int(await _fail(db, video, "missing", None))
        elif state.get("state") == "error":
            out["failed"] += int(await _fail(db, video, "cloudflare_error", state.get("error")))
        elif too_long:
            out["failed"] += int(await _fail(db, video, "timeout", state.get("state")))
        else:
            out["still_processing"] += 1
            await db.videos.update_one({"_id": video["_id"]},
                                       {"$set": {"upload_check.next_at": now + _backoff(attempts)}})
    if out["checked"]:
        logger.info("Stuck-upload rescue: %s", out)
    return out


class RescueLoop:
    def __init__(self):
        self._task = None

    async def _run(self):
        from app.core.database import get_database
        while True:
            try:
                await asyncio.sleep(LOOP_SECONDS)
                await rescue_once(get_database())
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Stuck-upload rescue loop error")

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None


rescue_loop = RescueLoop()
