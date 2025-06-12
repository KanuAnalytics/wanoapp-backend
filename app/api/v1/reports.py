"""
app/api/v1/reports.py

Video reporting API endpoints
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status, Query
from bson import ObjectId
from datetime import datetime
from app.models.report import VideoReport, ReportCreate, ReportResponse
from app.core.database import get_database
from app.api.deps import get_current_active_user
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/videos/{video_id}/report", response_model=ReportResponse, status_code=status.HTTP_201_CREATED)
async def report_video(
    video_id: str,
    report_data: ReportCreate,
    current_user: str = Depends(get_current_active_user)
):
    """
    Report a video for policy violations
    
    - Requires authentication
    - Prevents duplicate reports from same user for same video
    - Returns report details upon successful creation
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
    
    # Check if user has already reported this video
    existing_report = await db.reports.find_one({
        "video_id": ObjectId(video_id),
        "reporter_id": ObjectId(current_user)
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
        "reason": report_data.reason,
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
    except Exception as e:
        logger.error(f"Failed to create report: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit report"
        )
    
    # Increment report count on video
    await db.videos.update_one(
        {"_id": ObjectId(video_id)},
        {"$inc": {"report_count": 1}}
    )
    
    # Prepare response
    report_doc["_id"] = report_id
    report_doc["video_id"] = video_id
    report_doc["reporter_id"] = current_user
    
    return ReportResponse(**report_doc)

@router.get("/videos/{video_id}/reports", response_model=List[ReportResponse])
async def get_video_reports(
    video_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, description="Filter by report status"),
    current_user: str = Depends(get_current_active_user)
):
    """
    Get reports for a specific video
    
    - Only video creator can see reports for their videos
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
    
    if str(video["creator_id"]) != current_user:
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
        report["reporter_id"] = str(report["reporter_id"])
        reports.append(ReportResponse(**report))
    
    return reports

@router.get("/my-reports", response_model=List[ReportResponse])
async def get_my_reports(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, description="Filter by report status"),
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
    
    # Soft delete the report
    await db.reports.update_one(
        {"_id": ObjectId(report_id)},
        {
            "$set": {
                "is_active": False,
                "status": "withdrawn",
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    # Decrement report count on video
    await db.videos.update_one(
        {"_id": report["video_id"]},
        {"$inc": {"report_count": -1}}
    )

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