from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.database import get_database

router = APIRouter()


class VersionResponse(BaseModel):
    appVersionNumber: str


@router.get("/version", response_model=VersionResponse)
async def get_app_version():
    """Return the current app version from the config collection."""
    db = get_database()

    doc = await db.config.find_one(
        {},
        projection={"appVersionNumber": 1},
        sort=[("_id", -1)],
    )

    if not doc or "appVersionNumber" not in doc:
        raise HTTPException(status_code=404, detail="App version not found")

    raw_version = doc.get("appVersionNumber")
    normalized_version = raw_version.strip('"') if isinstance(raw_version, str) else str(raw_version)

    return VersionResponse(appVersionNumber=normalized_version)
