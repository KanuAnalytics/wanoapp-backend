"""
app/api/v1/reports.py

Video reporting API endpoints
"""

from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends, status, Query
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from datetime import datetime
from app.models.report import VideoReport, ReportCreate, ReportResponse
from app.core.database import get_database
from app.api.deps import get_current_active_user, require_admin
from app.services import moderation
from pydantic import BaseModel, Field
from typing import Literal
from app.services.email_service import email_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

REPORT_NOTIFICATION_EMAIL = "eilmer@wanoafrica.com"


class CreatorReportResponse(ReportResponse):
    """Report as the video's creator sees it: the reporter stays anonymous."""
    reporter_id: Optional[str] = None


async def _is_admin(db, user_id: str) -> bool:
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"user_type": 1})
    return bool(user) and str(user.get("user_type", "")).lower() == "admin"


async def _send_report_email(video: dict, reporter_id: str, video_id: str):
    """Background task: notify the admin inbox about a new report. Never raises."""
    try:
        db = get_database()
        creator = await db.users.find_one({"_id": video.get("creator_id")})
        reporter = await db.users.find_one({"_id": ObjectId(reporter_id)})
        if creator and reporter:
            video_link = f"https://share.wanoafrica.com/pages/feed?videoId={video_id}&single=true"
            await email_service.send_report_notification(
                to_email=REPORT_NOTIFICATION_EMAIL,
                creator=creator,
                reporter=reporter,
                video=video,
                video_link=video_link
            )
        else:
            logger.warning("Report notification email skipped: creator or reporter not found.")
    except Exception as e:
        logger.warning(f"Report notification email failed: {str(e)}")

@router.post("/videos/{video_id}/report", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def report_video(
    video_id: str,
    report_data: ReportCreate,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_active_user)
):
    """
    Report a video for policy violations
    
    - Requires authentication
    - Prevents duplicate reports from same user for same video
    - Returns report details upon successful creation
    - Reason field is now optional
    """
    db = get_database()
    
    # Validate video_id format
    if not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID format"
        )
    
    # Check if video exists and is active
    video = await db.videos.find_one({
        "_id": ObjectId(video_id),
        "is_active": True
    })
    
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    # Check if user already has an open report on this video (a withdrawn
    # report doesn't stop them reporting again)
    existing_report = await db.reports.find_one({
        "video_id": ObjectId(video_id),
        "reporter_id": ObjectId(current_user),
        "is_active": True
    })
    
    if existing_report:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have already reported this video"
        )
    
    # Validate timestamp against video duration if provided
    if report_data.timestamp is not None:
        video_duration = video.get("metadata", {}).get("duration")
        if video_duration and report_data.timestamp > video_duration:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Timestamp cannot exceed video duration ({video_duration} seconds)"
            )
    
    # Create report document
    report_doc = {
        "video_id": ObjectId(video_id),
        "reporter_id": ObjectId(current_user),
        "reason": report_data.reason,  # Can be None
        "category": report_data.category,
        "comment": report_data.comment,
        "timestamp": report_data.timestamp,
        "status": "pending",
        "reviewed_by": None,
        "reviewed_at": None,
        "admin_notes": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True
    }
    
    # Insert report
    try:
        result = await db.reports.insert_one(report_doc)
        report_id = str(result.inserted_id)
    except DuplicateKeyError:
        # Lost a race with a concurrent identical report.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have already reported this video"
        )
    except Exception as e:
        logger.error(f"Failed to create report: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit report"
        )
    
    # Increment report count on video (only reached after a successful insert)
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$inc": {"report_count": 1}}
    )
    # Trusted reports pause first viewers; enough of them hold it for review.
    try:
        await moderation.on_report(db, video, current_user, report_id)
    except Exception:
        logger.exception("Safety check failed for report %s", report_id)
    
    # Prepare response
    report_doc["_id"] = report_id
    report_doc["video_id"] = video_id
    report_doc["reporter_id"] = current_user

    # Send report notification email after the response (doesn't hold up the request)
    background_tasks.add_task(_send_report_email, video, current_user, video_id)

    return ReportResponse(**report_doc)

@router.get("/videos/{video_id}/reports", response_model=List[CreatorReportResponse])
async def get_video_reports(
    video_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, max_length=50, description="Filter by report status"),
    current_user: str = Depends(get_current_active_user)
):
    """
    Get reports for a specific video
    
    - Only video creator (or an admin) can see reports for their videos
    - reporter_id is only included for admins; creators get null
    - Supports pagination and status filtering
    """
    db = get_database()
    
    # Validate video_id format
    if not ObjectId.is_valid(video_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID format"
        )
    
    # Check if video exists and user is the creator
    video = await db.videos.find_one({
        "_id": ObjectId(video_id),
        "is_active": True
    })
    
    if not video:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found"
        )
    
    is_admin = await _is_admin(db, current_user)
    if str(video.get("creator_id")) != current_user and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view reports for your own videos"
        )
    
    # Build query
    query = {
        "video_id": ObjectId(video_id),
        "is_active": True
    }
    
    if status_filter:
        query["status"] = status_filter
    
    # Get reports with pagination
    cursor = db.reports.find(query).skip(skip).limit(limit).sort("created_at", -1)
    
    reports = []
    async for report in cursor:
        report["_id"] = str(report["_id"])
        report["video_id"] = str(report["video_id"])
        if is_admin:
            report["reporter_id"] = str(report["reporter_id"])
        else:
            report.pop("reporter_id", None)
        reports.append(CreatorReportResponse(**report))
    
    return reports

@router.get("/my-reports", response_model=List[ReportResponse])
async def get_my_reports(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, max_length=50, description="Filter by report status"),
    current_user: str = Depends(get_current_active_user)
):
    """
    Get current user's submitted reports
    
    - Shows all reports submitted by the current user
    - Supports pagination and status filtering
    """
    db = get_database()
    
    # Build query
    query = {
        "reporter_id": ObjectId(current_user),
        "is_active": True
    }
    
    if status_filter:
        query["status"] = status_filter
    
    # Get reports with pagination
    cursor = db.reports.find(query).skip(skip).limit(limit).sort("created_at", -1)
    
    reports = []
    async for report in cursor:
        report["_id"] = str(report["_id"])
        report["video_id"] = str(report["video_id"])
        report["reporter_id"] = str(report["reporter_id"])
        reports.append(ReportResponse(**report))
    
    return reports

@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw_report(
    report_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """
    Withdraw a report (soft delete)
    
    - Only the reporter can withdraw their own report
    - Only pending reports can be withdrawn
    """
    db = get_database()
    
    # Validate report_id format
    if not ObjectId.is_valid(report_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid report ID format"
        )
    
    # Find the report
    report = await db.reports.find_one({
        "_id": ObjectId(report_id),
        "is_active": True
    })
    
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found"
        )
    
    # Check if current user is the reporter
    if str(report["reporter_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only withdraw your own reports"
        )
    
    # Check if report is still pending
    if report["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending reports can be withdrawn"
        )
    
    # Soft delete the report; only the request that actually withdraws it
    # lowers the video's report count.
    result = await db.reports.update_one(
        {"_id": ObjectId(report_id), "is_active": True, "status": "pending"},
        {
            "$set": {
                "is_active": False,
                "status": "withdrawn",
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    if result.modified_count:
        await db.videos.update_one(
            {"_id": report["video_id"], "report_count": {"$gt": 0}},
            {"$inc": {"report_count": -1}}
        )
        try:
            await moderation.on_withdraw(db, report)
        except Exception:
            logger.exception("Safety check failed for withdrawn report %s", report_id)

@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report_details(
    report_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """
    Get details of a specific report
    
    - Only the reporter can view their own report details
    """
    db = get_database()
    
    # Validate report_id format
    if not ObjectId.is_valid(report_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid report ID format"
        )
    
    # Find the report
    report = await db.reports.find_one({
        "_id": ObjectId(report_id),
        "is_active": True
    })
    
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found"
        )
    
    # Check if current user is the reporter
    if str(report["reporter_id"]) != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own reports"
        )
    
    # Prepare response
    report["_id"] = str(report["_id"])
    report["video_id"] = str(report["video_id"])
    report["reporter_id"] = str(report["reporter_id"])
    
    return ReportResponse(**report)


# ------------------------------------------------------------------ admin review

class ModerationItem(BaseModel):
    id: str
    creator_id: Optional[str] = None
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    state: Optional[str] = None
    flags: int = 0
    reports: int = 0
    categories: dict = {}
    created_at: Optional[datetime] = None
    held_at: Optional[datetime] = None


class Decision(BaseModel):
    action: Literal["restore", "remove"]
    note: Optional[str] = Field(None, max_length=500)


@router.get("/admin/queue", response_model=List[ModerationItem])
async def moderation_queue(
    limit: int = Query(50, ge=1, le=200),
    admin_id: str = Depends(require_admin),
):
    """Posts waiting for review: held ones first, then the most flagged."""
    db = get_database()
    q = {"is_active": True, "$or": [{"moderation.state": moderation.HELD}, {"moderation.flags": {"$gt": 0}}]}
    docs = await db.videos.find(q, {"title": 1, "urls.thumbnail": 1, "creator_id": 1, "created_at": 1,
                                    "moderation": 1}).limit(1000).to_list(1000)

    def key(d):
        m = d.get("moderation") or {}
        return (0 if m.get("state") == moderation.HELD else 1, -(m.get("flags") or 0))
    docs.sort(key=key)
    docs = docs[:limit]
    counts = {}
    async for r in db.reports.find({"video_id": {"$in": [d["_id"] for d in docs]}, "status": "pending",
                                    "is_active": True}, {"video_id": 1, "category": 1}):
        c = counts.setdefault(r["video_id"], {"n": 0, "cats": {}})
        c["n"] += 1
        cat = str(r.get("category"))
        c["cats"][cat] = c["cats"].get(cat, 0) + 1
    out = []
    for d in docs:
        m = d.get("moderation") or {}
        urls = d.get("urls") if isinstance(d.get("urls"), dict) else {}
        c = counts.get(d["_id"], {"n": 0, "cats": {}})
        out.append(ModerationItem(
            id=str(d["_id"]), creator_id=str(d.get("creator_id")) if d.get("creator_id") else None,
            title=d.get("title") if isinstance(d.get("title"), str) else None,
            thumbnail=urls.get("thumbnail") if isinstance(urls.get("thumbnail"), str) else None,
            state=m.get("state"), flags=int(m.get("flags") or 0), reports=c["n"], categories=c["cats"],
            created_at=d.get("created_at") if isinstance(d.get("created_at"), datetime) else None,
            held_at=m.get("held_at") if isinstance(m.get("held_at"), datetime) else None,
        ))
    return out


@router.post("/admin/videos/{video_id}/decision")
async def moderation_decision(
    video_id: str,
    body: Decision,
    admin_id: str = Depends(require_admin),
):
    """restore: back to its old privacy, flags reset. remove: delete the post."""
    if not ObjectId.is_valid(video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video ID format")
    db = get_database()
    video = await db.videos.find_one({"_id": ObjectId(video_id), "is_active": True})
    if not video:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    after = await moderation.decide(db, video, body.action, admin_id, body.note)
    return {"id": video_id, "state": (after.get("moderation") or {}).get("state"),
            "privacy": after.get("privacy"), "is_active": after.get("is_active")}
