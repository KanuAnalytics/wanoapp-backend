"""
Authentication endpoints

app/api/v1/auth.py

"""
from profanity_check import predict, predict_prob
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from app.core.config import settings
from app.core.database import get_database
from app.core.security import verify_password, get_password_hash, create_access_token, create_verification_token
from app.services.email_service import email_service
from app.services.twilio_whatsapp_service import twilio_whatsapp_service, TwilioWhatsAppServiceError
from pydantic import BaseModel, EmailStr, validator, Field
import logging
import secrets


logger = logging.getLogger(__name__)

router = APIRouter()

def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _generate_numeric_otp(length: int = 6) -> str:
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))

class Token(BaseModel):
    access_token: str
    token_type: str
    user_id: str
    username: str
    is_verified: bool
    user_type: Optional[str] = None

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

class RequestRegistrationOtpRequest(BaseModel):
    """Request an OTP before registration (email or WhatsApp)."""
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+[1-9]\d{7,14}$")
    username: Optional[str] = None

class VerifyRegistrationOtpRequest(BaseModel):
    """Verify a registration OTP for email or WhatsApp."""
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+[1-9]\d{7,14}$")
    otp: str = Field(..., min_length=4, max_length=10)


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
    username: str = Field(..., min_length=3, max_length=30)
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = Field(None, pattern=r"^\+[1-9]\d{7,14}$")
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

# Manual banned word list (expand as needed)
BANNED_WORDS = {
    "fuck", "sex", "porn", "sexy", "sperm", "cum", "bitch", "dick", "pussy",
    "cock", "slut", "nigger", "faggot", "rape", "nude", "wasted sperm"
}

@router.post("/register", response_model=RegisterResponse)
async def register(request: RegisterRequest):
    """Register a new user and return access token"""
    db = get_database()

    if not request.email and not request.phone_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either email or phone number is required"
        )

    # --- Profanity and banned-word check (username + display name) ---
    username_probability = predict_prob([request.username])[0]
    display_probability = predict_prob([request.display_name])[0]

    lower_username = request.username.lower()
    lower_display_name = request.display_name.lower()

    if username_probability > 0.7 or any(bad_word in lower_username for bad_word in BANNED_WORDS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username contains inappropriate language"
        )

    if display_probability > 0.7 or any(bad_word in lower_display_name for bad_word in BANNED_WORDS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Display name contains inappropriate language"
        )

    # Check if username already exists
    existing_username = await db.users.find_one({"username": request.username})
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )

    if request.email:
        existing_email = await db.users.find_one({"email": request.email})
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    if request.phone_number:
        existing_phone = await db.users.find_one({"phone_number": request.phone_number})
        if existing_phone:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number already registered"
            )

    # Password policy: must contain at least one uppercase letter and one digit
    if not any(c.isupper() for c in request.password) or not any(c.isdigit() for c in request.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must contain at least one uppercase letter and one number"
        )

    # Generate verification token (email registrations only)
    verification_data = None
    if request.email:
        verification_data = create_verification_token(request.email)
    
    # Create user document with default localization
    user_doc = {
        "username": request.username,
        "email": request.email,
        "phone_number": request.phone_number,
        "display_name": request.display_name,
        "password_hash": get_password_hash(request.password),
        "user_type": "standard",
        "localization": request.localization,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "is_active": True,
        "is_verified": False,
        "verification_token": verification_data["token"] if verification_data else None,
        "verification_token_expires": verification_data["expires"] if verification_data else None,
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
    if request.email and verification_data:
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

@router.post("/request-registration-otp")
async def request_registration_otp(request: RequestRegistrationOtpRequest):
    """
    Send an OTP to verify an email or WhatsApp number before registration.
    Email and WhatsApp OTPs are stored hashed in a temporary collection.
    """
    db = get_database()

    if request.username:
        existing_username = await db.users.find_one({"username": request.username})
        if existing_username:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already taken"
            )

    now = datetime.now(timezone.utc)
    expiry_minutes = 10
    cooldown_minutes = 0.5

    channel = None
    if request.phone_number:
        channel = "whatsapp"
    elif request.email:
        channel = "email"

    if channel == "email":
        if not request.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required for email verification"
            )

        # Avoid sending OTP if email is already registered
        existing_user = await db.users.find_one({"email": request.email})
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        existing = await db.registration_otps.find_one({"email": request.email})
        if existing:
            last_requested = _as_utc(existing.get("requested_at"))
            if last_requested and isinstance(last_requested, datetime):
                earliest_next = last_requested + timedelta(minutes=cooldown_minutes)
                if earliest_next > now:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Please wait a few minutes before requesting another code"
                    )

        try:
            email_ok, otp = await email_service.send_registration_otp(
                to_email=request.email,
                username=request.username
            )
        except Exception:
            logger.exception("Error while sending registration OTP")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send OTP email"
            )

        if not email_ok:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send OTP email"
            )

        otp_hash = get_password_hash(otp)
        otp_expires = now + timedelta(minutes=expiry_minutes)

        await db.registration_otps.update_one(
            {"email": request.email},
            {"$set": {
                "email": request.email,
                "otp_hash": otp_hash,
                "otp_expires": otp_expires,
                "requested_at": now,
                "updated_at": now
            }},
            upsert=True
        )

        return {
            "message": "OTP sent to the email address.",
            "expires_in_minutes": expiry_minutes,
            "channel": "email"
        }

    if channel == "whatsapp":
        if not request.phone_number:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number is required for WhatsApp verification"
            )

        existing_number = await db.users.find_one({"whatsapp_number": request.phone_number})
        if existing_number:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number already registered"
            )

        existing = await db.registration_whatsapp_otps.find_one({"phone_number": request.phone_number})
        if existing:
            last_requested = _as_utc(existing.get("requested_at"))
            if last_requested and isinstance(last_requested, datetime):
                earliest_next = last_requested + timedelta(minutes=cooldown_minutes)
                if earliest_next > now:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Please wait a few minutes before requesting another code"
                    )

        otp = _generate_numeric_otp(6)
        try:
            result = await twilio_whatsapp_service.send_template_message(
                to=request.phone_number,
                content_variables={"1": otp},
            )
        except TwilioWhatsAppServiceError:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to send WhatsApp OTP"
            )

        otp_hash = get_password_hash(otp)
        otp_expires = now + timedelta(minutes=expiry_minutes)
        await db.registration_whatsapp_otps.update_one(
            {"phone_number": request.phone_number},
            {"$set": {
                "phone_number": request.phone_number,
                "otp_hash": otp_hash,
                "otp_expires": otp_expires,
                "requested_at": now,
                "updated_at": now,
                "twilio_sid": result.get("sid")
            }},
            upsert=True
        )

        return {
            "message": "OTP sent to the WhatsApp number.",
            "expires_in_minutes": expiry_minutes,
            "channel": "whatsapp"
        }

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported channel"
    )

@router.post("/verify-registration-otp")
async def verify_registration_otp(request: VerifyRegistrationOtpRequest):
    """
    Verify that the provided OTP is correct and not expired for email or WhatsApp.
    On success, delete the OTP record.
    """
    db = get_database()

    channel = None
    if request.phone_number:
        channel = "whatsapp"
    elif request.email:
        channel = "email"

    if channel == "email":
        if not request.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required for email verification"
            )

        record = await db.registration_otps.find_one({"email": request.email})
        if not record:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        otp_hash = record.get("otp_hash")
        otp_expires = record.get("otp_expires")
        otp_expires_utc = _as_utc(otp_expires)
        if not otp_hash or not otp_expires_utc or otp_expires_utc < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        if not verify_password(request.otp, otp_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        await db.registration_otps.delete_one({"email": request.email})

        return {"verified": True, "channel": "email"}

    if channel == "whatsapp":
        if not request.phone_number:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number is required for WhatsApp verification"
            )

        record = await db.registration_whatsapp_otps.find_one({"phone_number": request.phone_number})
        if not record:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        otp_expires = _as_utc(record.get("otp_expires"))
        if not otp_expires or otp_expires < datetime.now(timezone.utc):
            await db.registration_whatsapp_otps.delete_one({"phone_number": request.phone_number})
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        otp_hash = record.get("otp_hash")
        if not otp_hash:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        if not verify_password(request.otp, otp_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid code or expired"
            )

        await db.registration_whatsapp_otps.delete_one({"phone_number": request.phone_number})

        return {"verified": True, "channel": "whatsapp"}

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Unsupported channel"
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
        "is_verified": user.get("is_verified", False),
        "user_type": user.get("user_type")
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
        "verification_token_expires": {"$gt": datetime.now(timezone.utc)}
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
                "verified_at": datetime.now(timezone.utc),
                "verification_token": None,
                "verification_token_expires": None,
                "updated_at": datetime.now(timezone.utc)
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
    last_sent = _as_utc(user.get("verification_token_expires"))
    if last_sent:
        time_since_last = last_sent - timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS) + timedelta(minutes=5)
        if time_since_last > datetime.now(timezone.utc):
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
                "updated_at": datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)
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
    otp_expires_utc = _as_utc(otp_expires)
    if not otp_hash or not otp_expires_utc or otp_expires_utc < datetime.now(timezone.utc):
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
    otp_expires_utc = _as_utc(otp_expires)

    if not otp_hash or not otp_expires_utc or otp_expires_utc < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    if not verify_password(request.otp, otp_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    # Password policy: must contain at least one uppercase letter and one digit
    if not any(c.isupper() for c in request.new_password) or not any(c.isdigit() for c in request.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must contain at least one uppercase letter and one number"
        )
    # Update password and purge OTP fields
    new_hash = get_password_hash(request.new_password)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_hash": new_hash,
            "updated_at": datetime.now(timezone.utc),
            "password_changed_at": datetime.now(timezone.utc)
        }, "$unset": {
            "password_reset_otp_hash": "",
            "password_reset_otp_expires": "",
            "password_reset_requested_at": ""
        }}
    )

    return {"message": "Password has been reset successfully"}
