"""
Moves the song library from country tabs to real genres, using the client's
catalogue CSV (columns: title, genre, ...; genre like "Kizomba / Semba").

Why this exists: when the client's 128 songs were first loaded (Oct 2026),
`song_genres` was filled with the album countries (Angola, Botswana, ...)
instead of genres, and each song's `category` held one country key. The
client's catalogue ("Wano Catalog - Batch 1" CSV) lists the real genres, and
a song can have several, so `category` became a list of genre keys
(app/services/songs.py reads both the old string and the list). Kept in the
repo so later catalogue batches can be run through it too: it matches songs
by title, so new songs must be in `songs` before running it.

- `song_genres`: existing documents are reused for the CSV genres (key, name,
  order are overwritten), missing ones are inserted and leftovers removed.
- `songs`: each song matched by title gets `category` = list of genre keys.
  Songs not found in the CSV are reported and left as they are.

Dry run by default; nothing is written. With --apply both collections are
first backed up as JSON next to the CSV (or to --backup-dir). Safe to re-run.

Run from the repo root:
    python -m scripts.migrate_song_genres "path/to/catalog.csv"
    python -m scripts.migrate_song_genres "path/to/catalog.csv" --apply
"""

import argparse
import asyncio
import csv
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

# Descriptive words in the CSV that aren't part of the genre name.
GENRE_RENAMES = {
    "Ethio-pop interlude": "Ethio-pop",
    "Afro-rock anthem": "Afro-rock",
}


def genre_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def title_key(title: str) -> str:
    title = title.replace("’", "'").replace("‘", "'")
    return " ".join(unicodedata.normalize("NFC", title).split()).casefold()


def read_catalog(path: str):
    """{title_key: (title, [genre names])} and genre names ordered by song count."""
    songs = {}
    counts = Counter()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            title = (row.get("title") or "").strip()
            genre = (row.get("genre") or "").strip()
            # Rows without a file path are the duplicates noted in the CSV.
            if not title or not genre or not (row.get("file_path") or "").strip():
                continue
            names = []
            for part in genre.split("/"):
                name = GENRE_RENAMES.get(part.strip(), part.strip())
                if name and name not in names:
                    names.append(name)
            key = title_key(title)
            if key in songs:
                raise SystemExit(f"Title appears twice in the CSV: {title!r}")
            songs[key] = (title, names)
            counts.update(names)
    genres = sorted(counts, key=lambda n: (-counts[n], n.casefold()))
    return songs, genres, counts


def plan_genres(existing: list, genre_names: list):
    """
    Which existing document becomes which genre. Documents already holding a
    target key keep it (so re-runs change nothing); the rest are reused in
    their current order. Returns (updates, inserts, deletes).
    """
    targets = [{"key": genre_key(n), "name": n, "order": i + 1} for i, n in enumerate(genre_names)]
    by_key = {d.get("key"): d for d in existing}
    updates, unassigned = [], []
    for target in targets:
        doc = by_key.pop(target["key"], None)
        if doc is not None:
            updates.append((doc, target))
        else:
            unassigned.append(target)
    spare = [d for d in existing if d.get("key") in by_key]
    inserts = []
    for target in unassigned:
        if spare:
            updates.append((spare.pop(0), target))
        else:
            inserts.append(target)
    return updates, inserts, spare


def backup(db_docs: dict, backup_dir: str):
    os.makedirs(backup_dir, exist_ok=True)
    for name, docs in db_docs.items():
        with open(os.path.join(backup_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            f.write(json_util.dumps(docs, indent=2, ensure_ascii=False))
    print(f"Backup written to {backup_dir}")


async def migrate(csv_path: str, apply: bool, backup_dir: str):
    catalog, genre_names, counts = read_catalog(csv_path)
    client = AsyncIOMotorClient(settings.MONGODB_URL)
    db = client[settings.DATABASE_NAME]
    try:
        existing_genres = await db.song_genres.find().sort([("order", 1), ("createdAt", 1), ("_id", 1)]).to_list(length=None)
        songs = await db.songs.find().sort([("order", 1), ("_id", 1)]).to_list(length=None)

        updates, inserts, deletes = plan_genres(existing_genres, genre_names)
        print(f"Database {settings.DATABASE_NAME}: {len(existing_genres)} genres, {len(songs)} songs")
        print(f"CSV: {len(catalog)} songs, {len(genre_names)} genres\n")
        print("Genres:")
        for doc, target in sorted(updates, key=lambda u: u[1]["order"]):
            change = "same" if doc.get("key") == target["key"] and doc.get("name") == target["name"] else f"was {doc.get('key')}"
            print(f"  {target['order']:>2}. {target['name']} ({target['key']}, {counts[target['name']]} songs) - update, {change}")
        for target in inserts:
            print(f"  {target['order']:>2}. {target['name']} ({target['key']}, {counts[target['name']]} songs) - insert")
        for doc in deletes:
            print(f"   -  {doc.get('key')} - delete")

        song_updates = []
        unmatched_db = []
        seen = Counter(title_key(s.get("title") or "") for s in songs)
        matched_keys = set()
        for song in songs:
            key = title_key(song.get("title") or "")
            if key not in catalog:
                unmatched_db.append(song)
                continue
            if seen[key] > 1:
                print(f"\n! {seen[key]} database songs are titled {song.get('title')!r}; all get the same genres")
            matched_keys.add(key)
            keys = [genre_key(n) for n in catalog[key][1]]
            if song.get("category") != keys:
                song_updates.append((song, keys))
        unmatched_csv = [t for k, (t, _) in catalog.items() if k not in matched_keys]

        print(f"\nSongs to update: {len(song_updates)} (of {len(songs) - len(unmatched_db)} matched)")
        for song, keys in song_updates:
            print(f"  {song.get('title')}: {song.get('category')} -> {keys}")
        print(f"\nDatabase songs not in the CSV (left unchanged): {len(unmatched_db)}")
        for song in unmatched_db:
            print(f"  {song.get('title')} [{song.get('category')}] id={song.get('id')}")
        print(f"\nCSV songs not in the database: {len(unmatched_csv)}")
        for title in unmatched_csv:
            print(f"  {title}")

        if not apply:
            print("\nDry run, nothing written. Re-run with --apply to write.")
            return

        backup({"song_genres": existing_genres, "songs": songs}, backup_dir)
        now = datetime.utcnow()
        # Two passes so a key moving between documents never hits the unique index.
        for doc, _ in updates:
            await db.song_genres.update_one({"_id": doc["_id"]}, {"$set": {"key": f"tmp-{doc['_id']}"}})
        for doc, target in updates:
            await db.song_genres.update_one(
                {"_id": doc["_id"]},
                {"$set": {**target, "is_active": True}},
            )
        if inserts:
            await db.song_genres.insert_many([{**t, "createdAt": now, "is_active": True} for t in inserts])
        if deletes:
            await db.song_genres.delete_many({"_id": {"$in": [d["_id"] for d in deletes]}})
        for song, keys in song_updates:
            await db.songs.update_one({"_id": song["_id"]}, {"$set": {"category": keys}})
        print(
            f"\nDone: {len(updates)} genres updated, {len(inserts)} inserted, {len(deletes)} deleted; "
            f"{len(song_updates)} songs updated"
        )
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--backup-dir", help="where --apply saves the JSON backup")
    args = parser.parse_args()
    backup_dir = args.backup_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.csv_path)),
        f"song-genres-backup-{datetime.now():%Y-%m-%d-%H%M%S}",
    )
    asyncio.run(migrate(args.csv_path, args.apply, backup_dir))
