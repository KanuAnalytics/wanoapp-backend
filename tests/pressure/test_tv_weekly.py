from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import pytest
from bson import ObjectId
from app.services import tv_release, tv_admin, tv_more
from tests.pressure.conftest import make_user
from tests.pressure.test_tv_series import _series, _titles
from tests.pressure.test_tv_protect import CDN, MASTER, VARIANT, spaces
from tests.pressure.test_for_you import mix_on, rb
from tests.pressure.test_tv_round3 import _on

pytestmark = pytest.mark.asyncio

async def scheduled(db):
    tid = await _series(db, seasons=1, per=2)
    future = datetime.now(timezone.utc) + timedelta(days=7)
    await db.tv_titles.update_one({'_id':ObjectId(tid)}, {'$set':{'seasons.0.episodes.0.publish_at':future}}) # e2
    return tid

async def test_embargo_removes_urls_but_shows_safe_schedule(client, db, spaces, rb):
    tid = await scheduled(db)
    t = (await _titles(client))['roots']
    assert [e['id'] for e in t['seasons'][0]['episodes']] == ['s1e1']
    assert t['upcoming_episodes'][0]['id'] == 's1e2'
    assert 'video_url' not in t['upcoming_episodes'][0]
    assert 's1e2/master' not in str(t)
    assert t['episode_count'] == 1

async def test_released_token_cannot_open_future_episode(client, db, spaces, rb):
    await scheduled(db)
    spaces.objects['wano-tv/roots/s1e1/master.m3u8'] = MASTER
    spaces.objects['wano-tv/roots/s1e2/master.m3u8'] = MASTER
    t = (await _titles(client))['roots']
    path = urlparse(t['video_url']).path
    assert (await client.get(path)).status_code == 200
    assert (await client.get(path.replace('s1e1','s1e2'))).status_code == 404

async def test_future_episode_cannot_enter_history_progress_or_download(client, db, spaces, rb):
    tid=await scheduled(db)
    _,h=await make_user(db)
    await _on(db,'tv_plus','tv_downloads')
    assert (await client.post(f'/api/v1/tv/history/{tid}?episode=s1e2',headers=h)).status_code == 404
    assert (await client.put(f'/api/v1/tv/plus/progress/{tid}',headers=h,json={'episode':'s1e2','position':60,'duration':600})).status_code == 404
    with pytest.raises(Exception) as e:
        await tv_more.download(db,str((await db.users.find_one({}))['_id']),tid,'s1e2')
    assert e.value.status_code == 404

async def test_publish_time_requires_zone_and_hidden_stays_hidden(db):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        tv_admin.EpisodeIn(season=1,number=1,title='E',publish_at='2026-11-21T19:00:00')
    ep=tv_admin.EpisodeIn(season=1,number=1,title='E',publish_at='2026-11-21T19:00:00+03:00')
    assert ep.publish_at.hour == 16
    assert not tv_release.released({'is_published':False,'publish_at':datetime(2020,1,1)})
    assert not tv_release.released({'publish_at':'broken'})

async def test_worker_premiere_waits_then_releases_ready_series(db, spaces, rb):
    tid=await _series(db,seasons=1,per=1)
    now=datetime.now(timezone.utc)
    await db.tv_titles.update_one({'_id':ObjectId(tid)},{'$set':{'status':'coming_soon','is_active':False,'premiere_at':now+timedelta(hours=1)}})
    assert await tv_release.publish_due(db,now)==0
    await db.tv_titles.update_one({'_id':ObjectId(tid)},{'$set':{'premiere_at':now-timedelta(seconds=1)}})
    assert await tv_release.publish_due(db,now)==1
    assert await tv_release.publish_due(db,now)==0
    assert (await db.tv_titles.find_one({'_id':ObjectId(tid)}))['is_active'] is True

async def test_weekly_push_retries_failure_and_is_deduplicated(db, spaces, rb, monkeypatch):
    tid=await _series(db,seasons=1,per=1)
    uid,_=await make_user(db,expo_push_tokens=['ExponentPushToken[test]'])
    now=datetime.now(timezone.utc)
    doc=await db.tv_titles.find_one({'_id':ObjectId(tid)})
    ep={**doc['seasons'][0]['episodes'][0],'publish_at':now-timedelta(seconds=1)}
    await db.tv_reminders.insert_one({'_id':'reminder','title_id':ObjectId(tid),'user_id':ObjectId(uid),'at':now-timedelta(days=1)})
    monkeypatch.setattr("app.services.growth_notifications._quiet", lambda u,n: False)
    calls=[]
    async def fail(msgs): calls.append(msgs);return 0
    monkeypatch.setattr(tv_more,'_send',fail)
    assert await tv_release._notify(db,doc,ep,now)==0
    async def ok(msgs): calls.append(msgs);return len(msgs)
    monkeypatch.setattr(tv_more,'_send',ok)
    assert await tv_release._notify(db,doc,ep,now)==1
    assert await tv_release._notify(db,doc,ep,now)==0
    assert len(calls)==2

async def test_playback_events_are_idempotent_and_future_episode_rejected(client,db,spaces,rb):
    tid=await scheduled(db)
    _,h=await make_user(db)
    body={'session':'1234567890abcdef','kind':'play_start','episode':'s1e1'}
    url=f'/api/v1/tv/titles/{tid}/events'
    for _ in range(2): assert (await client.post(url,headers=h,json=body)).status_code==200
    assert await db.tv_playback_sessions.count_documents({})==1
    body.update(kind='complete',position=600,duration=600)
    assert (await client.post(url,headers=h,json=body)).status_code==200
    row=await db.tv_playback_sessions.find_one({})
    assert row['completed']==1 and row['position']==600
    body['episode']='s1e2'
    assert (await client.post(url,headers=h,json=body)).status_code==404


async def test_analytics_report_separates_completed_and_early_exits(db,spaces,rb):
    from app.services import tv_analytics
    from app.api.v1.tv import PlaybackEventIn
    tid=await _series(db,seasons=1,per=1)
    for session,kind,position in [('1234567890abcdef','progress',20),('abcdef1234567890','complete',100)]:
        await tv_analytics.record(db,tid,'viewer',PlaybackEventIn(session=session,kind=kind,episode='s1e1',position=position,duration=100))
    rows=await tv_analytics.report(db,30)
    assert rows[0]['starts']==2 and rows[0]['completed']==1
    assert rows[0]['stopped_under_25']==1 and rows[0]['stopped_25_50']==0
