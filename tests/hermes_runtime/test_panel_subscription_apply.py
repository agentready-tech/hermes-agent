"""Panel saves drive the existing main owner's next-turn route, never a second session."""
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from tests.hermes_runtime.test_panel_main_bootstrap import runtime

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.mark.asyncio
async def test_subscription_save_changes_next_main_route_without_mutating_history(main_home, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from hermes_runtime.standalone.panel_subscriptions import Subscriptions
    from hermes_runtime.adapters.main_model import resolve_turn_runtime
    from websockets.asyncio.server import serve

    home, db = main_home
    managed = {'provider': 'custom', 'default': 'managed-fixture',
               'base_url': 'https://panel.invalid/app/api/agents/own/inference/v1',
               'key_env': 'AGENTSEE_INFERENCE_KEY', 'api_mode': 'chat_completions'}
    config = {'model': managed, 'terminal': {'backend': 'docker'},
              'autonomous': {'panel': {'url': 'https://panel.invalid', 'environment_id': 'own'}},
              'agent': {'continuation_guard': {'enabled': True}}}
    (home / 'config.yaml').write_text(json.dumps(config))
    (home / '.env').write_text('AGENTSEE_INFERENCE_KEY=fixture-private-key\n')
    credentials = (home / '.env').read_bytes()
    history = db.get_messages(HOME_SESSION_ID)
    authority, owner = runtime(home, db, monkeypatch)
    assert 'result' in await owner.dispatch({'id': 0, 'method': 'session.resume',
                                           'params': {'session_id': HOME_SESSION_ID}})
    received = []

    async def native(socket):
        async for raw in socket:
            request = json.loads(raw)
            received.append(request)
            await socket.send(json.dumps(await owner.dispatch(request)))

    class Auth:
        gateway_owned = True

        def rpc_connection(self):
            return target, {}

    value = Subscriptions(home, Auth())
    monkeypatch.setattr(value, '_request', lambda _path: {
        'providers': [{'id': 'openai-codex', 'status': {'logged_in': True}}]})
    monkeypatch.setattr(value, '_claude', lambda: {'available': False, 'logged_in': False})
    runner = run.GatewayRunner(GatewayConfig())
    runner.session_store = authority.runner.session_store
    source = authority.sessions[HOME_SESSION_ID].source

    def native_credentials():
        # Resolve only fixture credentials. The production route/config and cache-key
        # code remain real; no subscription or inference provider is contacted.
        route = value._config()['model']
        return {'provider': route['provider'], 'base_url': route.get('base_url', ''),
                'api_key': 'fixture-private-key', 'api_mode': route.get('api_mode', 'codex_responses')}

    monkeypatch.setattr(run, '_resolve_runtime_agent_kwargs', native_credentials)

    def next_route():
        route = resolve_turn_runtime(runner, source, HOME_SESSION_KEY,
                                     run._load_gateway_config(home / 'config.yaml'))
        return route, runner._agent_config_signature(*route, [], '')

    before, old_signature = next_route()
    try:
        async with serve(native, '127.0.0.1', 0) as server:
            target = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/ws'
            stale = {'model': 'previous-session-model', 'provider': 'custom', 'api_key': 'fixture-key'}
            runner.session_store.set_model_override(HOME_SESSION_KEY, stale)
            runner._session_model_overrides[HOME_SESSION_KEY] = stale
            assert await value.select('openai', 'fixture-codex-next', True) == {'saved': True, 'deferred': True}
            assert runner._session_model_overrides[HOME_SESSION_KEY] == stale, 'finish the current reply first'
            selected, new_signature = next_route()
            assert selected[0] == 'fixture-codex-next'
            assert selected[1]['provider'] == 'openai-codex'
            assert selected[1]['base_url'] == ''
            assert new_signature != old_signature, 'the old cached agent must not serve the next route'
            assert runner.session_store.get_model_override(HOME_SESSION_KEY) is None
            # A later genuine /model remains authoritative after the receipt was consumed,
            # including when a fresh owner rehydrates the routing index after restart.
            later = {**stale, 'model': 'later-explicit-model'}
            runner.session_store.set_model_override(HOME_SESSION_KEY, later)
            from gateway.session import SessionStore
            restarted = SessionStore(home / 'sessions', runner.config)
            runner.session_store = restarted
            runner._session_model_overrides.pop(HOME_SESSION_KEY, None)
            monkeypatch.setattr(run, '_resolve_runtime_agent_kwargs_for_provider', lambda *_a, **_k: {
                'provider': 'custom', 'api_key': 'fixture-key', 'base_url': '', 'api_mode': 'chat_completions'})
            assert next_route()[0][0] == 'later-explicit-model'
            assert await value.select('managed', None, False) == {'saved': True, 'deferred': True}
            restored, restored_signature = next_route()
            assert restarted.get_model_override(HOME_SESSION_KEY) is None
            # /model also runs outside ordinary turns: it must consume an older pending
            # Panel revision before recording its newer override (including --once).
            from gateway.slash_commands_model import _ModelSwitchContext
            from hermes_cli.model_switch import ModelSwitchResult
            monkeypatch.setattr(runner, '_model_switch_confirmation', AsyncMock(return_value='switched'))
            monkeypatch.setattr(runner, '_record_switch_metrics', lambda *_: None)
            result = ModelSwitchResult(True, new_model='later-command', target_provider='custom',
                                       api_key='fixture-key', api_mode='chat_completions')
            for once in (False, True):
                runner.session_store.set_model_override(HOME_SESSION_KEY, stale)
                runner._session_model_overrides[HOME_SESSION_KEY] = stale
                await value.select('managed', None, False)
                ctx = _ModelSwitchContext(HOME_SESSION_KEY, source, home / 'config.yaml',
                                          False, one_turn=once,
                                          restore_snapshot=runner._snapshot_session_model_override(HOME_SESSION_KEY))
                ctx.read_config()
                assert await runner._commit_model_switch_locked(result, ctx, source=source, picker=False) == 'switched'
                assert next_route()[0][0] == 'later-command'
                if once:
                    snapshot = runner._pending_one_turn_model_restores.pop(HOME_SESSION_KEY)
                    runner._restore_session_model_override(HOME_SESSION_KEY, snapshot)
                    assert next_route()[0] == before
            # A Panel save made after a command prepared is newer than that command.
            ctx = _ModelSwitchContext(HOME_SESSION_KEY, source, home / 'config.yaml', False)
            ctx.read_config()
            await value.select('managed', None, False)
            await runner._commit_model_switch_locked(result, ctx, source=source, picker=False)
            assert next_route()[0] == before
            restarted.close_all_db_handles()
        assert restored == before and restored_signature == old_signature
        assert value._config()['model'] == managed
        assert value._config()['terminal'] == config['terminal']
        assert value._config()['agent'] == config['agent']
        assert (home / '.env').read_bytes() == credentials
        assert db.get_messages(HOME_SESSION_ID) == history
        assert set(authority.sessions) == {HOME_SESSION_ID}
        assert received == [], 'main settings must not use legacy config.set or local-session mutations'
    finally:
        await owner.close()
        authority.runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pending_selection_is_scoped_and_failed_persistence_is_not_published(main_home, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from hermes_runtime.adapters.main_model import apply_selection
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY

    home, db = main_home
    authority, owner = runtime(home, db, monkeypatch)
    assert 'result' in await owner.dispatch({'id': 0, 'method': 'session.resume',
                                           'params': {'session_id': HOME_SESSION_ID}})
    runner = run.GatewayRunner(GatewayConfig())
    runner.session_store = authority.runner.session_store
    source = authority.sessions[HOME_SESSION_ID].source
    stale = {'model': 'keep-old', 'provider': 'custom', 'api_key': 'fixture-key'}
    runner.session_store.set_model_override(HOME_SESSION_KEY, stale)
    runner._session_model_overrides[HOME_SESSION_KEY] = stale
    evictions = []
    monkeypatch.setattr(runner, '_evict_cached_agent', evictions.append)
    revision = 'a' * 32
    entry = runner.session_store._entries[HOME_SESSION_KEY]
    before = entry.to_dict()
    try:
        assert not apply_selection(runner, source, 'agent:child', revision)
        assert not apply_selection(runner, replace(source, profile='foreign-profile'), HOME_SESSION_KEY, revision)
        assert not apply_selection(runner, source, HOME_SESSION_KEY, 'invalid')
        from hermes_runtime.adapters import hermes
        with monkeypatch.context() as patch:
            patch.setattr(hermes, '_home', None)
            assert not apply_selection(runner, source, HOME_SESSION_KEY, revision)
        with monkeypatch.context() as patch:
            def fail(*_args):
                raise OSError('fixture routing persistence failure')
            patch.setattr(runner.session_store, '_persist_routing_data', fail)
            with pytest.raises(OSError, match='fixture routing persistence failure'):
                apply_selection(runner, source, HOME_SESSION_KEY, revision)
        assert entry.to_dict() == before
        assert runner._session_model_overrides[HOME_SESSION_KEY] == stale
        assert not evictions
        assert apply_selection(runner, source, HOME_SESSION_KEY, revision)
        assert not apply_selection(runner, source, HOME_SESSION_KEY, revision)
        assert evictions == [HOME_SESSION_KEY]
    finally:
        await owner.close()
        runner.session_store.close_all_db_handles()
