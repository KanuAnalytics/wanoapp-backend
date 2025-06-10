"""
Authentication endpoints

app/api/v1/auth.py

"""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordRequestForm
from jose import jwt
from app.core.config import settings
from app.core.database import get_database
from app.core.security import verify_password, get_password_hash, create_access_token, create_verification_token
from app.services.email_service import email_service
from pydantic import BaseModel, EmailStr

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

@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """Login endpoint - accepts form data"""
    db = get_database()
    
    # Find user by username or email
    user = await db.users.find_one({
        "$or": [
            {"username": form_data.username},
            {"email": form_data.username}  # Allow login with email too
        ]
    })
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify password
    if not verify_password(form_data.password, user.get("password_hash", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
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

@router.post("/register")
async def register(
    username: str,
    email: str,
    password: str,
    display_name: str
):
    """Register a new user - DEPRECATED"""
    return {
        "message": "This endpoint is deprecated. Use POST /api/v1/users/ for registration",
        "endpoint": "/api/v1/users/"
    }

@router.post("/verify-email")
async def verify_email(request: VerifyEmailRequest):
    """Verify user email with token (JSON body)"""
    db = get_database()
    
    # Find user with this token
    user = await db.users.find_one({
        "verification_token": request.token,
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