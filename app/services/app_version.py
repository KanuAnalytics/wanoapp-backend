"""The "latest app version" the app compares itself with (GET /config/version).

An app older than the number set for its platform shows a full-screen
"Update Available" box with only an Update Now button, which opens the store.
So these numbers force everyone below them to update: never set one higher
than the version people can actually download from that store today.

The version check reads the newest config document, so the numbers are always
written into that one (the same document the feature switches use).
"""

import re
from typing import Dict, Optional

from pymongo import ReturnDocument

# platform -> config field. "all" is what apps fall back to when their own
# platform's number is empty, and what older app versions may read.
FIELDS = {"android": "appVersionAndroid", "ios": "appVersionIos", "all": "appVersionNumber"}
VERSION_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
NOT_FORCED = "0.0.0"


class VersionError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().strip('"').strip()
    return s or None


def parse(v: str):
    return tuple(int(x) for x in v.split("."))


async def current(db) -> Dict[str, Optional[str]]:
    doc = await db.config.find_one({}, projection={f: 1 for f in FIELDS.values()}, sort=[("_id", -1)]) or {}
    return {p: _clean(doc.get(f)) for p, f in FIELDS.items()}


async def set_version(db, platform: str, version: Optional[str]) -> Dict[str, Optional[str]]:
    if platform not in FIELDS:
        raise VersionError(422, "Platform must be android, ios or all")
    v = _clean(version)
    if v is None:
        if platform == "all":
            # The version check answers 404 without this field; "not forced" is 0.0.0.
            v = NOT_FORCED
    elif not VERSION_RE.match(v):
        raise VersionError(422, "Write the version like 2.3.32")
    field = FIELDS[platform]
    update = {"$set": {field: v}} if v is not None else {"$unset": {field: ""}}
    doc = await db.config.find_one_and_update({}, update, sort=[("_id", -1)], return_document=ReturnDocument.AFTER)
    if not doc:
        await db.config.insert_one({FIELDS["all"]: NOT_FORCED, **({field: v} if v is not None else {})})
    else:
        if not _clean(doc.get(FIELDS["all"])):
            await db.config.update_one({"_id": doc["_id"]}, {"$set": {FIELDS["all"]: NOT_FORCED}})
    return await current(db)
