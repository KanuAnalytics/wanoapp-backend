import time
from datetime import datetime, timedelta
import pytest
from bson import ObjectId
from tests.pressure.conftest import make_user
from tests.pressure.test_safe_mod import _switch
from app.services import staff
pytestmark = pytest.mark.asyncio
D = 'trusted-phone-123'

async def prepare_staff(client, db):
    uid, h = await make_user(db, user_type='admin')
    secret = staff.new_secret()
    await db.users.update_one({'_id':ObjectId(uid)}, {'$set':{'staff_totp':{'secret':secret}}})
    await _switch(db, safe_ban=True, warnings=True, mod_tools=True)
    r = await client.post('/api/v1/mod/session', json={'device':D,'code':staff._code(secret,int(time.time()//30))},headers=h)
    assert r.status_code == 200, r.text
    return uid,h,r.json()['session']

async def enroll(client,h,sid):
    return await client.post('/api/v1/mod/devices/enroll',json={'device':D},headers={**h,'X-Staff-Session':sid})

async def unlock(client,h,token,device=D):
    return await client.post('/api/v1/mod/devices/session',json={'device':device,'credential':token},headers=h)

async def test_enrollment_and_unlock(client,db):
    uid,h,sid=await prepare_staff(client,db)
    r=await enroll(client,h,sid); assert r.status_code==200,r.text
    token=r.json()['credential']; stored=await db.staff_trusted_devices.find_one({})
    assert token not in str(stored)
    s=await unlock(client,h,token); assert s.status_code==200,s.text
    headers={**h,'X-Staff-Session':s.json()['session'],'X-Staff-Device':D}
    assert (await client.get('/api/v1/mod/inbox',headers=headers)).status_code==200
    # A biometric session cannot enroll another credential.
    assert (await enroll(client,h,s.json()['session'])).status_code==401

async def test_binding_expiry_and_revoke(client,db):
    uid,h,sid=await prepare_staff(client,db)
    token=(await enroll(client,h,sid)).json()['credential']
    assert (await unlock(client,h,token,'another-phone')).status_code==401
    _,other=await make_user(db,user_type='admin')
    assert (await unlock(client,other,token)).status_code==401
    assert (await unlock(client,h,'x'*43)).status_code==401
    await staff.end_all(db,ObjectId(uid))
    assert (await unlock(client,h,token)).status_code==401

async def test_old_session_cannot_enroll(client,db):
    uid,h,sid=await prepare_staff(client,db)
    await db.staff_sessions.update_one({'_id':staff._hash(sid)},{'$set':{'created_at':datetime.utcnow()-timedelta(minutes=6)}})
    assert (await enroll(client,h,sid)).status_code==401
    assert await db.staff_trusted_devices.count_documents({})==0

@pytest.mark.parametrize('change',['expired','reset','role','global_revoke'])
async def test_trust_stops_on_security_change(client,db,change):
    uid,h,sid=await prepare_staff(client,db)
    token=(await enroll(client,h,sid)).json()['credential']
    if change=='expired':
        await db.staff_trusted_devices.update_many({}, {'$set':{'ends_at':datetime.utcnow()-timedelta(seconds=1)}})
    elif change=='reset':
        await db.users.update_one({'_id':ObjectId(uid)},{'$set':{'staff_totp.secret':staff.new_secret()}})
    elif change=='role':
        await db.users.update_one({'_id':ObjectId(uid)},{'$set':{'user_type':'standard'}})
        staff.forget()
    else: await staff.end_all(db)
    assert (await unlock(client,h,token)).status_code in (401,403)

async def test_reenrollment_replaces_previous_credential(client,db):
    uid,h,sid=await prepare_staff(client,db)
    first=(await enroll(client,h,sid)).json()['credential']
    second=(await enroll(client,h,sid)).json()['credential']
    assert await db.staff_trusted_devices.count_documents({'user':ObjectId(uid)})==1
    assert (await unlock(client,h,first)).status_code==401
    assert (await unlock(client,h,second)).status_code==200

async def test_revocation_epoch_fences_sessions_issued_during_race(client,db):
    uid,h,sid=await prepare_staff(client,db)
    await staff.end_all(db,ObjectId(uid))
    # A request that already read the old account cannot resurrect access by
    # inserting a late session after the sign-out sweep.
    late='late-session-created-from-old-user-snapshot'
    now=datetime.utcnow()
    await db.staff_sessions.insert_one({'_id':staff._hash(late),'user':ObjectId(uid),
        'device':staff._hash(D),'created_at':now,'last_at':now,'ends_at':now+timedelta(hours=2),'trust_version':0})
    assert not await staff.check_session(db,ObjectId(uid),late,D)
