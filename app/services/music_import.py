"""Add-only, content-addressed music imports. Existing songs are never edited."""
import asyncio
import hashlib
import json
import math
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


class ImportErrorDetail(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


def metadata(raw):
    try:
        row = json.loads(raw)
    except (ValueError, TypeError):
        raise ImportErrorDetail(422, "Invalid music manifest")
    if not isinstance(row, dict):
        raise ImportErrorDetail(422, "Invalid music manifest")
    title = row.get('title')
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200:
        raise ImportErrorDetail(422, "A title of 1 to 200 characters is required")
    for key in ('audio_sha256', 'cover_sha256'):
        if not isinstance(row.get(key), str) or not re.fullmatch('[a-f0-9]{64}', row[key]):
            raise ImportErrorDetail(422, "Audio and cover checksums are required")
    genres = row.get('genres')
    if not isinstance(genres, list) or not 1 <= len(genres) <= 8 or any(
        not isinstance(g, str) or not re.fullmatch('[a-z0-9-]{1,64}', g) for g in genres
    ):
        raise ImportErrorDetail(422, "Pick existing music genres")
    categories = row.get('source_categories', [])
    if not isinstance(categories, list) or len(categories) > 10 or any(
        not isinstance(c, str) or not 1 <= len(c) <= 100 for c in categories
    ):
        raise ImportErrorDetail(422, "Invalid source categories")
    return {"title": title.strip(), "genres": list(dict.fromkeys(genres)),
            "source_categories": list(dict.fromkeys(categories)),
            "audio_sha256": row['audio_sha256'], "cover_sha256": row['cover_sha256']}


async def save_upload(upload, path, limit, expected):
    size = 0
    digest = hashlib.sha256()
    with path.open('wb') as f:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ImportErrorDetail(413, "Music file exceeds the upload limit")
            digest.update(chunk)
            f.write(chunk)
    if not size or digest.hexdigest() != expected:
        raise ImportErrorDetail(422, "The audio or artwork does not match its manifest checksum")


def inspect_media(audio, cover):
    def probe(path):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_streams', '-show_format',
                                     '-of', 'json', str(path)], capture_output=True, text=True, timeout=20, check=True)
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise ImportErrorDetail(422, "Could not read the audio or cover")
    a, c = probe(audio), probe(cover)
    streams = a.get('streams', [])
    if len(streams) != 1 or streams[0].get('codec_name') != 'aac' or streams[0].get('codec_type') != 'audio':
        raise ImportErrorDetail(422, "Music must be an audio-only AAC M4A file")
    if streams[0].get('sample_rate') != '48000' or streams[0].get('channels') != 2:
        raise ImportErrorDetail(422, "Music must be 48 kHz stereo")
    if not set(a.get('format', {}).get('format_name', '').split(',')) & {'mov', 'mp4', 'm4a'}:
        raise ImportErrorDetail(422, "Music must use the M4A container")
    images = c.get('streams', [])
    if len(images) != 1 or images[0].get('codec_name') != 'webp' or (
        images[0].get('width'), images[0].get('height')) != (800, 800):
        raise ImportErrorDetail(422, "Artwork must be an 800 by 800 WebP")
    try:
        duration = float(a['format']['duration'])
    except (ValueError, KeyError, TypeError):
        raise ImportErrorDetail(422, "Audio duration is missing")
    if not math.isfinite(duration) or not 0 < duration <= 3600:
        raise ImportErrorDetail(422, "Audio must be between zero and 60 minutes")
    return duration


def upload_pair(audio, cover, digest, cover_digest):
    from app.core.config import settings
    from app.services.upload_DO import _spaces_client
    client = _spaces_client()
    urls = []
    for path, ext, mime in ((audio, 'm4a', 'audio/mp4'), (cover, 'webp', 'image/webp')):
        key = f'songs/imports/{digest if ext == "m4a" else cover_digest}.{ext}'
        client.upload_file(str(path), settings.DO_SPACES_BUCKET_NAME, key,
                           ExtraArgs={'ContentType': mime, 'ACL': 'public-read',
                                      'CacheControl': 'public, max-age=31536000, immutable'})
        if client.head_object(Bucket=settings.DO_SPACES_BUCKET_NAME, Key=key)['ContentLength'] != path.stat().st_size:
            raise RuntimeError('Uploaded music size mismatch')
        urls.append(f'{settings.DO_SPACES_CDN_URL.rstrip("/")}/{settings.DO_SPACES_BUCKET_NAME}/{key}')
    return urls


async def import_song(db, row, audio, cover, admin):
    valid = await db.song_genres.count_documents({'key': {'$in': row['genres']}, 'is_active': {'$ne': False}})
    if valid != len(row['genres']):
        raise ImportErrorDetail(422, "One or more music genres do not exist")
    song_id = 'wm-' + row['audio_sha256'][:32]
    with tempfile.TemporaryDirectory(prefix='wano-music-') as tmp:
        ap, cp = Path(tmp) / 'audio.m4a', Path(tmp) / 'cover.webp'
        await save_upload(audio, ap, 50 * 1024 * 1024, row['audio_sha256'])
        await save_upload(cover, cp, 1024 * 1024, row['cover_sha256'])
        duration = await asyncio.to_thread(inspect_media, ap, cp)
        previous = await db.songs.find_one({'_id': song_id})
        if previous:
            return {'id': previous['id'], 'title': previous['title'], 'created': False,
                    'uri': previous['uri'], 'cover_url': previous['thumbnail']}
        # Neither a partial upload nor invalid media can create a visible song.
        uri, thumbnail = await asyncio.to_thread(upload_pair, ap, cp, row['audio_sha256'], row['cover_sha256'])
    latest = await db.songs.find_one({'order': {'$type': 'number'}}, sort=[('order', -1)]) or {}
    seconds = round(duration)
    doc = {'id': song_id, 'title': row['title'], 'artist': 'Wano Originals',
           'uri': uri, 'thumbnail': thumbnail, 'duration': f'{seconds // 60}:{seconds % 60:02d}',
           'duration_sec': duration, 'category': row['genres'], 'source_categories': row['source_categories'],
           'order': latest.get('order', 0) + 1, 'is_active': True, 'createdAt': datetime.utcnow(),
           'import_batch': 'wano-music-2026-11-23', 'imported_by': admin,
           'audio_sha256': row['audio_sha256'], 'cover_sha256': row['cover_sha256'], 'ai_generated': True}
    result = await db.songs.update_one({'_id': song_id}, {'$setOnInsert': doc}, upsert=True)
    if not result.upserted_id:
        # Another request may have completed the same song while we uploaded.
        winner = await db.songs.find_one({'_id': song_id})
        return {'id': winner['id'], 'title': winner['title'], 'created': False,
                'uri': winner['uri'], 'cover_url': winner['thumbnail']}
    return {'id': song_id, 'title': doc['title'], 'created': bool(result.upserted_id),
            'uri': uri, 'cover_url': thumbnail}
