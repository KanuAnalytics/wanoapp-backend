"""
scripts/import_songs.py

Fills and maintains the `songs` and `music_genres` collections. Every command
is safe to run more than once. Run from the repo root (uses MONGODB_URL and
DATABASE_NAME from .env):

    python -m scripts.import_songs import-json
        Copy app/api/v1/json/songs.json into `songs`, matched on song_id.
        Existing genres, artist and cover are kept.

    python -m scripts.import_songs import-csv path/to/songs.csv
        Update songs from a CSV with the columns
        song_id,genres,artist,cover_url   (genres separated by "|").
        Empty cells are left unchanged. Unknown genres are created.

    python -m scripts.import_songs seed-test-genres
        Create a few test genres and spread them over some songs, so the
        genre tabs can be tested before the client's data arrives.

    python -m scripts.import_songs clear-test-genres
        Remove the test genres from every song and delete them.
"""

import argparse
import csv
import json
import re
import sys
from datetime import datetime

from pymongo import MongoClient, UpdateOne

from app.core.config import settings
from app.services.songs import SONGS_FILE, duration_to_seconds

TEST_GENRES = ["Afrobeats", "Amapiano", "Hip Hop", "Pop", "R&B"]
TEST_SONGS_PER_GENRE = 8


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower().replace("&", "n")).strip("-")
    return slug or "genre"


def get_db():
    return MongoClient(settings.MONGODB_URL)[settings.DATABASE_NAME]


def ensure_indexes(db):
    db.songs.create_index([("song_id", 1)], unique=True)
    db.songs.create_index([("is_active", 1), ("sort_order", 1)])
    db.music_genres.create_index([("slug", 1)], unique=True)


def ensure_genre(db, name: str, is_test: bool = False) -> str:
    slug = slugify(name)
    now = datetime.utcnow()
    next_order = db.music_genres.count_documents({})
    db.music_genres.update_one(
        {"slug": slug},
        {
            "$setOnInsert": {
                "slug": slug,
                "name": name.strip(),
                "sort_order": next_order,
                "is_active": True,
                "is_test": is_test,
                "created_at": now,
            },
            "$set": {"updated_at": now},
        },
        upsert=True,
    )
    return slug


def import_json(db):
    with open(SONGS_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    now = datetime.utcnow()
    ops = []
    skipped = 0
    for index, song in enumerate(raw):
        if song.get("id") is None or not song.get("uri"):
            skipped += 1
            continue
        duration = song.get("duration")
        ops.append(UpdateOne(
            {"song_id": str(song["id"])},
            {
                "$set": {
                    "title": song.get("title") or "",
                    "uri": song["uri"],
                    "duration": duration,
                    "duration_sec": duration_to_seconds(duration),
                    "sort_order": index,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "song_id": str(song["id"]),
                    "artist": None,
                    "cover_url": None,
                    "genres": [],
                    "is_active": True,
                    "created_at": now,
                },
            },
            upsert=True,
        ))
    if ops:
        result = db.songs.bulk_write(ops, ordered=False)
        print(f"songs.json: {len(ops)} songs, {result.upserted_count} new, "
              f"{result.modified_count} updated, {skipped} skipped")
    print(f"songs collection now has {db.songs.count_documents({})} songs")


def import_csv(db, path: str):
    now = datetime.utcnow()
    updated = missing = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            song_id = (row.get("song_id") or "").strip()
            if not song_id:
                continue
            fields = {"updated_at": now}
            genres_cell = (row.get("genres") or "").strip()
            if genres_cell:
                fields["genres"] = [ensure_genre(db, g) for g in genres_cell.split("|") if g.strip()]
            for column in ("artist", "cover_url"):
                value = (row.get(column) or "").strip()
                if value:
                    fields[column] = value
            result = db.songs.update_one({"song_id": song_id}, {"$set": fields})
            if result.matched_count:
                updated += 1
            else:
                missing += 1
                print(f"  song_id {song_id} not found, skipped")
    print(f"CSV: {updated} songs updated, {missing} not found")


def seed_test_genres(db):
    song_ids = [s["song_id"] for s in db.songs.find({}, {"song_id": 1}).sort("sort_order", 1)]
    if not song_ids:
        sys.exit("The songs collection is empty. Run import-json first.")
    for i, name in enumerate(TEST_GENRES):
        slug = ensure_genre(db, name, is_test=True)
        # Every 3rd song from a different starting point, so genres overlap a little.
        picked = song_ids[i::3][:TEST_SONGS_PER_GENRE]
        db.songs.update_many({"song_id": {"$in": picked}}, {"$addToSet": {"genres": slug}})
        print(f"{name}: {len(picked)} songs")


def clear_test_genres(db):
    slugs = [g["slug"] for g in db.music_genres.find({"is_test": True}, {"slug": 1})]
    if not slugs:
        print("No test genres found")
        return
    result = db.songs.update_many({"genres": {"$in": slugs}}, {"$pull": {"genres": {"$in": slugs}}})
    db.music_genres.delete_many({"is_test": True})
    print(f"Removed {len(slugs)} test genres from {result.modified_count} songs")


def main():
    parser = argparse.ArgumentParser(description="Manage the songs library")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import-json")
    csv_parser = sub.add_parser("import-csv")
    csv_parser.add_argument("path")
    sub.add_parser("seed-test-genres")
    sub.add_parser("clear-test-genres")
    args = parser.parse_args()

    db = get_db()
    ensure_indexes(db)
    if args.command == "import-json":
        import_json(db)
    elif args.command == "import-csv":
        import_csv(db, args.path)
    elif args.command == "seed-test-genres":
        seed_test_genres(db)
    elif args.command == "clear-test-genres":
        clear_test_genres(db)


if __name__ == "__main__":
    main()
