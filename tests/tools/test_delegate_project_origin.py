"""Persistent completion routes survive the tool call and profile handoff."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('name', ['alpha', 'bravo'])
def test_push_project_captures_routable_parent_not_api_wake_id(name):
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.delegate_projects import _dispatch_origin

    route = 'agent:' + name + ':agentready:home'
    token = set_current_session_key(route)
    tokens = set_session_vars(platform='telegram', chat_id='fixture-' + name,
                              session_key=route, session_id='agentready_home', async_delivery=True)
    try:
        origin = _dispatch_origin(SimpleNamespace(session_id='agentready_home'), True)
        assert origin['wake'] is True
        assert origin['session_key'] == route
        assert not origin.get('mail_results')
    finally:
        clear_session_vars(tokens)
        reset_current_session_key(token)


def test_finite_project_keeps_inline_delivery_policy():
    from gateway.session_context import clear_session_vars, set_session_vars
    from gateway.session_finite import finite_turn_scope
    from tools.delegate_projects import _dispatch_origin

    tokens = set_session_vars(platform='telegram', async_delivery=True)
    try:
        with finite_turn_scope(True, False):
            origin = _dispatch_origin(SimpleNamespace(session_id='agentready_home'), True)
        assert origin['wake'] is False
        assert origin['mail_results'] is True
    finally:
        clear_session_vars(tokens)
