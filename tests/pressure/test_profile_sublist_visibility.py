"""Isolated audit reproductions. No external messages or posts are sent."""
import pytest
from bson import ObjectId
from tests.pressure.conftest import make_user, make_video
pytestmark=pytest.mark.asyncio

async def test_DEEP_liked_list_does_not_disclose_a_video_made_private(client,db):
    owner,_=await make_user(db)
    vid=await make_video(db,owner,title="private-title-after-change",urls={"hls_playlist":"https://cdn.example.com/private/master.m3u8","thumbnail":"https://cdn.example.com/private/thumb.jpg"})
    liker,_=await make_user(db,liked_videos=[ObjectId(vid)])
    viewer,h=await make_user(db)
    await db.videos.update_one({'_id':ObjectId(vid)},{'$set':{'privacy':'private'}})
    assert (await client.get(f'/api/v1/videos/{vid}',headers=h)).status_code==404
    r=await client.get(f'/api/v1/users/{liker}/complete?include_liked=true',headers=h)
    assert r.status_code==200,r.text
    leaked=next((v for v in r.json()['user']['liked_videos'] if v['id']==vid),None)
    if leaked is not None: print('SYNTHETIC_PRIVATE_RESPONSE',leaked['title'],leaked.get('url'))
    assert leaked is None,r.json()['user']['liked_videos']

async def test_DEEP_saved_list_does_not_disclose_another_creators_now_private_video(client,db):
    owner,_=await make_user(db)
    vid=await make_video(db,owner,title="private-saved-title")
    viewer,h=await make_user(db,bookmarked_videos=[ObjectId(vid)])
    await db.videos.update_one({'_id':ObjectId(vid)},{'$set':{'privacy':'private'}})
    r=await client.get(f'/api/v1/users/{viewer}/complete',headers=h)
    assert r.status_code==200,r.text
    assert not any(v['id']==vid for v in r.json()['user']['bookmarked_videos']),r.json()['user']['bookmarked_videos']

async def test_DEEP_liked_list_respects_creator_blocking_viewer(client,db):
    owner,_=await make_user(db)
    vid=await make_video(db,owner,title="blocked-creator-title")
    liker,_=await make_user(db,liked_videos=[ObjectId(vid)])
    viewer,h=await make_user(db,blocked_by=[ObjectId(owner)])
    await db.users.update_one({'_id':ObjectId(owner)},{'$addToSet':{'blocked_users':ObjectId(viewer)}})
    r=await client.get(f'/api/v1/users/{liker}/complete?include_liked=true',headers=h)
    assert r.status_code==200,r.text
    assert not any(v['id']==vid for v in r.json()['user']['liked_videos']),r.json()['user']['liked_videos']

async def test_DEEP_CONTROL_public_liked_video_remains_visible(client,db):
    owner,_=await make_user(db)
    vid=await make_video(db,owner,title="public-control")
    liker,_=await make_user(db,liked_videos=[ObjectId(vid)])
    viewer,h=await make_user(db)
    r=await client.get(f'/api/v1/users/{liker}/complete?include_liked=true',headers=h)
    assert r.status_code==200,r.text
    assert any(v['id']==vid for v in r.json()['user']['liked_videos'])

async def test_DEEP_CONTROL_creator_can_see_own_private_saved_item(client,db):
    owner,h=await make_user(db)
    vid=await make_video(db,owner,privacy="private",title="own-private-control")
    await db.users.update_one({'_id':ObjectId(owner)},{'$set':{'bookmarked_videos':[ObjectId(vid)]}})
    r=await client.get(f'/api/v1/users/{owner}/complete',headers=h)
    assert r.status_code==200,r.text
    assert any(v['id']==vid for v in r.json()['user']['bookmarked_videos'])

@pytest.mark.parametrize('kind', ['liked', 'bookmarked'])
async def test_private_items_are_filtered_from_standalone_lists(client, db, kind):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, privacy='private')
    field = 'liked_videos' if kind == 'liked' else 'bookmarked_videos'
    viewer, headers = await make_user(db, **{field: [ObjectId(vid)]})
    response = await client.get(f'/api/v1/users/me/{kind}-videos', headers=headers)
    assert response.status_code == 200
    assert response.json() == []

@pytest.mark.parametrize('hidden', [
    {'trial': True}, {'status': 'scheduled'}, {'status': 'publishing'},
    {'moderation': {'state': 'held'}}, {'moderation': {'state': 'removed'}},
])
async def test_unpublished_items_are_filtered_from_profile_lists(client, db, hidden):
    owner, _ = await make_user(db)
    vid = await make_video(db, owner, **hidden)
    liker, _ = await make_user(db, liked_videos=[ObjectId(vid)])
    _, headers = await make_user(db)
    response = await client.get(f'/api/v1/users/{liker}/complete?include_liked=true', headers=headers)
    assert response.status_code == 200
    assert response.json()['user']['liked_videos'] == []
