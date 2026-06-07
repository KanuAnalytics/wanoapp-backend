# services/auth/google_auth.py

from google.oauth2 import id_token
from google.auth.transport import requests
from fastapi import HTTPException, status
from app.core.config import settings
import random

async def verify_google_token(token: str):
    try:
        idinfo = id_token.verify_oauth2_token(
            token,
            requests.Request(),
            None,  # ❗ disable strict audience check here
        )

        if idinfo["iss"] not in ["accounts.google.com", "https://accounts.google.com"]:
            raise ValueError("Invalid issuer")

        if idinfo["aud"] not in [
            settings.GOOGLE_WEB_CLIENT_ID,
            settings.GOOGLE_IOS_CLIENT_ID,
            settings.GOOGLE_ANDROID_CLIENT_ID,
        ]:
            raise ValueError("Invalid audience")

        return idinfo

    except Exception as e:
        print("Google verify error:", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token"
        )

async def generate_unique_username(db, base: str):
    base = base.lower().replace(" ", "")
    
    username = base
    counter = 1

    while await db.users.find_one({"username": username}):
        username = f"{base}{counter}"
        counter += 1

    return username