"""Authenticated, switch-gated music import for the existing library."""
import logging
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from app.api.deps import require_admin
from app.core.database import get_database
from app.services import admin_log, features, music_import

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post('/music/import')
async def import_music(manifest: str = Form(..., max_length=10000),
                       audio: UploadFile = File(...), cover: UploadFile = File(...),
                       admin: str = Depends(require_admin)):
    db = get_database()
    try:
        if not (await features.current(db)).get('music_import'):
            raise HTTPException(409, 'Turn on music_import first')
        row = music_import.metadata(manifest)
        result = await music_import.import_song(db, row, audio, cover, admin)
        if result['created']:
            await admin_log.record(db, admin, 'music_import', {'id': result['id'], 'title': result['title'], 'genres': row['genres']})
        return result
    except music_import.ImportErrorDetail as exc:
        raise HTTPException(exc.status, exc.detail)
    except HTTPException:
        raise
    except Exception:
        logger.exception('Music import failed')
        raise HTTPException(503, 'Music import failed. Retry this track; existing songs are unchanged.')
    finally:
        await audio.close()
        await cover.close()
