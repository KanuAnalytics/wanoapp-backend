"""
Drops the five indexes Atlas Performance Advisor flagged as redundant (each is a
prefix of a compound index that stays, so reads don't change; writes get cheaper).
The app no longer creates them (app/core/database.py).

    python -m scripts.drop_redundant_indexes            # dry run: lists what would be dropped
    python -m scripts.drop_redundant_indexes --apply

Only drops an index when the compound index that covers it exists.
"""

import asyncio
import sys

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

REDUNDANT = [
    # (collection, index to drop, index that covers it)
    ("videos", "created_at_-1", "created_at_-1__id_-1"),
    ("videos", "creator_id_1", "creator_id_1_created_at_-1"),
    ("videos", "is_active_1_privacy_1", "is_active_1_privacy_1_created_at_-1"),
    ("comments", "user_id_1", "user_id_1_is_active_1_created_at_-1"),
    ("comments", "video_id_1", "video_id_1_parent_id_1_is_active_1_created_at_-1"),
]


async def run(db, apply: bool) -> list:
    out = []
    for coll, drop, keep in REDUNDANT:
        names = {i["name"] async for i in db[coll].list_indexes()}
        if drop not in names:
            out.append((coll, drop, "not there"))
        elif keep not in names:
            out.append((coll, drop, f"kept: covering index {keep} missing"))
        elif apply:
            await db[coll].drop_index(drop)
            out.append((coll, drop, "dropped"))
        else:
            out.append((coll, drop, "would drop"))
    return out


def main():
    apply = "--apply" in sys.argv
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    for coll, name, what in asyncio.run(run(client[settings.DATABASE_NAME], apply)):
        print(f"{coll}.{name}: {what}")


if __name__ == "__main__":
    main()
