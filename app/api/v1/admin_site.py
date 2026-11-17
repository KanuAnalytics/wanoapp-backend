"""Admin pages for what arrives from wanoafrica.com: mailing list, enquiries
(advertising, press, partners) and account-deletion requests."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_admin
from app.core.database import get_database
from app.services import admin_log
from app.services import public_site as ps

router = APIRouter()
KINDS = {"enquiries": "site_enquiries", "deletion": "deletion_requests", "newsletter": "newsletter"}


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(..., max_length=20)


@router.get("/newsletter.csv")
async def newsletter_csv(admin: str = Depends(require_admin)):
    db = get_database()
    await admin_log.record(db, admin, "newsletter_export", {})
    return PlainTextResponse(await ps.newsletter_csv(db), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="wano-mailing-list.csv"'})


@router.get("/{kind}")
async def listing(kind: str, admin: str = Depends(require_admin)):
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail="Not found")
    return await ps.admin_list(get_database(), KINDS[kind])


@router.post("/{kind}/{item_id}/status")
async def set_status(kind: str, item_id: str, body: StatusIn, admin: str = Depends(require_admin)):
    if kind not in ("enquiries", "deletion"):
        raise HTTPException(status_code=404, detail="Not found")
    db = get_database()
    try:
        ok = await ps.admin_set_status(db, KINDS[kind], item_id[:40], body.status, admin)
    except ps.Invalid as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    await admin_log.record(db, admin, f"site_{kind}_status", {"id": item_id[:40], "status": body.status})
    return {"status": body.status}
