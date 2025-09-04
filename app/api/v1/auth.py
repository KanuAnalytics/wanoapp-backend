"""
Authentication endpoints

app/api/v1/auth.py

"""
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from app.core.config import settings
from app.core.database import get_database
from app.core.security import verify_password, get_password_hash, create_access_token, create_verification_token
from app.services.email_service import email_service
from pydantic import BaseModel, EmailStr, validator, Field
import logging


logger = logging.getLogger(__name__)

router = APIRouter()

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: str
    username: str
    is_verified: bool

class LoginRequest(BaseModel):
    username: str
    password: str

class VerifyEmailRequest(BaseModel):
    token: str

class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    """Request a password reset OTP for a user identified by username or email."""
    username_or_email: str = Field(..., min_length=3, max_length=100)


# --- Inserted models for OTP verification and password reset ---
class VerifyResetOtpRequest(BaseModel):
    """Verify a password reset OTP for a user."""
    username_or_email: str = Field(..., min_length=3, max_length=100)
    otp: str = Field(..., min_length=6, max_length=6)

class ResetPasswordRequest(BaseModel):
    """Reset password using a valid OTP."""
    username_or_email: str = Field(..., min_length=3, max_length=100)
    otp: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30, pattern="^[a-zA-Z0-9_]+$")
    email: EmailStr
    password: str = Field(..., min_length=6)
    display_name: str = Field(..., min_length=1, max_length=100)
    localization: dict
    
    # Optional demographic fields
    gender: Optional[str] = None
    date_of_birth: Optional[datetime] = None
    tags: Optional[List[str]] = Field(default_factory=list)
    
    @validator('gender')
    def validate_gender(cls, v):
        if v is not None:
            valid_genders = ['male', 'female', 'other', 'prefer_not_to_say']
            if v.lower() not in valid_genders:
                raise ValueError(f'Gender must be one of: {", ".join(valid_genders)}')
            return v.lower()
        return v
    
    @validator('tags')
    def validate_tags(cls, v):
        if v is not None:
            # Normalize tags: lowercase, strip whitespace, remove duplicates
            normalized_tags = []
            for tag in v:
                normalized_tag = tag.strip().lower()
                if normalized_tag and normalized_tag not in normalized_tags:
                    normalized_tags.append(normalized_tag)
            return normalized_tags
        return v
    

class RegisterResponse(BaseModel):
    access_token: str
    token_type: str
    user_id: str
    username: str
    is_verified: bool
    is_new_user: bool = True  # Indicates this is a new registration
    verification_email_sent: bool = False  # Indicates if verification email was sent


@router.post("/register", response_model=RegisterResponse)
async def register(request: RegisterRequest):
    """Register a new user and return access token"""
    db = get_database()
    
    # Check if username or email already exists
    existing_user = await db.users.find_one({
        "$or": [
            {"username": request.username},
            {"email": request.email}
        ]
    })
    
    if existing_user:
        if existing_user["username"] == request.username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
    
    # Generate verification token
    verification_data = create_verification_token(request.email)
    
    # Create user document with default localization
    user_doc = {
        "username": request.username,
        "email": request.email,
        "display_name": request.display_name,
        "password_hash": get_password_hash(request.password),
        "user_type": "standard",
        "localization": request.localization,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "is_active": True,
        "is_verified": False,
        "verification_token": verification_data["token"],
        "verification_token_expires": verification_data["expires"],
        "verified_at": None,
        "followers_count": 0,
        "following_count": 0,
        "videos_count": 0,
        "likes_count": 0,
        "features": {},
        "video_upload_limit": 90,
        "can_upload_music": False,
        "can_create_ads": False,
        "theme": None,
        
        # Standard user specific fields
        "bookmarked_videos": [],
        "liked_videos": [],
        "following": [],
        "followers": [],
        
        # Optional demographic fields
        "gender": request.gender,
        "date_of_birth": request.date_of_birth,
        "tags": request.tags if request.tags else [],
        "bio": None,
        "profile_picture": None,
        "cover_picture": None
    }
    
    # Insert user into database
    try:
        result = await db.users.insert_one(user_doc)
        user_id = str(result.inserted_id)
    except Exception as e:
        logger.error(f"Failed to create user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user account"
        )
    
    # Create access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_id}, 
        expires_delta=access_token_expires
    )
    
    # Send verification email (non-blocking)
    verification_email_sent = False
    try:
        email_sent = await email_service.send_verification_email(
            request.email,
            request.username,
            verification_data["token"]
        )
        verification_email_sent = email_sent
        
        if not email_sent:
            logger.warning(f"Failed to send verification email to {request.email}, but user was created successfully")
    except Exception as e:
        logger.error(f"Error sending verification email: {e}")
        # Don't fail registration if email fails
    
    return RegisterResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user_id,
        username=request.username,
        is_verified=False,  # New users are not verified initially
        is_new_user=True,
        verification_email_sent=verification_email_sent
    )

@router.post("/login/json", response_model=Token)
async def login_json(login_data: LoginRequest):
    """Login endpoint - accepts JSON"""
    db = get_database()
    
    # Find user by username or email
    user = await db.users.find_one({
        "$or": [
            {"username": login_data.username},
            {"email": login_data.username}  # Allow login with email too
        ]
    })
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Verify password
    if not verify_password(login_data.password, user.get("password_hash", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password"
        )
    
    # Create access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user["_id"])}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": str(user["_id"]),
        "username": user["username"],
        "is_verified": user.get("is_verified", False)
    }

# @router.post("/register")
# async def register(
#     username: str,
#     email: str,
#     password: str,
#     display_name: str
# ):
#     """Register a new user - DEPRECATED"""
#     return {
#         "message": "This endpoint is deprecated. Use POST /api/v1/users/ for registration",
#         "endpoint": "/api/v1/users/"
#     }

# @router.post("/verify-email")
# async def verify_email(request: VerifyEmailRequest):
#     """Verify user email with token (JSON body)"""
#     db = get_database()
    
#     # Find user with this token
#     user = await db.users.find_one({
#         "verification_token": request.token,
#         "verification_token_expires": {"$gt": datetime.utcnow()}
#     })
    
#     if not user:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Invalid or expired verification token"
#         )
    
#     # Check if already verified
#     if user.get("is_verified", False):
#         return {
#             "message": "Email already verified",
#             "username": user["username"],
#             "already_verified": True
#         }
    
#     # Update user verification status
#     await db.users.update_one(
#         {"_id": user["_id"]},
#         {
#             "$set": {
#                 "is_verified": True,
#                 "verified_at": datetime.utcnow(),
#                 "verification_token": None,
#                 "verification_token_expires": None,
#                 "updated_at": datetime.utcnow()
#             }
#         }
#     )
    
#     return {
#         "message": "Email verified successfully",
#         "username": user["username"],
#         "user_id": str(user["_id"])
#     }

@router.get("/verify-email")
async def verify_email_get(token: str = Query(..., description="Verification token")):
    """Verify user email with token from URL (for email link clicks)"""
    db = get_database()
    
    # Find user with this token
    user = await db.users.find_one({
        "verification_token": token,
        "verification_token_expires": {"$gt": datetime.utcnow()}
    })
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token"
        )
    
    # Check if already verified
    if user.get("is_verified", False):
        return {
            "message": "Email already verified",
            "username": user["username"],
            "already_verified": True
        }
    
    # Update user verification status
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "is_verified": True,
                "verified_at": datetime.utcnow(),
                "verification_token": None,
                "verification_token_expires": None,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    return {
        "message": "Email verified successfully",
        "username": user["username"],
        "user_id": str(user["_id"])
    }

@router.post("/resend-verification")
async def resend_verification(request: ResendVerificationRequest):
    """Resend verification email"""
    db = get_database()
    
    user = await db.users.find_one({"email": request.email})
    
    if not user:
        # Don't reveal if email exists or not for security
        return {"message": "If this email is registered, a verification email will be sent"}
    
    if user.get("is_verified"):
        return {"message": "Email already verified"}
    
    # Check if we recently sent a verification email (rate limiting)
    last_sent = user.get("verification_token_expires")
    if last_sent:
        time_since_last = last_sent - timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS) + timedelta(minutes=5)
        if time_since_last > datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait a few minutes before requesting another verification email"
            )
    
    # Generate new token
    verification_data = create_verification_token(request.email)
    
    # Update user with new token
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "verification_token": verification_data["token"],
                "verification_token_expires": verification_data["expires"],
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    # Send email
    await email_service.send_verification_email(
        request.email,
        user["username"],
        verification_data["token"]
    )
    
    return {"message": "Verification email sent"}

@router.get("/check-verification/{username}")
async def check_verification_status(username: str):
    """Check if a user's email is verified"""
    db = get_database()
    
    user = await db.users.find_one({"username": username})
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return {
        "username": username,
        "is_verified": user.get("is_verified", False),
        "verified_at": user.get("verified_at")
    }

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """
    Create and email a 6-digit OTP to reset password for a specific user.
    - Accepts username or email.
    - Stores a hashed OTP and an expiry in the user's record.
    - Always returns a generic message to avoid user enumeration.
    """
    db = get_database()

    # Find user by username or email (case-insensitive for email)
    user = await db.users.find_one({
        "$or": [
            {"username": request.username_or_email},
            {"email": request.username_or_email}
        ]
    })

    # Return error if user not found
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Optional: simple rate limiting to prevent abuse
    now = datetime.utcnow()
    cooldown_minutes = 1
    last_requested = user.get("password_reset_requested_at")
    if last_requested and isinstance(last_requested, datetime):
        earliest_next = last_requested + timedelta(minutes=cooldown_minutes)
        if earliest_next > now:
            # Too many requests
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Please wait a few minutes before requesting another OTP"
            )

    # Generate & send OTP via email service
    try:
        email_ok, otp = await email_service.send_password_reset_otp(
            to_email=user["email"],
            username=user["username"]
        )
    except Exception as e:
        logging.exception("Error while sending password reset OTP")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send OTP email"
        )

    if not email_ok:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send OTP email"
        )

    # Persist hashed OTP and expiry
    expiry_minutes = 30
    otp_expires = now + timedelta(minutes=expiry_minutes)

    # Reuse existing password hash function for storage; do NOT store raw OTP
    otp_hash = get_password_hash(otp)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_reset_otp_hash": otp_hash,
            "password_reset_otp_expires": otp_expires,
            "password_reset_requested_at": now,
            "updated_at": now
        }}
    )


    # Always a generic message (prevents user enumeration)
    return {
        "message": "If the account exists, an OTP has been sent to the registered email.",
        "expires_in_minutes": expiry_minutes,
    }


# --- Inserted endpoints for OTP verification and password reset ---

@router.post("/verify-reset-otp")
async def verify_reset_otp(request: VerifyResetOtpRequest):
    """
    Verify that the provided OTP is correct and not expired for the user.
    Always uses a generic error to avoid user enumeration.
    """
    db = get_database()

    user = await db.users.find_one({
        "$or": [
            {"username": request.username_or_email},
            {"email": request.username_or_email}
        ]
    })

    # Generic error to avoid enumeration
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    # Check presence and expiry
    otp_hash = user.get("password_reset_otp_hash")
    otp_expires = user.get("password_reset_otp_expires")
    if not otp_hash or not otp_expires or otp_expires < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    # Verify code
    if not verify_password(request.otp, otp_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    return {"valid": True, "expires_at": otp_expires}


@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """
    Reset the user's password using a valid OTP. On success, clears the OTP fields.
    """
    db = get_database()

    user = await db.users.find_one({
        "$or": [
            {"username": request.username_or_email},
            {"email": request.username_or_email}
        ]
    })

    # Generic error to avoid enumeration
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    otp_hash = user.get("password_reset_otp_hash")
    otp_expires = user.get("password_reset_otp_expires")

    if not otp_hash or not otp_expires or otp_expires < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    if not verify_password(request.otp, otp_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    # Update password and purge OTP fields
    new_hash = get_password_hash(request.new_password)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_hash": new_hash,
            "updated_at": datetime.utcnow(),
            "password_changed_at": datetime.utcnow()
        }, "$unset": {
            "password_reset_otp_hash": "",
            "password_reset_otp_expires": "",
            "password_reset_requested_at": ""
        }}
    )

    return {"message": "Password has been reset successfully"}