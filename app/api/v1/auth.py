"""
Authentication endpoints

app/api/v1/auth.py

"""
from profanity_check import predict, predict_prob
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal
from fastapi import APIRouter, Depends, Header, HTTPException, status, Query, Request
from app.services import recommendations
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from app.core.config import settings
from app.core.disposable_email import ensure_not_disposable_email
from app.core.database import get_database
from app.core.security import verify_password, get_password_hash, create_access_token, create_verification_token
from app.services.email_service import email_service
from app.services.twilio_whatsapp_service import twilio_whatsapp_service, TwilioWhatsAppServiceError
from pydantic import BaseModel, EmailStr, validator, Field
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque


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


# --- OTP brute-force protection (C2) ---
MAX_OTP_ATTEMPTS = 5
# Each new code clears the guess counter, so codes issued per account (or per
# signup address) are capped too. Without this, one request a minute gives an
# attacker 5 fresh guesses a minute, forever.
MAX_OTPS_PER_DAY = 10


async def _claim_otp_send(collection, filter_: dict, prefix: str) -> bool:
    """Atomically count one more code sent for this record in a rolling 24 h
    window. False once MAX_OTPS_PER_DAY codes were already sent in it."""
    now = datetime.now(timezone.utc)
    start_f, count_f = f"{prefix}_window_start", f"{prefix}_window_count"
    # Window missing or older than a day: start a new one at 1.
    started = await collection.update_one(
        {**filter_, "$or": [{start_f: {"$exists": False}}, {start_f: None},
                            {start_f: {"$lt": now - timedelta(hours=24)}}]},
        {"$set": {start_f: now, count_f: 1}},
    )
    if started.modified_count == 1:
        return True
    bumped = await collection.update_one(
        {**filter_, count_f: {"$lt": MAX_OTPS_PER_DAY}},
        {"$inc": {count_f: 1}},
    )
    return bumped.modified_count == 1


def _too_many_codes() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many codes requested today. Please try again tomorrow.",
    )
# How long a verified registration OTP stays usable by /register (H5).
REGISTRATION_VERIFIED_TTL = timedelta(minutes=30)
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{3,30}$")
GENERIC_OTP_ERROR = "Invalid code or expired"


def _normalize_email(email: str | None) -> str | None:
    return email.strip().lower() if email else email


def _normalize_identifier(value: str) -> str:
    """Login/forgot-password identifier: emails are matched case-insensitively."""
    value = (value or "").strip()
    return value.lower() if "@" in value else value


def _identifier_query(identifier: str) -> dict:
    ident = _normalize_identifier(identifier)
    return {"$or": [
        {"username": ident},
        {"email": ident},
        {"phone_number": ident},
    ]}


def _ci_exact(value: str) -> dict:
    """Matches `value` whatever its capitals."""
    return {"$regex": f"^{re.escape(value)}$", "$options": "i"}


def _email_exists_query(email: str) -> dict:
    """Finds an account by email whatever its capitals: accounts created
    before emails were stored lowercased still carry their original ones."""
    return {"email": _ci_exact(email)}


async def _find_user_by_identifier(db, identifier: str):
    """The account behind a login/reset identifier (username, email, phone).

    The exact match runs first, so an ordinary login stays a single indexed
    lookup. Only when that finds nothing do we retry ignoring capitals, for
    the accounts registered before emails were lowercased on the way in:
    their stored address never equals the lowercased identifier, which
    would otherwise shut them out of both login and password reset.
    """
    user = await db.users.find_one(_identifier_query(identifier))
    if user:
        return user
    ident = _normalize_identifier(identifier)
    if "@" not in ident:
        # Only identifiers containing "@" are lowercased above; anything
        # else was already compared exactly.
        return None
    return await db.users.find_one({"$or": [
        {"username": _ci_exact(ident)},
        {"email": _ci_exact(ident)},
    ]})


async def _register_otp_failure(collection, filter_: dict, counter_field: str, wipe_fields: list[str]):
    """Atomically count a wrong guess; after MAX_OTP_ATTEMPTS wipe the OTP so
    even the correct code stops working. Raises the HTTP error either way."""
    updated = await collection.find_one_and_update(
        filter_,
        {"$inc": {counter_field: 1}},
        return_document=ReturnDocument.AFTER,
    )
    attempts = (updated or {}).get(counter_field, 0)
    if attempts >= MAX_OTP_ATTEMPTS:
        await collection.update_one(filter_, {"$unset": {f: "" for f in wipe_fields}})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Please request a new code.",
        )
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_OTP_ERROR)


# --- Failed-login throttle (M6): in-memory, per identifier+IP ---
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures: dict[str, deque] = defaultdict(deque)
_login_lock = threading.Lock()


def _login_key(identifier: str, ip: str | None) -> str:
    return f"{_normalize_identifier(identifier)}|{ip or '-'}"


def _login_throttled(key: str) -> bool:
    now = time.monotonic()
    with _login_lock:
        q = _login_failures.get(key)
        if not q:
            return False
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        if not q:
            _login_failures.pop(key, None)
            return False
        return len(q) >= LOGIN_MAX_FAILURES


def _record_login_failure(key: str) -> None:
    now = time.monotonic()
    with _login_lock:
        q = _login_failures[key]
        q.append(now)
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        # Bound memory if someone sprays many identifiers.
        if len(_login_failures) > 100_000:
            _login_failures.clear()


def _clear_login_failures(key: str) -> None:
    with _login_lock:
        _login_failures.pop(key, None)

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

# Manual banned word list (expand as needed)
BANNED_WORDS = {
    "fuck", "sex", "porn", "sexy", "sperm", "cum", "bitch", "dick", "pussy",
    "cock", "slut", "nigger", "faggot", "rape", "nude", "wasted sperm"
}

@router.post("/register", response_model=RegisterResponse)
async def register(request: RegisterRequest, x_device_id: Optional[str] = Header(None, alias="X-Device-Id")):
    """Register a new user and return access token"""
    db = get_database()

    if not request.email and not request.phone_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either email or phone number is required"
        )

    email = _normalize_email(request.email)
    phone_number = request.phone_number

    # Checked here as well as at OTP request time (runs before any DB access).
    if email:
        ensure_not_disposable_email(email)

    # Usernames must not look like an email or phone number (M4), since
    # login accepts any of the three as the identifier.
    if not USERNAME_RE.match(request.username or ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be 3-30 characters: letters, numbers, underscores or dots"
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

    if email:
        existing_email = await db.users.find_one(_email_exists_query(email))
        if existing_email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    if phone_number:
        existing_phone = await db.users.find_one({"phone_number": phone_number})
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

    # Require a contact proven via /verify-registration-otp in the last
    # 30 minutes (H5). Email takes precedence when both are given.
    now = datetime.now(timezone.utc)
    verified_cutoff = now - REGISTRATION_VERIFIED_TTL
    otp_collection = None
    otp_filter = None
    if email:
        otp_collection, otp_filter = db.registration_otps, {"email": email}
    else:
        otp_collection, otp_filter = db.registration_whatsapp_otps, {"phone_number": phone_number}
    otp_record = await otp_collection.find_one(otp_filter)
    verified_at = _as_utc((otp_record or {}).get("verified_at"))
    if not verified_at or verified_at < verified_cutoff:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please verify your email or phone first"
        )

    # Create user document with default localization
    user_doc = {
        "username": request.username,
        "display_name": request.display_name,
        "password_hash": get_password_hash(request.password),
        "user_type": "standard",
        "localization": request.localization,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "is_active": True,
        # The contact was proven by the registration OTP above.
        "is_verified": True,
        "verification_token": None,
        "verification_token_expires": None,
        "verified_at": now,
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
    # Omit absent contact keys rather than storing null, so the partial
    # unique indexes don't treat many phone-only users as duplicates (H6).
    if email:
        user_doc["email"] = email
    if phone_number:
        user_doc["phone_number"] = phone_number

    # Insert user into database
    try:
        result = await db.users.insert_one(user_doc)
        user_id = str(result.inserted_id)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username, email or phone number already registered"
        )
    except Exception as e:
        logger.error(f"Failed to create user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create user account"
        )

    # The verified OTP is single-use.
    await otp_collection.delete_one(otp_filter)

    # Recommendations: what this device watched before signing up, plus the
    # interest tags picked at signup, so the first feed isn't blank.
    await recommendations.merge_device_into_user(x_device_id, user_id)
    await recommendations.set_user_profile({**user_doc, "_id": user_id})

    # Create access token
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_id}, 
        expires_delta=access_token_expires
    )
    
    return RegisterResponse(
        access_token=access_token,
        token_type="bearer",
        user_id=user_id,
        username=request.username,
        is_verified=True,  # Contact verified via registration OTP
        is_new_user=True
    )

@router.post("/request-registration-otp")
async def request_registration_otp(request: RequestRegistrationOtpRequest):
    """
    Send an OTP to verify an email or WhatsApp number before registration.
    Email and WhatsApp OTPs are stored hashed in a temporary collection.
    """
    email = _normalize_email(request.email)
    if email:
        # Refuse throwaway inboxes before any DB access or OTP email.
        ensure_not_disposable_email(email)

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
    elif email:
        channel = "email"

    if channel == "email":
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email is required for email verification"
            )

        # Avoid sending OTP if email is already registered
        existing_user = await db.users.find_one(_email_exists_query(email))
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

        existing = await db.registration_otps.find_one({"email": email})
        if existing:
            last_requested = _as_utc(existing.get("requested_at"))
            if last_requested and isinstance(last_requested, datetime):
                earliest_next = last_requested + timedelta(minutes=cooldown_minutes)
                if earliest_next > now:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Please wait a few minutes before requesting another code"
                    )

        await db.registration_otps.update_one(
            {"email": email}, {"$setOnInsert": {"email": email}}, upsert=True)
        if not await _claim_otp_send(db.registration_otps, {"email": email}, "send"):
            raise _too_many_codes()

        try:
            email_ok, otp = await email_service.send_registration_otp(
                to_email=email,
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
            {"email": email},
            {"$set": {
                "email": email,
                "otp_hash": otp_hash,
                "otp_expires": otp_expires,
                "requested_at": now,
                "updated_at": now,
                "failed_attempts": 0,
            }, "$unset": {"verified_at": ""}},
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

        existing_number = await db.users.find_one({"phone_number": request.phone_number})
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

        await db.registration_whatsapp_otps.update_one(
            {"phone_number": request.phone_number},
            {"$setOnInsert": {"phone_number": request.phone_number}}, upsert=True)
        if not await _claim_otp_send(db.registration_whatsapp_otps,
                                     {"phone_number": request.phone_number}, "send"):
            raise _too_many_codes()

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
                "twilio_sid": (result or {}).get("sid"),
                "failed_attempts": 0,
            }, "$unset": {"verified_at": ""}},
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
    On success, mark the record verified (verified_at); /register then
    consumes it within 30 minutes. Wrong guesses are counted and the OTP is
    wiped after MAX_OTP_ATTEMPTS failures.
    """
    db = get_database()

    email = _normalize_email(request.email)
    if request.phone_number:
        channel = "whatsapp"
        collection = db.registration_whatsapp_otps
        filter_ = {"phone_number": request.phone_number}
    elif email:
        channel = "email"
        collection = db.registration_otps
        filter_ = {"email": email}
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported channel"
        )

    record = await collection.find_one(filter_)
    if not record:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_OTP_ERROR)

    if record.get("failed_attempts", 0) >= MAX_OTP_ATTEMPTS:
        await collection.update_one(filter_, {"$unset": {"otp_hash": "", "otp_expires": ""}})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Please request a new code.",
        )

    otp_hash = record.get("otp_hash")
    otp_expires_utc = _as_utc(record.get("otp_expires"))
    if not otp_hash or not otp_expires_utc or otp_expires_utc < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_OTP_ERROR)

    if not verify_password(request.otp, otp_hash):
        await _register_otp_failure(collection, filter_, "failed_attempts", ["otp_hash", "otp_expires"])

    # Keep the record so /register can confirm the contact was proven, but
    # drop the code so it can't be replayed here.
    now = datetime.now(timezone.utc)
    await collection.update_one(
        filter_,
        {"$set": {"verified_at": now, "updated_at": now, "failed_attempts": 0},
         "$unset": {"otp_hash": "", "otp_expires": ""}},
    )

    return {"verified": True, "channel": channel}

@router.post("/login/json", response_model=Token)
async def login_json(login_data: LoginRequest, http_request: Request):
    """Login endpoint - accepts JSON"""
    db = get_database()

    client_ip = None
    if http_request is not None and http_request.client:
        client_ip = http_request.client.host
    throttle_key = _login_key(login_data.username, client_ip)
    if _login_throttled(throttle_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again later."
        )

    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password"
    )

    # Find user by username, email, or phone number
    user = await _find_user_by_identifier(db, login_data.username)

    if not user:
        _record_login_failure(throttle_key)
        raise invalid

    # Verify password
    if not verify_password(login_data.password, user.get("password_hash", "")):
        _record_login_failure(throttle_key)
        raise invalid

    # Deactivated/banned accounts get the same generic error.
    if user.get("is_active") is False:
        raise invalid

    _clear_login_failures(throttle_key)
    await recommendations.merge_device_into_user(http_request.headers.get("X-Device-Id"), str(user["_id"]))

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
    
    generic = {"message": "If this email is registered, a verification email will be sent"}
    user = await db.users.find_one(_email_exists_query(_normalize_email(request.email)))

    # Same response for unknown and already-verified emails (M3).
    if not user or user.get("is_verified"):
        return generic
    
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
    verification_data = create_verification_token(user["email"])
    
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
    
    return generic

@router.get("/check-verification/{username}")
async def check_verification_status(username: str):
    """Check if a user's email is verified"""
    db = get_database()
    
    user = await db.users.find_one({"username": username})

    # Unknown usernames look like unverified ones so this can't be used to
    # probe which accounts exist (M3).
    if not user:
        return {"username": username, "is_verified": False, "verified_at": None}

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

    expiry_minutes = 30
    generic = {
        "message": "If the account exists, an OTP has been sent to the registered contact.",
        "expires_in_minutes": expiry_minutes,
    }

    identifier = _normalize_identifier(request.username_or_email)
    # Find user by username, email, or phone number
    user = await _find_user_by_identifier(db, identifier)

    # Same response whether or not the account exists (M3).
    if not user:
        return generic

    # Optional: simple rate limiting to prevent abuse
    now = datetime.now(timezone.utc)
    cooldown_minutes = 1
    last_requested = _as_utc(user.get("password_reset_requested_at"))
    if last_requested and isinstance(last_requested, datetime):
        earliest_next = last_requested + timedelta(minutes=cooldown_minutes)
        if earliest_next > now:
            # Too many requests
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Please wait a few minutes before requesting another OTP"
            )

    if not await _claim_otp_send(db.users, {"_id": user["_id"]}, "password_reset"):
        raise _too_many_codes()

    use_whatsapp = False
    if not user.get("email"):
        use_whatsapp = True
    elif identifier == user.get("phone_number"):
        use_whatsapp = True

    # Generate & send OTP via WhatsApp or email
    if use_whatsapp:
        otp = _generate_numeric_otp(6)
        try:
            await twilio_whatsapp_service.send_template_message(
                to=user["phone_number"],
                content_variables={"1": otp},
            )
        except TwilioWhatsAppServiceError:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send OTP WhatsApp message"
            )
    else:
        try:
            email_ok, otp = await email_service.send_password_reset_otp(
                to_email=user["email"],
                username=user["username"]
            )
        except Exception:
            logger.exception("Error while sending password reset OTP")
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
    otp_expires = now + timedelta(minutes=expiry_minutes)

    # Reuse existing password hash function for storage; do NOT store raw OTP
    otp_hash = get_password_hash(otp)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "password_reset_otp_hash": otp_hash,
            "password_reset_otp_expires": otp_expires,
            "password_reset_requested_at": now,
            "password_reset_failed_attempts": 0,
            "updated_at": now
        }}
    )

    # Always a generic message (prevents user enumeration)
    return generic


# --- Inserted endpoints for OTP verification and password reset ---

RESET_OTP_FIELDS = ["password_reset_otp_hash", "password_reset_otp_expires"]


async def _check_reset_otp(db, user: dict, otp: str):
    """Validate a password-reset OTP with a failed-attempt cap (C2).
    Returns the OTP expiry on success; raises 400/429 otherwise."""
    filter_ = {"_id": user["_id"]}
    if user.get("password_reset_failed_attempts", 0) >= MAX_OTP_ATTEMPTS:
        await db.users.update_one(filter_, {"$unset": {f: "" for f in RESET_OTP_FIELDS}})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many incorrect attempts. Please request a new code.",
        )

    otp_hash = user.get("password_reset_otp_hash")
    otp_expires = user.get("password_reset_otp_expires")
    otp_expires_utc = _as_utc(otp_expires)
    if not otp_hash or not otp_expires_utc or otp_expires_utc < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_OTP_ERROR)

    if not verify_password(otp, otp_hash):
        await _register_otp_failure(db.users, filter_, "password_reset_failed_attempts", RESET_OTP_FIELDS)

    return otp_expires


@router.post("/verify-reset-otp")
async def verify_reset_otp(request: VerifyResetOtpRequest):
    """
    Verify that the provided OTP is correct and not expired for the user.
    Always uses a generic error to avoid user enumeration.
    """
    db = get_database()

    user = await _find_user_by_identifier(db, request.username_or_email)

    # Generic error to avoid enumeration
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    otp_expires = await _check_reset_otp(db, user, request.otp)

    return {"valid": True, "expires_at": otp_expires}


@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """
    Reset the user's password using a valid OTP. On success, clears the OTP fields.
    """
    db = get_database()

    user = await _find_user_by_identifier(db, request.username_or_email)

    # Generic error to avoid enumeration
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid code or expired")

    await _check_reset_otp(db, user, request.otp)

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
            "password_reset_requested_at": "",
            "password_reset_failed_attempts": ""
        }}
    )

    return {"message": "Password has been reset successfully"}
