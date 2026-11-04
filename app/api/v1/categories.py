from typing import List
from fastapi import APIRouter, Depends, HTTPException
from app.core.database import get_database
from app.api.deps import get_current_active_user
import json
import logging
from bson.json_util import dumps

logger = logging.getLogger(__name__)

router = APIRouter()

# The app shows the whole list; this only stops a runaway collection from
# being loaded into memory in one request.
MAX_CATEGORIES = 1000

@router.get("/", response_model=List[dict])
async def get_categories(current_user: str = Depends(get_current_active_user)):
    """Get all categories and subcategories with stringified IDs"""
    db = get_database()

    try:
        pipeline = [
            {
                "$project": {
                    "_id": {"$toString": "$_id"},
                    "name": 1,
                    "subcategories": {
                        "$map": {
                            "input": "$subcategories",
                            "as": "sub",
                            "in": {
                                "_id": {"$toString": "$$sub._id"},
                                "name": "$$sub.name"
                            }
                        }
                    }
                }
            }
        ]

        docs = await db.categories.aggregate(pipeline).to_list(length=MAX_CATEGORIES)
        return json.loads(dumps(docs))

    except Exception:
        # Don't echo DB/driver errors to clients.
        logger.exception("Reading categories failed")
        raise HTTPException(status_code=500, detail="Failed to load categories")