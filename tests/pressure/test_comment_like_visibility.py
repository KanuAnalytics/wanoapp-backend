"""Like writes must enforce the same privacy/moderation boundaries as reading."""
import asyncio
import pytest
from bson import ObjectId
from tests.pressure.conftest import make_user, make_video
pytestmark = pytest.mark.asyncio

async def fixture(client, db):
    await db.config.insert_one({'features': {'comment_guard': True, 'comment_plus': True}})
    owner, _ = await make_user(db)
    author, ah = await make_user(db)
    actor, headers = await make_user(db)
    vid = await make_video(db, owner)
    r = await client.post('/api/v1/', json={'video_id': str(vid), 'content': 'A thoughtful comment'}, headers=ah)
    assert r.status_code == 200, r.text
    cid = r.json()['_id']
    return owner, author, actor, headers, vid, cid

@pytest.mark.parametrize('flag,value', [('mod_hidden',True), ('hidden',True), ('held','link')])
async def test_like_rejects_nonpublic_comment(client, db, flag, value):
    _,_,_,h,_,cid = await fixture(client,db)
    await db.comments.update_one({'_id':ObjectId(cid)}, {'$set':{flag:value}})
    r = await client.post(f'/api/v1/{cid}/like',headers=h)
    assert r.status_code == 404, r.text
    assert (await db.comments.find_one({'_id':ObjectId(cid)}))['likes_count'] == 0

@pytest.mark.parametrize('change',[{'privacy':'private'}, {'is_active':False}])
async def test_like_rejects_inaccessible_video(client,db,change):
    _,_,_,h,vid,cid = await fixture(client,db)
    await db.videos.update_one({'_id':ObjectId(str(vid))},{'$set':change})
    r=await client.post(f'/api/v1/{cid}/like',headers=h)
    assert r.status_code == 404, r.text

async def test_blocked_author_cannot_be_liked_by_guessed_comment_id(client,db):
    _,author,actor,h,_,cid=await fixture(client,db)
    await db.users.update_one({'_id':ObjectId(str(actor))},{'$set':{'blocked_users':[ObjectId(str(author))]}})
    r=await client.post(f'/api/v1/{cid}/like',headers=h)
    assert r.status_code == 404, r.text

async def test_repeated_toggles_keep_membership_and_count_consistent(client,db):
    _,_,actor,h,_,cid=await fixture(client,db)
    for i in range(100):
        r=await client.post(f'/api/v1/{cid}/like',headers=h)
        assert r.status_code == 200, r.text
        c=await db.comments.find_one({'_id':ObjectId(cid)})
        expected=(i+1)%2
        assert c['likes_count']==expected
        assert len(c['liked_by'])==expected
        assert r.json()=={'liked':bool(expected),'likes_count':expected}

async def test_simultaneous_likes_from_different_users_count_each_once(client,db):
    _,_,_,_,_,cid=await fixture(client,db)
    actors=[await make_user(db) for _ in range(20)]
    responses=await asyncio.gather(*(client.post(f'/api/v1/{cid}/like',headers=h) for _,h in actors))
    assert all(r.status_code==200 for r in responses)
    c=await db.comments.find_one({'_id':ObjectId(cid)})
    assert c['likes_count']==20
    assert len(set(c['liked_by']))==20

async def test_simultaneous_same_user_requests_never_duplicate_membership(client,db):
    _,_,_,h,_,cid=await fixture(client,db)
    responses=await asyncio.gather(*(client.post(f'/api/v1/{cid}/like',headers=h) for _ in range(40)))
    assert all(r.status_code==200 for r in responses)
    c=await db.comments.find_one({'_id':ObjectId(cid)})
    assert c['likes_count']==len(c['liked_by'])
    assert c['likes_count'] in (0,1)

async def test_owner_can_still_like_comment_on_own_private_post(client,db):
    await db.config.insert_one({'features':{'comment_guard':True,'comment_plus':True}})
    owner,h=await make_user(db)
    vid=await make_video(db,owner)
    r=await client.post('/api/v1/',json={'video_id':vid,'content':'My comment'},headers=h)
    cid=r.json()['_id']
    await db.videos.update_one({'_id':ObjectId(vid)},{'$set':{'privacy':'private'}})
    assert (await client.post(f'/api/v1/{cid}/like',headers=h)).status_code==200

async def test_reply_to_hidden_parent_cannot_receive_likes(client,db):
    _,_,_,h,vid,cid=await fixture(client,db)
    r=await client.post('/api/v1/',json={'video_id':vid,'content':'Reply','parent_id':cid},headers=h)
    reply=r.json()['_id']
    await db.comments.update_one({'_id':ObjectId(cid)},{'$set':{'mod_hidden':True}})
    assert (await client.post(f'/api/v1/{reply}/like',headers=h)).status_code==404
