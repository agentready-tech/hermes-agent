"""Managed BotMode profiles own disjoint data and in-process routing lanes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from hermes_runtime.adapters import hermes


@pytest.fixture
def shared_homes(tmp_path, monkeypatch):
    root = tmp_path / '.hermes'
    homes = {name: root / 'profiles' / name for name in ('alpha', 'bravo')}
    for name, home in homes.items():
        (home / 'workspace').mkdir(parents=True)
        (home / 'config.yaml').write_text('model:\n  default: ' + name + '-model\n')
        (home / '.env').write_text('ISOLATION_TOKEN=' + name + '-private-fixture\n')
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(hermes, '_hosted_homes', frozenset())
    hermes.activate_profiles(homes.values())
    return root, homes


def test_parallel_scopes_keep_home_keys_model_and_secrets_separate(shared_homes):
    from gateway.run import _profile_runtime_scope
    from agent.secret_scope import get_secret
    from hermes_cli.config import load_config
    from hermes_runtime.sessions import home_session_key

    _, homes = shared_homes
    barrier = threading.Barrier(2)

    def exercise(name):
        with _profile_runtime_scope(homes[name]):
            hermes.activate(homes[name])
            barrier.wait(timeout=20)
            for _ in range(50):
                assert hermes.enabled()
                assert hermes.managed_home() == homes[name]
                assert get_secret('ISOLATION_TOKEN') == name + '-private-fixture'
                assert load_config()['model']['default'] == name + '-model'
                assert home_session_key() == 'agent:' + name + ':agentready:home'
            return home_session_key()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(exercise, homes))
    assert first != second
    assert not hermes.enabled()
    with pytest.raises(RuntimeError, match='No owned managed profile'):
        hermes.managed_home()


def test_both_profiles_keep_the_same_logical_home_id_in_their_own_sqlite(shared_homes):
    from gateway.run import _profile_runtime_scope
    from hermes_runtime.adapters.state import ensure_home_session, current_home_session_id, state_db_path
    from hermes_state import SessionDB

    _, homes = shared_homes
    for name, home in homes.items():
        with _profile_runtime_scope(home):
            ensure_home_session()
            assert current_home_session_id() == 'agentready_home'
            assert state_db_path() == home / 'state.db'
            db = SessionDB()
            db.append_message('agentready_home', 'user', name + '-private-message')
            db.close()
    for name, home in homes.items():
        with _profile_runtime_scope(home):
            db = SessionDB()
            rows = db.get_messages('agentready_home')
            assert [row['content'] for row in rows] == [name + '-private-message']
            db.close()


def test_unregistered_home_cannot_activate_or_borrow_a_host_profile(shared_homes, tmp_path):
    from gateway.run import _profile_runtime_scope

    foreign = tmp_path / 'foreign'; foreign.mkdir()
    with pytest.raises(RuntimeError, match='not owned'):
        hermes.activate(foreign)
    with _profile_runtime_scope(foreign):
        assert not hermes.enabled()
        with pytest.raises(RuntimeError, match='No owned managed profile'):
            hermes.managed_home()


def test_legacy_single_runtime_keeps_its_existing_home_key(tmp_path, monkeypatch):
    from hermes_runtime.sessions import HOME_SESSION_KEY, home_session_key

    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(hermes, '_hosted_homes', frozenset())
    hermes.activate(tmp_path)
    assert home_session_key() == HOME_SESSION_KEY
    with pytest.raises(RuntimeError, match='exactly one'):
        hermes.activate(tmp_path / 'different')


@pytest.mark.parametrize('name', ['alpha', 'bravo'])
def test_adapter_completion_route_matches_botmode_store(shared_homes, name):
    from types import SimpleNamespace
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionSource, SessionStore
    from gateway.session_identity import RoutingIdentity

    root, homes = shared_homes
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-' + name,
                           user_id='9001', profile=name)
    adapter = SimpleNamespace(config=PlatformConfig(), _canonicalize=lambda src: None,
                              _session_key_profile=lambda src: src.profile)
    store = SessionStore(root / 'sessions', GatewayConfig(multiplex_profiles=True))
    try:
        with _profile_runtime_scope(homes[name]):
            route = store._generate_session_key(source)
            assert route == 'agent:' + name + ':agentready:home'
            assert BasePlatformAdapter._source_session_key(adapter, source) == route
        # Real background ingress starts outside a turn's profile scope.
        source._identity = RoutingIdentity(transport_profile=name, runtime_profile=name,
            authorization_home=homes[name], runtime_home=homes[name])
        assert BasePlatformAdapter._source_session_key(adapter, source) == route
        source._identity = RoutingIdentity(transport_profile='foreign', runtime_profile='foreign',
            authorization_home=root / 'foreign', runtime_home=root / 'foreign')
        assert ':agentready:home' not in BasePlatformAdapter._source_session_key(adapter, source)
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_panel_only_main_binds_each_profile_on_recovery(shared_homes, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import _build_profile_authority
    from gateway.session_authorities import SessionAuthorities
    from hermes_runtime.adapters.main_transport import authorize_source
    from hermes_runtime.adapters.state import ensure_home_session

    root, homes = shared_homes
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=root / 'sessions'))
    runner._primary_profile_name = 'default'
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_runtime_descriptor = {'instance_id': 'shared-panel-fixture'}
    try:
        sources = {}
        for name, home in homes.items():
            with _profile_runtime_scope(home):
                ensure_home_session()
            authority = await _build_profile_authority(runner, name, home, register=False)
            live = authority.sessions['agentready_home']
            assert live.route == 'agent:' + name + ':agentready:home'
            assert live.source.profile == name
            sources[name] = live.source
            with _profile_runtime_scope(home):
                assert authorize_source(runner, live.source)
                assert runner.session_store.lookup_by_session_key(live.route).origin == live.source
        assert sources['alpha'] != sources['bravo']
    finally:
        runner.session_store.close_all_db_handles()
