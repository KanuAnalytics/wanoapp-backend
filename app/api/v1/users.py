"""
User CRUD operations

app/api/v1/users.py

"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status
from bson import ObjectId
from datetime import datetime
from app.models.base import PyObjectId
from app.models.user import StandardUser, ArtistUser, AdvertiserUser, AdminUser, UserType
from app.core.database import get_database
from app.core.security import get_password_hash, create_verification_token
from app.api.deps import get_current_active_user
from pydantic import BaseModel, Field, EmailStr
from app.services.email_service import email_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter()

class UserCreate(BaseModel):
    username: str
    email: EmailStr
    password: str
    display_name: str
    user_type: UserType = UserType.STANDARD
    localization: dict
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = Field(default_factory=list)

class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    profile_picture: Optional[str] = None
    localization: Optional[dict] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = None

class UserResponse(BaseModel):
    id: str = Field(alias="_id")
    username: str
    email: str
    display_name: str
    user_type: UserType
    is_verified: bool = False
    created_at: datetime
    followers_count: int = 0
    following_count: int = 0
    videos_count: int = 0
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)
    
    class Config:
        populate_by_name = True

class TagsUpdate(BaseModel):
    tags: List[str] = Field(..., description="List of user interest tags")

@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate):
    """Create a new user"""
    db = get_database()
    
    # Check if username or email already exists
    existing_user = await db.users.find_one({
        "$or": [
            {"username": user.username},
            {"email": user.email}
        ]
    })
    
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    
    # Generate verification token
    verification_data = create_verification_token(user.email)
    
    # Create user document
    user_doc = {
        **user.dict(exclude={"password"}),
        "password_hash": get_password_hash(user.password),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "is_verified": False,  # Not verified by default
        "verification_token": verification_data["token"],
        "verification_token_expires": verification_data["expires"],
        "verified_at": None,
        "followers_count": 0,
        "following_count": 0,
        "videos_count": 0,
        "likes_count": 0,
        "gender": user.gender,
        "date_of_birth": user.date_of_birth,
        "tags": user.tags if user.tags else []
    }
    
    # Add type-specific fields
    if user.user_type == UserType.STANDARD:
        user_doc.update({
            "bookmarked_videos": [],
            "liked_videos": [],
            "following": [],
            "followers": []
        })
    
    result = await db.users.insert_one(user_doc)
    user_doc["_id"] = str(result.inserted_id)
    
    # Send verification email (non-blocking)
    try:
        email_sent = await email_service.send_verification_email(
            user.email,
            user.username,
            verification_data["token"]
        )
        
        if not email_sent:
            logger.warning(f"Failed to send verification email to {user.email}, but user was created successfully")
    except Exception as e:
        logger.error(f"Error sending verification email: {e}")
        # Don't fail registration if email fails
    
    return UserResponse(**user_doc)

@router.get("/", response_model=List[UserResponse])
async def get_users(
    skip: int = 0,
    limit: int = 20,
    user_type: Optional[UserType] = None
):
    """Get all users with pagination"""
    db = get_database()
    
    query = {"is_active": True}
    if user_type:
        query["user_type"] = user_type
    
    cursor = db.users.find(query).skip(skip).limit(limit)
    users = []
    async for user in cursor:
        user["_id"] = str(user["_id"])
        users.append(UserResponse(**user))
    
    return users

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: str):
    """Get a specific user by ID"""
    db = get_database()
    
    if not ObjectId.is_valid(user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid user ID"
        )
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    user_update: UserUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update a user (only the user themselves can update)"""
    db = get_database()
    
    # Verify user is updating their own profile
    if user_id != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only update your own profile"
        )
    
    update_data = {
        k: v for k, v in user_update.dict(exclude_unset=True).items()
    }
    update_data["updated_at"] = datetime.utcnow()
    
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Soft delete a user"""
    db = get_database()
    
    # Only allow users to delete their own account (or admins)
    if user_id != current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only delete your own account"
        )
    
    result = await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_active": False, "updated_at": datetime.utcnow()}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

@router.post("/{user_id}/follow", status_code=status.HTTP_200_OK)
async def follow_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Follow a user"""
    db = get_database()
    
    if user_id == current_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot follow yourself"
        )
    
    # Check if target user exists
    target_user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Check if already following
    current_user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if ObjectId(user_id) in current_user_doc.get("following", []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already following this user"
        )
    
    # Add to following list
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$addToSet": {"following": ObjectId(user_id)},
            "$inc": {"following_count": 1}
        }
    )
    
    # Add to followers list
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$addToSet": {"followers": ObjectId(current_user)},
            "$inc": {"followers_count": 1}
        }
    )
    
    return {"message": "Successfully followed user"}

@router.delete("/{user_id}/follow", status_code=status.HTTP_200_OK)
async def unfollow_user(
    user_id: str,
    current_user: str = Depends(get_current_active_user)
):
    """Unfollow a user"""
    db = get_database()
    
    # Check if actually following
    current_user_doc = await db.users.find_one({"_id": ObjectId(current_user)})
    if ObjectId(user_id) not in current_user_doc.get("following", []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not following this user"
        )
    
    # Remove from following list
    await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$pull": {"following": ObjectId(user_id)},
            "$inc": {"following_count": -1}
        }
    )
    
    # Remove from followers list
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$pull": {"followers": ObjectId(current_user)},
            "$inc": {"followers_count": -1}
        }
    )
    
    return {"message": "Successfully unfollowed user"}

@router.get("/me/profile", response_model=UserResponse)
async def get_my_profile(current_user: str = Depends(get_current_active_user)):
    """Get current user's profile"""
    db = get_database()
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.put("/me/tags", response_model=UserResponse)
async def update_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Update user's interest tags (replaces all existing tags)"""
    db = get_database()
    
    # Normalize tags
    normalized_tags = []
    for tag in tags_update.tags:
        normalized_tag = tag.strip().lower()
        if normalized_tag and normalized_tag not in normalized_tags:
            normalized_tags.append(normalized_tag)
    
    # Update user tags
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$set": {
                "tags": normalized_tags,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.post("/me/tags/add", response_model=UserResponse)
async def add_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Add tags to user's interests without replacing existing ones"""
    db = get_database()
    
    # Normalize new tags
    new_tags = [tag.strip().lower() for tag in tags_update.tags if tag.strip()]
    
    # Add tags using $addToSet to avoid duplicates
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$addToSet": {"tags": {"$each": new_tags}},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)

@router.delete("/me/tags", response_model=UserResponse)
async def remove_user_tags(
    tags_update: TagsUpdate,
    current_user: str = Depends(get_current_active_user)
):
    """Remove specific tags from user's interests"""
    db = get_database()
    
    # Normalize tags to remove
    tags_to_remove = [tag.strip().lower() for tag in tags_update.tags if tag.strip()]
    
    # Remove tags using $pull
    result = await db.users.update_one(
        {"_id": ObjectId(current_user)},
        {
            "$pull": {"tags": {"$in": tags_to_remove}},
            "$set": {"updated_at": datetime.utcnow()}
        }
    )
    
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    user["_id"] = str(user["_id"])
    return UserResponse(**user)