from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from datetime import datetime
from bson import ObjectId
from app.core.database import get_database
from app.api.deps import get_current_active_user
from app.models.draft import (
    DraftCreate, 
    DraftUpdate, 
    DraftPatch, 
    DraftResponse
)

router = APIRouter()

@router.post("/", response_model=DraftResponse, status_code=status.HTTP_201_CREATED)
async def create_draft(
    draft: DraftCreate,
    current_user: str = Depends(get_current_active_user)
):
    """
    Create a new draft
    
    - Stores video editor draft data
    - Can have multiple drafts per user
    - No expiration
    """
    db = get_database()
    
    # Create draft document
    draft_doc = {
        **draft.dict(),
        "user_id": ObjectId(current_user),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True
    }
    
    # Insert draft
    result = await db.drafts.insert_one(draft_doc)
    draft_doc["_id"] = str(result.inserted_id)
    draft_doc["user_id"] = str(draft_doc["user_id"])
    
    return DraftResponse(**draft_doc)

@router.get("/", response_model=List[DraftResponse])
async def get_my_drafts(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: str = Depends(get_current_active_user)
):
    """
    Get all drafts for current user
    
    - Returns drafts in descending order (newest first)
    - Supports pagination
    """
    db = get_database()
    
    cursor = db.drafts.find({
        "user_id": ObjectId(current_user),
        "is_active": True
    }).sort("updated_at", -1).skip(skip).limit(limit)
    
    drafts = []
    async for draft in cursor:
        draft["_id"] = str(draft["_id"])
        draft["user_id"] = str(draft["user_id"])
        drafts.append(DraftResponse(**draft))
    
    return drafts

@router.get("/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Get a specific draft by ID"""
    db = get_database()
    
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid draft ID format"
        )
    
    draft = await db.drafts.find_one({
        "_id": ObjectId(draft_id),
        "user_id": ObjectId(current_user),
        "is_active": True
    })
    
    if not draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    
    draft["_id"] = str(draft["_id"])
    draft["user_id"] = str(draft["user_id"])
    
    return DraftResponse(**draft)

@router.put("/{draft_id}", response_model=DraftResponse)
async def update_draft(
    draft_id: str,
    draft_update: DraftUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """
    Update a draft (full replacement)
    
    - Replaces all draft data
    - Requires all fields
    """
    db = get_database()
    
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid draft ID format"
        )
    
    # Verify draft exists and belongs to user
    existing_draft = await db.drafts.find_one({
        "_id": ObjectId(draft_id),
        "user_id": ObjectId(current_user),
        "is_active": True
    })
    
    if not existing_draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    
    # Update draft
    update_doc = {
        **draft_update.dict(),
        "updated_at": datetime.utcnow()
    }
    
    result = await db.drafts.update_one(
        {"_id": ObjectId(draft_id)},
        {"$set": update_doc}
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Draft update failed"
        )
    
    # Get updated draft
    updated_draft = await db.drafts.find_one({"_id": ObjectId(draft_id)})
    updated_draft["_id"] = str(updated_draft["_id"])
    updated_draft["user_id"] = str(updated_draft["user_id"])
    
    return DraftResponse(**updated_draft)

@router.patch("/{draft_id}", response_model=DraftResponse)
async def patch_draft(
    draft_id: str,
    draft_patch: DraftPatch,
    current_user: str = Depends(get_current_active_user)
):
    """
    Partially update a draft
    
    - Only updates provided fields
    - All fields are optional
    """
    db = get_database()
    
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid draft ID format"
        )
    
    # Verify draft exists and belongs to user
    existing_draft = await db.drafts.find_one({
        "_id": ObjectId(draft_id),
        "user_id": ObjectId(current_user),
        "is_active": True
    })
    
    if not existing_draft:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    
    # Build update document from provided fields only
    update_doc = draft_patch.dict(exclude_unset=True)
    
    if not update_doc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update"
        )
    
    update_doc["updated_at"] = datetime.utcnow()
    
    # Update draft
    result = await db.drafts.update_one(
        {"_id": ObjectId(draft_id)},
        {"$set": update_doc}
    )
    
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Draft update failed"
        )
    
    # Get updated draft
    updated_draft = await db.drafts.find_one({"_id": ObjectId(draft_id)})
    updated_draft["_id"] = str(updated_draft["_id"])
    updated_draft["user_id"] = str(updated_draft["user_id"])
    
    return DraftResponse(**updated_draft)

@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(
    draft_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """
    Delete a draft
    
    - Soft delete (marks as inactive)
    - Only owner can delete
    """
    db = get_database()
    
    if not ObjectId.is_valid(draft_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid draft ID format"
        )
    
    # Soft delete
    result = await db.drafts.update_one(
        {
            "_id": ObjectId(draft_id),
            "user_id": ObjectId(current_user),
            "is_active": True
        },
        {
            "$set": {
                "is_active": False,
                "deleted_at": datetime.utcnow()
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Draft not found"
        )
    
    return None

@router.get("/count/me")
async def get_draft_count(
    current_user: str = Depends(get_current_active_user)
):
    """Get total draft count for current user"""
    db = get_database()
    
    count = await db.drafts.count_documents({
        "user_id": ObjectId(current_user),
        "is_active": True
    })
    
    return {"count": count}