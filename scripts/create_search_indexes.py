"""
Create the Atlas Search indexes used by app/services/search.py.

    python -m scripts.create_search_indexes            # show what would be created
    python -m scripts.create_search_indexes --apply    # create (Atlas builds them in the background)

Then set ATLAS_SEARCH=true in Doppler and redeploy. Until the index has
finished building, search falls back to word matching on its own.
"""

import asyncio
import json
import sys

from app.services.search import COMMENT_INDEX, VIDEO_INDEX

DEFINITIONS = [
    {"collection": "videos", "name": VIDEO_INDEX, "definition": {"mappings": {"dynamic": False, "fields": {
        "title": {"type": "string", "analyzer": "lucene.standard"},
        "description": {"type": "string", "analyzer": "lucene.standard"},
        "hashtags": {"type": "string", "analyzer": "lucene.standard"},
        "views_count": {"type": "number"},
    }}}},
    {"collection": "comments", "name": COMMENT_INDEX, "definition": {"mappings": {"dynamic": False, "fields": {
        "content": {"type": "string", "analyzer": "lucene.standard"},
        "video_id": {"type": "objectId"},
    }}}},
]


async def run(db, apply: bool = False) -> list:
    out = []
    for d in DEFINITIONS:
        existing = set()
        try:
            existing = {i["name"] async for i in db[d["collection"]].aggregate([{"$listSearchIndexes": {}}])}
        except Exception as e:
            out.append((d["collection"], d["name"], f"cannot list search indexes: {e}"))
            continue
        if d["name"] in existing:
            out.append((d["collection"], d["name"], "exists"))
        elif not apply:
            out.append((d["collection"], d["name"], "would create"))
        else:
            await db.command({"createSearchIndexes": d["collection"],
                              "indexes": [{"name": d["name"], "definition": d["definition"]}]})
            out.append((d["collection"], d["name"], "created"))
    return out


async def main():
    from motor.motor_asyncio import AsyncIOMotorClient
    from app.core.config import settings
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    try:
        for row in await run(client[settings.DATABASE_NAME], apply="--apply" in sys.argv):
            print(*row, sep="  ")
        if "--apply" not in sys.argv:
            print(json.dumps(DEFINITIONS, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
