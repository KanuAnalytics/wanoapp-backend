"""@mentions: suggestions while typing, resolving a tapped name, one push per
person mentioned. Attacks written with the code."""

import asyncio
from datetime import datetime

import pytest
from bson import ObjectId

from tests.pressure.conftest import make_user, make_video
from tests.pressure.test_engagement import _post

pytestmark = pytest.mark.asyncio
M = "/api/v1/creator/mentions"


async def _on(db):
    await db.config.insert_one({"features": {"mentions": True}})


async def _settle():
    from app.services import mentions
    for _ in range(20):
        if not mentions._tasks:
            return
        await asyncio.sleep(0.02)


def test_MN0_names_in_text():
    from app.services.mentions import names_in
    assert names_in("hi @kofi and @Kofi, also @amina.") == ["kofi", "amina"]
    assert names_in("email me@x.com @ab @" + "a" * 40) == ["a" * 30]
    assert names_in("(@paren) start@no @ok_1.2") == ["ok_1.2"]
    assert names_in(None) == []


async def test_MN1_switch_off(client, db):
    _, h = await make_user(db)
    assert (await client.get(f"{M}/suggest", params={"q": "k"}, headers=h)).status_code == 404
    assert (await client.get("/api/v1/config/features")).json()["mentions"] is False


async def test_MN2_suggestions_people_i_follow_first_then_popular(client, db):
    await _on(db)
    me, h = await make_user(db, username="me_user")
    a, _ = await make_user(db, username="kofi_small", followers_count=3)
    b, _ = await make_user(db, username="kofi_big", followers_count=900)
    c, _ = await make_user(db, username="Kampala_Kid", display_name="Kofi Mensah", followers_count=50)
    await make_user(db, username="kofi_banned", is_active=False)
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"following": [ObjectId(a)]}})
    rows = (await client.get(f"{M}/suggest", params={"q": "ko"}, headers=h)).json()["items"]
    names = [r["username"] for r in rows]
    assert names[0] == "kofi_small" and rows[0]["following"] is True
    assert names[1:] == ["kofi_big", "Kampala_Kid"]                 # display name matches too
    assert "kofi_banned" not in names


async def test_MN3_blocked_and_myself_never_suggested_regex_safe(client, db):
    await _on(db)
    me, h = await make_user(db, username="kofi_me")
    x, _ = await make_user(db, username="kofi_troll")
    await db.users.update_one({"_id": ObjectId(me)}, {"$set": {"blocked_users": [ObjectId(x)]}})
    names = [r["username"] for r in (await client.get(f"{M}/suggest", params={"q": "kofi"}, headers=h)).json()["items"]]
    assert names == []
    for q in [".*", "(", "$where", "a" * 39]:
        assert (await client.get(f"{M}/suggest", params={"q": q}, headers=h)).status_code == 200
    assert (await client.get(f"{M}/suggest", params={"q": "a" * 41}, headers=h)).status_code == 422


async def test_MN4_resolve_a_tapped_name(client, db):
    await _on(db)
    a, _ = await make_user(db, username="Amina_K")
    await make_user(db, username="gone_one", is_active=False)
    r = await client.get(f"{M}/resolve", params={"username": "amina_k", "authenticated": "false"})
    assert r.status_code == 200 and r.json()["id"] == a
    assert (await client.get(f"{M}/resolve", params={"username": "@Amina_K.", "authenticated": "false"})).status_code == 200
    for bad in ["gone_one", "nobody_here", ".*", "a b"]:
        assert (await client.get(f"{M}/resolve", params={"username": bad, "authenticated": "false"})).status_code == 404


async def test_MN5_post_mention_pushes_once_rules_respected(client, db, calls):
    await _on(db)
    me, h = await make_user(db, username="amina", display_name="Amina")
    k, _ = await make_user(db, username="kofi", expo_push_tokens=["ExponentPushToken[k]"])
    off, _ = await make_user(db, username="quiet", expo_push_tokens=["ExponentPushToken[q]"],
                             notification_prefs={"mentions": False})
    blk, _ = await make_user(db, username="blocker", expo_push_tokens=["ExponentPushToken[b]"],
                             blocked_users=[ObjectId(me)])
    calls["pushes"].clear()
    r = await _post(client, h, description="With @kofi @kofi @quiet @blocker @amina @nobody")
    assert r.status_code == 201
    await _settle()
    flat = repr(calls["pushes"])
    assert flat.count("ExponentPushToken[k]") == 1 and "Amina mentioned you in a post" in flat
    assert "ExponentPushToken[q]" not in flat and "ExponentPushToken[b]" not in flat


async def test_MN6_comment_mention_and_no_double_push(client, db, calls):
    await _on(db)
    me, h = await make_user(db, username="amina")
    k, _ = await make_user(db, username="kofi", expo_push_tokens=["ExponentPushToken[k]"])
    v = await make_video(db, me)
    calls["pushes"].clear()
    r = await client.post("/api/v1/", json={"video_id": v, "content": "@kofi look"}, headers=h)
    assert r.status_code in (200, 201), r.text
    await _settle()
    assert repr(calls["pushes"]).count("ExponentPushToken[k]") == 1
    assert "comment" in repr(calls["pushes"])


async def test_MN7_scheduled_trial_private_posts_dont_push(client, db, calls):
    await db.config.insert_one({"features": {"mentions": True, "trial_posts": True}})
    me, h = await make_user(db, username="amina")
    await make_user(db, username="kofi", expo_push_tokens=["ExponentPushToken[k]"])
    calls["pushes"].clear()
    await _post(client, h, description="@kofi", trial=True)
    await _post(client, h, description="@kofi", privacy="private")
    await _settle()
    assert calls["pushes"] == []


async def test_MN8_at_most_5_people_and_a_daily_cap(client, db, calls, monkeypatch):
    await _on(db)
    from app.services import mentions
    me, h = await make_user(db, username="amina")
    for i in range(8):
        await make_user(db, username=f"fan{i}x", expo_push_tokens=[f"ExponentPushToken[{i}]"])
    calls["pushes"].clear()
    await _post(client, h, description=" ".join(f"@fan{i}x" for i in range(8)))
    await _settle()
    assert len(calls["pushes"]) == 5
    monkeypatch.setattr(mentions, "DAILY_PER_PERSON", 2)
    calls["pushes"].clear()
    for _ in range(4):
        await _post(client, h, description="@fan0x again")
    await _settle()
    assert len(calls["pushes"]) == 1          # already 1 today from above, cap 2


async def test_MN9_switch_off_no_pushes_and_posting_never_breaks(client, db, calls, monkeypatch):
    me, h = await make_user(db, username="amina")
    await make_user(db, username="kofi", expo_push_tokens=["ExponentPushToken[k]"])
    calls["pushes"].clear()
    assert (await _post(client, h, description="@kofi")).status_code == 201
    await _settle()
    assert calls["pushes"] == []
    await _on(db)
    from app.services import mentions

    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(mentions, "by_username", boom)
    assert (await _post(client, h, description="@kofi")).status_code == 201
