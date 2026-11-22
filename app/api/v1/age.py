"""Birthday check (app/services/age_check.py). Behind the "age_check" switch."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StrictStr

from app.api.deps import get_current_active_user
from app.core.database import get_database
from app.services import age_check, creator_push

router = APIRouter()


class BirthdayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    birthday: StrictStr          # YYYY-MM-DD


async def _db():
    db = get_database()
    await creator_push.require(db, "age_check")
    return db


@router.get("/check")
async def check(me: str = Depends(get_current_active_user)):
    return await age_check.check(await _db(), me)


@router.post("/confirm")
async def confirm(me: str = Depends(get_current_active_user)):
    return await age_check.confirm(await _db(), me)


@router.post("/birthday")
async def birthday(body: BirthdayIn, me: str = Depends(get_current_active_user)):
    return await age_check.fix(await _db(), me, body.birthday[:20])


# ------------------------------------------------ birthday for accounts without one
# app/services/birthday_gate.py, behind the "birthday_needed" switch.
async def _gate_db():
    from app.services import birthday_gate
    db = get_database()
    await creator_push.require(db, birthday_gate.SWITCH)
    return db


@router.get("/needed")
async def needed(me: str = Depends(get_current_active_user)):
    from app.services import birthday_gate
    return await birthday_gate.check(await _gate_db(), me)


@router.post("/later")
async def later(me: str = Depends(get_current_active_user)):
    from app.services import birthday_gate
    return await birthday_gate.later(await _gate_db(), me)


@router.post("/add")
async def add(body: BirthdayIn, me: str = Depends(get_current_active_user)):
    from app.services import birthday_gate
    return await birthday_gate.add(await _gate_db(), me, body.birthday[:20])
