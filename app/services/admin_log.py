"""What admins did, and when: one row per action that succeeded."""

from datetime import datetime
from typing import Optional

from bson import ObjectId


async def record(db, admin: str, action: str, detail: Optional[dict] = None) -> None:
    try:
        await db.admin_log.insert_one({"at": datetime.utcnow(), "admin": ObjectId(admin), "action": action,
                                       "detail": detail or {}})
    except Exception:  # the log must never undo an action that already happened
        pass


async def recent(db, limit: int = 100) -> list:
    rows = await db.admin_log.find({}).sort([("at", -1), ("_id", -1)]).limit(limit).to_list(limit)
    names = {u["_id"]: u.get("username") async for u in
             db.users.find({"_id": {"$in": list({r["admin"] for r in rows})}}, {"username": 1})}
    return [{"at": r["at"], "action": r["action"], "detail": r.get("detail") or {},
             "admin": {"id": str(r["admin"]), "username": names.get(r["admin"])}} for r in rows]
