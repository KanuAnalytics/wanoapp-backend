"""
app/core/security.py

"""



from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings
import secrets

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Hash a password"""
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create a JWT access token with an `iat` claim and no expiry.

    Tokens don't expire (as before), so ACCESS_TOKEN_EXPIRE_MINUTES and
    `expires_delta` are ignored: a short value in the server's env would
    otherwise sign everyone out. `iat` still lets deps reject tokens issued
    before a password change (users.password_changed_at)."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    to_encode.setdefault("iat", int(now.timestamp()))
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str):
    """Decode JWT access token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None
    
def generate_verification_token() -> str:
    """Generate a secure random verification token"""
    return secrets.token_urlsafe(32)  # FIXED: token_urlsafe instead of urlsafe_token

def create_verification_token(email: str) -> dict:
    """Create email verification token with expiration"""
    token = generate_verification_token()
    expires = datetime.utcnow() + timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
    return {
        "token": token,
        "expires": expires
    }