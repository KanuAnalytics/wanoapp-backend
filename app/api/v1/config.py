from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import get_database

router = APIRouter()


class VersionResponse(BaseModel):
    appVersionNumber: str
    appVersionIos: str | None = None
    appVersionAndroid: str | None = None
    showReviewAndroid: bool = False
    showReviewIos: bool = False
    showUpdateAndroid: bool = False
    showUpdateIos: bool = False


@router.get("/version", response_model=VersionResponse)
async def get_app_version():
    """Return the current app version from the config collection."""
    db = get_database()

    doc = await db.config.find_one(
        {},
        projection={
            "appVersionNumber": 1,
            "appVersionIos": 1,
            "appVersionAndroid": 1,
            "showReviewAndroid": 1,
            "showReviewIos": 1,
            "showUpdateAndroid": 1,
            "showUpdateIos": 1,
        },
        sort=[("_id", -1)],
    )

    if not doc or "appVersionNumber" not in doc:
        raise HTTPException(status_code=404, detail="App version not found")

    raw_version = doc.get("appVersionNumber")
    normalized_version = raw_version.strip('"') if isinstance(raw_version, str) else str(raw_version)

    return VersionResponse(
        appVersionNumber=normalized_version,
        appVersionIos=doc.get("appVersionIos"),
        appVersionAndroid=doc.get("appVersionAndroid"),
        showReviewAndroid=doc.get("showReviewAndroid", False),
        showReviewIos=doc.get("showReviewIos", False),
        showUpdateAndroid=doc.get("showUpdateAndroid", False),
        showUpdateIos=doc.get("showUpdateIos", False),
    )
