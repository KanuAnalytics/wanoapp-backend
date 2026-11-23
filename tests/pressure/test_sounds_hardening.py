import math
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import songs
from tests.pressure.conftest import make_user, make_video

pytestmark=pytest.mark.asyncio
CDN='https://wano-africadev.lon1.cdn.digitaloceanspaces.com/wanoafrica-dospaces-key'

async def track(db,sid='a',**extra):
 d={'id':sid,'title':'Current title','uri':f'{CDN}/songs/{sid}.m4a','thumbnail':f'{CDN}/songs/{sid}.webp',
 'duration':'2:00','duration_sec':120,'category':['instrumental'],'is_active':True,**extra}
 await db.songs.insert_one(d)
 return d

async def test_no_resurrection_when_entire_catalog_is_retired(db,monkeypatch):
 await track(db,is_active=False)
 monkeypatch.setattr(songs,'_json_songs',[{'id':'legacy','title':'Legacy','uri':'https://old/song.mp3'}])
 assert await songs.list_songs()==[]

async def test_catalog_outage_does_not_revert_to_old_catalog(db,monkeypatch):
 class Broken:
  def find(self,*a,**k):raise RuntimeError('database unavailable')
 monkeypatch.setattr(songs,'get_database',lambda:SimpleNamespace(songs=Broken()))
 with pytest.raises(RuntimeError):await songs.list_songs()

async def test_explicit_retired_id_cannot_switch_to_another_song(db):
 await track(db,'a',is_active=False);b=await track(db,'b')
 assert await songs.sound_for_post('a',b['uri']) is None

async def test_url_lookup_cannot_resurrect_retired_json_song(db,monkeypatch):
 a=await track(db,is_active=False)
 monkeypatch.setattr(songs,'_json_songs',[a])
 assert await songs.sound_for_post(None,a['uri']) is None

async def test_sound_page_includes_canonical_album_art_without_usage(client,db):
 a=await track(db)
 data=(await client.get('/api/v1/music/sounds/a/posts?authenticated=false')).json()
 assert data['sound']['cover_url']==a['thumbnail']
 assert data['sound']['available'] is True

async def test_sound_page_prefers_current_catalog_to_stale_post_snapshot(client,db):
 await track(db);u,_=await make_user(db)
 await make_video(db,u,sound={'id':'a','title':'Old title','uri':'https://old/song.mp3'})
 data=(await client.get('/api/v1/music/sounds/a/posts?authenticated=false')).json()
 assert data['sound']['title']=='Current title'
 assert data['sound']['uri']==f'{CDN}/songs/a.m4a'

async def test_retired_recording_cannot_be_reused_from_a_post(client,db):
 a=await track(db,is_active=False);u,_=await make_user(db)
 await make_video(db,u,sound={'id':'a','title':a['title'],'uri':a['uri']})
 data=(await client.get('/api/v1/music/sounds/a/posts?authenticated=false')).json()
 assert data['sound']['uri'] is None
 assert data['sound']['available'] is False

async def test_visible_owner_filter_applies_before_pagination_and_totals(client,db):
 await track(db);active,_=await make_user(db);banned,_=await make_user(db,is_active=False)
 for i in range(3):await make_video(db,banned,sound={'id':'a','title':'A','uri':'x'},views_count=100)
 for i in range(4):await make_video(db,active,sound={'id':'a','title':'A','uri':'x'},views_count=1)
 data=(await client.get('/api/v1/music/sounds/a/posts?authenticated=false&limit=2')).json()
 assert len(data['items'])==2
 assert data['stats']=={'posts':4,'creators':1}
 assert data['has_more'] is True

async def test_moods_and_ai_origin_are_exposed_separately_from_genres(client,db):
 await track(db,source_categories=['🌅 CHILL & AESTHETIC','🥁 CULTURE & ROOTS '],ai_generated=True)
 data=(await client.get('/api/v1/music/songs')).json()
 assert data['songs'][0]['moods']==['chill-aesthetic','culture-roots']
 assert data['songs'][0]['genres']==['instrumental']
 assert data['songs'][0]['ai_generated'] is True

@pytest.mark.parametrize('duration',[float('nan'),float('inf'),-1,'-2:00','nan',True])
async def test_invalid_duration_is_not_exposed(duration):
 assert songs.duration_to_seconds(duration) is None

async def test_render_uses_resolved_catalog_recording(monkeypatch):
 from app.api.v1 import videos
 capture=AsyncMock(return_value='job')
 monkeypatch.setattr(videos,'create_stitch_job',capture)
 monkeypatch.setattr(videos,'_points_category',AsyncMock(return_value=None))
 inp=SimpleNamespace(audio=SimpleNamespace(id='a',uri=f'{CDN}/songs/b.m4a'),videoUrls=[],
 keepOriginalSound=True,videoVolume=1,musicVolume=.5,title='T',description='',privacy=SimpleNamespace(value='public'),
 thumbnail=None,categoryId=None,subcategoryId=None,comments_enabled=True,points_category=None,
 trial=False,ai_generated=False,challenge_id=None)
 sound={'id':'a','title':'A','uri':f'{CDN}/songs/a.m4a'}
 await videos._create_stitch_job(inp,'user',sound,None,None)
 assert capture.call_args.kwargs['audio_url']==sound['uri']

async def test_retired_audio_url_is_rejected_even_on_own_cdn(db):
 from app.services.stitch_jobs import validate_audio_uri,StitchValidationError
 a=await track(db,is_active=False)
 with pytest.raises(StitchValidationError):await validate_audio_uri(db,a['uri'])

async def test_inactive_creators_do_not_inflate_trending(client,db):
 u,_=await make_user(db,is_active=False)
 await make_video(db,u,sound={'id':'a','title':'A','uri':'x'})
 data=(await client.get('/api/v1/music/trending?authenticated=false')).json()
 assert data==[]


async def test_catalog_version_changes_with_metadata(client,db):
 await track(db)
 first=(await client.get('/api/v1/music/songs')).json()
 second=(await client.get('/api/v1/music/songs')).json()
 assert first['catalog_version']==second['catalog_version']
 await db.songs.update_one({'id':'a'},{'$set':{'thumbnail':f'{CDN}/songs/new.webp'}})
 changed=(await client.get('/api/v1/music/songs')).json()
 assert changed['catalog_version']!=first['catalog_version']

async def test_catalog_endpoint_reports_temporary_unavailability(client,db,monkeypatch):
 from app.api.v1 import music
 monkeypatch.setattr(music,'list_songs',AsyncMock(side_effect=RuntimeError('outage')))
 response=await client.get('/api/v1/music/songs')
 assert response.status_code==503

async def test_retired_song_is_not_promoted_as_trending(client,db):
 a=await track(db,is_active=False);u,_=await make_user(db)
 await make_video(db,u,sound={'id':'a','title':'A','uri':a['uri']})
 assert (await client.get('/api/v1/music/trending?authenticated=false')).json()==[]

async def test_malformed_duration_cannot_break_catalog_json(client,db):
 await track(db,duration=float('nan'),duration_sec=float('inf'))
 response=await client.get('/api/v1/music/songs')
 assert response.status_code==200
 assert response.json()['songs'][0]['duration_sec'] is None
 assert response.json()['songs'][0]['duration'] is None

from tests.pressure.test_replies_hashtags import _stitch_body,stream_not_ready

async def test_publishing_resolves_audio_and_credit_from_same_recording(client,db,stream_not_ready):
 a=await track(db,'a');b=await track(db,'b');_,h=await make_user(db)
 response=await client.post('/api/v1/videos/stitch',headers=h,json=_stitch_body(audio={'id':'a','uri':b['uri']}))
 assert response.status_code==202,response.text
 from bson import ObjectId
 job=await db.pending_videos.find_one({'_id':ObjectId(response.json()['job_id'])})
 assert job['audio_url']==a['uri']==job['post']['sound']['uri']

async def test_publishing_rejects_unavailable_explicit_recording(client,db,stream_not_ready):
 a=await track(db,is_active=False);_,h=await make_user(db)
 response=await client.post('/api/v1/videos/stitch',headers=h,json=_stitch_body(audio={'id':'a','uri':a['uri']}))
 assert response.status_code==400,response.text
 assert await db.pending_videos.count_documents({})==0

async def test_photo_publish_rejects_retired_sound_without_creating_post(client,db):
 await track(db,is_active=False);_,h=await make_user(db)
 response=await client.post('/api/v1/videos/post',headers=h,json={
  'media_type':'photo','images':[f'{CDN}/photos/test.webp'],'sound_id':'a','title':'Photo','description':''})
 assert response.status_code==400,response.text
 assert await db.videos.count_documents({})==0

async def test_photo_publish_keeps_active_sound_credit(client,db):
 a=await track(db);_,h=await make_user(db)
 response=await client.post('/api/v1/videos/post',headers=h,json={
  'media_type':'photo','images':[f'{CDN}/photos/test.webp'],'sound_id':'a','title':'Photo','description':''})
 assert response.status_code==201,response.text
 from bson import ObjectId
 post=await db.videos.find_one({'_id':ObjectId(response.json()['video_id'])})
 assert post['sound']['id']=='a' and post['sound']['uri']==a['uri']
