from typing import List
from fastapi import APIRouter, Depends, HTTPException
from app.core.database import get_database
from app.api.deps import get_current_active_user
import json
from bson.json_util import dumps

router = APIRouter()

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

        docs = await db.categories.aggregate(pipeline).to_list(length=None)
        return json.loads(dumps(docs))

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))