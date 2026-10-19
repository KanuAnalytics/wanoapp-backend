"""
Loads the Wano TV placeholder catalogue (scripts/wano_tv_seed.json) into
MongoDB. Safe to re-run: titles are upserted by slug and genres by name, so
edits to the JSON overwrite what's there without duplicating anything.

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

        slugs = [title["slug"] for title in data["titles"]]
        hidden = await db.tv_titles.update_many(
            {"slug": {"$nin": slugs}, "is_active": True},
            {"$set": {"is_active": False, "updated_at": now}},
        )

        for genre in data["genres"]:
            await db.tv_genres.update_one(
                {"name": genre["name"]},
                {
                    "$set": {**genre, "is_active": True, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )

        print(
            f"Seeded {len(data['titles'])} titles and "
            f"{len(data['genres'])} genres into {settings.DATABASE_NAME}; "
            f"hid {hidden.modified_count} titles no longer in the seed file"
        )
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(seed())
