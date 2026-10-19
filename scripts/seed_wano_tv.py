"""
Loads the Wano TV placeholder catalogue (scripts/wano_tv_seed.json) into
MongoDB. Safe to re-run: titles are upserted by slug, so edits to the JSON
overwrite what's there without duplicating anything.

Run from the repo root:
    python -m scripts.seed_wano_tv
"""

import asyncio
import json
import os
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings

SEED_FILE = os.path.join(os.path.dirname(__file__), "wano_tv_seed.json")


async def seed():
    with open(SEED_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.DATABASE_NAME]
    now = datetime.utcnow()

    try:
        for title in data["titles"]:
            await db.tv_titles.update_one(
                {"slug": title["slug"]},
                {
                    "$set": {**title, "is_active": True, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )

        print(f"Seeded {len(data['titles'])} titles into {settings.DATABASE_NAME}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(seed())
