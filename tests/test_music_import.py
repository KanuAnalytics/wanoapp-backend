import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from app.services import music_import as m


def row():
    return m.metadata(json.dumps({'title':'Test song', 'genres':['instrumental'], 'source_categories':['Chill'],
        'audio_sha256':hashlib.sha256(b'audio').hexdigest(), 'cover_sha256':hashlib.sha256(b'cover').hexdigest()}))


def db():
    return SimpleNamespace(song_genres=SimpleNamespace(count_documents=AsyncMock(return_value=1)),
        songs=SimpleNamespace(find_one=AsyncMock(return_value=None),
                              update_one=AsyncMock(return_value=SimpleNamespace(upserted_id='new'))))


def files():
    return UploadFile(io.BytesIO(b'audio')), UploadFile(io.BytesIO(b'cover'))


def test_manifest_rejects_invalid_genres_checksums_and_titles():
    for change in [{'title':''}, {'genres':['../bad']}, {'audio_sha256':'x'}, {'source_categories':'wrong'}]:
        with pytest.raises(m.ImportErrorDetail):m.metadata(json.dumps({**row(),**change}))
    with pytest.raises(m.ImportErrorDetail):m.metadata('[]')


def test_success_includes_cover_genres_and_only_inserts(monkeypatch):
    database=db()
    monkeypatch.setattr(m,'inspect_media',lambda *args:120.5)
    monkeypatch.setattr(m,'upload_pair',lambda *args:['https://cdn/song.m4a','https://cdn/song.webp'])
    result=asyncio.run(m.import_song(database,row(),*files(),'admin'))
    assert result['created'] and result['cover_url'].endswith('.webp')
    change=database.songs.update_one.call_args.args[1]
    assert set(change)=={'$setOnInsert'}
    doc=change['$setOnInsert']
    assert doc['thumbnail']=='https://cdn/song.webp' and doc['category']==['instrumental']
    assert doc['source_categories']==['Chill'] and doc['duration_sec']==120.5


def test_retry_never_uploads_or_changes_existing_song(monkeypatch):
    database=db()
    database.songs.find_one.return_value={'id':'old','title':'Existing','uri':'a','thumbnail':'c'}
    monkeypatch.setattr(m,'inspect_media',lambda *args:120.5)
    def fail(*a):raise AssertionError('must not upload on retry')
    monkeypatch.setattr(m,'upload_pair',fail)
    result=asyncio.run(m.import_song(database,row(),*files(),'admin'))
    assert result=={'id':'old','title':'Existing','created':False,'uri':'a','cover_url':'c'}
    database.songs.update_one.assert_not_called()


def test_failed_cover_upload_does_not_publish(monkeypatch):
    database=db()
    monkeypatch.setattr(m,'inspect_media',lambda *args:120.5)
    def fail(*a):raise RuntimeError('cover upload failed')
    monkeypatch.setattr(m,'upload_pair',fail)
    with pytest.raises(RuntimeError):asyncio.run(m.import_song(database,row(),*files(),'admin'))
    database.songs.update_one.assert_not_called()


def test_unknown_genre_rejected_before_upload(monkeypatch):
    database=db();database.song_genres.count_documents.return_value=0
    with pytest.raises(m.ImportErrorDetail) as e:asyncio.run(m.import_song(database,row(),*files(),'admin'))
    assert e.value.status==422
    database.songs.update_one.assert_not_called()


def test_cover_checksum_rejected_before_publish():
    database=db();audio,_=files()
    with pytest.raises(m.ImportErrorDetail) as e:
        asyncio.run(m.import_song(database,row(),audio,UploadFile(io.BytesIO(b'wrong')),'admin'))
    assert e.value.status==422
    database.songs.update_one.assert_not_called()


def test_size_limit(tmp_path):
    with pytest.raises(m.ImportErrorDetail) as e:
        asyncio.run(m.save_upload(UploadFile(io.BytesIO(b'too big')),tmp_path/'file',2,'0'*64))
    assert e.value.status==413


def test_invalid_media_is_rejected(tmp_path):
    audio=tmp_path/'audio.m4a';cover=tmp_path/'cover.webp'
    audio.write_bytes(b'not music');cover.write_bytes(b'not a cover')
    with pytest.raises(m.ImportErrorDetail):m.inspect_media(audio,cover)
