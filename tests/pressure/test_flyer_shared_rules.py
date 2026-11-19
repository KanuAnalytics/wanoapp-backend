"""Flyers and TV consume one slot and must receive identical Admin rules."""
import pytest
from app.services import flyers, tv_strip

pytestmark = pytest.mark.asyncio

@pytest.mark.parametrize('viewer', [None, 'invalid'])
async def test_flyers_use_current_shared_strip_rules(db, viewer):
    configured = {'min_posts': 1, 'gap_posts': 3, 'max_per_session': 5,
                  'show_seconds': 12, 'after_portion': 0.4}
    await tv_strip.set_rules(db, configured)
    result = await flyers.for_viewer(db, viewer)
    assert result['rules'] == await tv_strip.rules(db)
    for key, value in configured.items():
        assert result['rules'][key] == value

async def test_default_rules_stay_shared_without_an_admin_override(db):
    result = await flyers.for_viewer(db, None)
    assert result['rules'] == await tv_strip.rules(db) == tv_strip.RULES
