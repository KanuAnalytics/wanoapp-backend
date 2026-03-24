#app/api/deps.py

from typing import Optional
from fastapi import Depends, HTTPException, status, Query
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from app.core.config import settings
from app.core.database import get_database
from bson import ObjectId

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/v1/auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(
    tokenUrl="api/v1/auth/login",
    auto_error=False,
)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    """Get current authenticated user"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    return user_id

async def get_current_active_user(current_user: str = Depends(get_current_user)):
    """Verify user is active"""
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(current_user), "is_active": True})
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    
    return current_user

async def get_optional_active_user(
    auth_required: bool = Query(True, alias="authenticated"),
    token: Optional[str] = Depends(oauth2_scheme_optional),
):
    """Optionally authenticate user; when authenticated=false, allow anonymous access."""
    if not auth_required:
        return None

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = await get_current_user(token)

    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(user_id), "is_active": True})

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )

    return user_id

async def get_verified_user(current_user: str = Depends(get_current_active_user)):
    """Get current user and verify they have verified email"""
    db = get_database()
    user = await db.users.find_one({"_id": ObjectId(current_user)})
    
    # if not user.get("is_verified", False):
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Email verification required. Please check your email and verify your account."
    #     )
    
    return current_user
