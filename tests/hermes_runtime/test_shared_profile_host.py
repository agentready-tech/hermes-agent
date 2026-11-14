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
    monkeypatch.setattr(hermes, '_shared_root', None)
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
    monkeypatch.setattr(hermes, '_shared_root', None)
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
        assert runner.session_store._db_for_session_id('agentready_home') is None
        for name, home in homes.items():
            with _profile_runtime_scope(home):
                assert runner.session_store._db_for_session_id('agentready_home').db_path == home / 'state.db'
                assert runner.session_store._owner_key_for_session_id('agentready_home') == f'agent:{name}:agentready:home'
        assert sources['alpha'] != sources['bravo']
    finally:
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_shared_gateway_polls_only_enabled_mail_and_admits_to_own_profile(shared_homes, monkeypatch):
    import base64
    import json
    import sqlite3
    from types import SimpleNamespace

    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import _build_profile_authority, _stop_profile_mail, publish_gateway_runtime_ready
    from gateway.runtime_ownership import ProfileOwnership
    from gateway.session_authorities import SessionAuthorities
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.workspace.service import Workspace
    from tests.hermes_runtime.test_workspace_native import SECRET, connection
    from tests.waiting import async_wait_until

    root, homes = shared_homes
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    ownership = ProfileOwnership()
    monkeypatch.setattr('gateway.runtime_ownership.process_ownership', ownership)
    ownership.reserve(homes.values())
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=root / 'sessions'))
    runner._primary_profile_name = 'default'
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_runtime_descriptor = {'instance_id': 'shared-mail-fixture'}
    runner.session_api = SimpleNamespace(task=SimpleNamespace(done=lambda: False))
    runner._running = True
    requests = []
    disabled_checked = threading.Event()
    from hermes_runtime.standalone.workspace import poll

    def track_poll(home, *args, **kwargs):
        result = poll(home, *args, **kwargs)
        if home == homes['bravo']:
            disabled_checked.set()
        return result

    def fetch(_home, program, request):
        requests.append(request['credential']['username'])
        assert program == 'sandbox_mail' and request['operation'] == 'incoming'
        return {'messages': [{'uid': 27, 'format': 'text-v1', 'attachments': [],
            'internalDate': '06-Nov-2026 08:00:00 +0000',
            'raw': base64.b64encode(b'From: lead@example.invalid\r\nSubject: Synthetic\r\n\r\nBody').decode()}],
            'cursor': {'validity': '7', 'uid': 27}}

    monkeypatch.setattr('hermes_runtime.workspace.transport.poll_request', fetch)
    monkeypatch.setattr('hermes_runtime.standalone.workspace.poll', track_poll)
    monkeypatch.setattr('tools.terminal_tool.ensure_task_env', lambda *_: pytest.fail('Mail must not access the sandbox'))
    authorities = {}
    try:
        for name, home in homes.items():
            (home / 'config.yaml').write_text('autonomous:\n  workspace:\n    native: true\n')
            Workspace(home).store.upsert(connection(proactive=name == 'alpha', senderPolicy='all'),
                                         {**SECRET, 'username': name + '@example.invalid'})
            with _profile_runtime_scope(home):
                ensure_home_session()
            authorities[name] = await _build_profile_authority(runner, name, home, register=False)
        publish_gateway_runtime_ready(runner)
        alpha_cursor = homes['alpha'] / 'autonomous' / 'workspace-cursors.json'
        await async_wait_until(lambda: alpha_cursor.is_file() and disabled_checked.is_set(), timeout=10)
        for authority in authorities.values():
            assert getattr(authority, '_workspace_mail', None) is not None
            assert await _stop_profile_mail(authority, 5)
        assert requests == ['alpha@example.invalid']
        with sqlite3.connect(homes['alpha'] / 'state.db') as con:
            event, = con.execute('SELECT event_json FROM async_delegations').fetchall()
        event = json.loads(event[0])
        assert event['session_key'] == 'agent:alpha:agentready:home'
        scheduled = []
        monkeypatch.setattr(authorities['alpha'], '_schedule', scheduled.append)
        from gateway.run import _format_gateway_process_notification
        assert await runner._deliver_completion_notification(_format_gateway_process_notification(event), event) is True
        with sqlite3.connect(homes['alpha'] / 'state.db') as con:
            assert con.execute("SELECT status FROM session_admissions WHERE principal_id LIKE 'automation:%'").fetchall() == [('queued',)]
        assert [(ref.profile_id, ref.session_id) for ref in scheduled] == [(str(homes['alpha']), 'agentready_home')]
        with sqlite3.connect(homes['bravo'] / 'state.db') as con:
            assert con.execute('SELECT count(*) FROM session_admissions').fetchone() == (0,)
        assert not (homes['bravo'] / 'autonomous' / 'workspace-cursors.json').exists()
    finally:
        for authority in authorities.values():
            await _stop_profile_mail(authority, 5)
        runner.session_store.close_all_db_handles()
        ownership.close()


@pytest.mark.asyncio
async def test_shared_profile_host_bootstraps_newly_reserved_home(tmp_path, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import _build_profile_authority
    from gateway.runtime_ownership import ProfileOwnership
    from gateway.session_authorities import SessionAuthorities
    from hermes_runtime.adapters.state import current_home_session_id

    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(hermes, '_home', None)
    monkeypatch.setattr(hermes, '_hosted_homes', frozenset())
    monkeypatch.setattr(hermes, '_shared_root', None)
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    ownership = ProfileOwnership()
    monkeypatch.setattr('gateway.runtime_ownership.process_ownership', ownership)
    hermes.activate_profile_host(root)
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=root / 'sessions'))
    runner._primary_profile_name = 'default'
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_runtime_descriptor = {'instance_id': 'shared-hot-add-fixture'}
    try:
        for name in ('alpha', 'bravo'):
            home = root / 'profiles' / name
            home.mkdir(parents=True)
            (home / 'config.yaml').write_text('model: {}\n')
            assert not hermes.owns_home(home)
            ownership.reserve([home])
            assert hermes.owns_home(home) and not hermes.owns_home(root)
            authority = await _build_profile_authority(runner, name, home, register=False)
            assert authority.sessions['agentready_home'].route == f'agent:{name}:agentready:home'
            with _profile_runtime_scope(home):
                assert current_home_session_id() == 'agentready_home'
        assert runner.session_authorities.for_home(root / 'profiles' / 'alpha') is not authority
    finally:
        runner.session_store.close_all_db_handles()
        ownership.close()


@pytest.mark.asyncio
async def test_profile_panel_connector_uses_own_home_and_credential(shared_homes, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from agent.secret_scope import get_secret
    from gateway.run_runtime import _start_profile_panel, _stop_profile_panel
    from hermes_runtime.standalone import panel

    _, homes = shared_homes
    runner = SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True), _draining=False,
                             session_runtime_descriptor={'state': 'ready'})
    authorities = [SimpleNamespace(profile_id=str(home), runner=runner) for home in homes.values()]
    seen = {}
    connected = asyncio.Event()
    blocking = asyncio.Event()

    async def worker(home):
        seen[home.name] = hermes.managed_home(), get_secret('HERMES_PANEL_CREDENTIAL')
        if len(seen) == len(homes):
            connected.set()
        await blocking.wait()

    monkeypatch.setattr(panel, 'worker', worker)
    for name, home in homes.items():
        (home / 'config.yaml').write_text(f'autonomous:\n  panel:\n    url: https://fixture.invalid\n    environment_id: {name}\n')
        (home / '.env').write_text(f'HERMES_PANEL_CREDENTIAL={name}-fixture\n')
    try:
        for authority in authorities:
            _start_profile_panel(authority)
        await asyncio.wait_for(connected.wait(), timeout=5)
        assert seen == {name: (home, name + '-fixture') for name, home in homes.items()}
    finally:
        for authority in authorities:
            await _stop_profile_panel(authority)


@pytest.mark.asyncio
async def test_shared_gateway_keeps_existing_transport_and_legacy_route(shared_homes, monkeypatch):
    from datetime import datetime, timezone
    import json

    import hermes_state
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from gateway.run_runtime import _build_profile_authority
    from gateway.session import SessionEntry, SessionSource
    from gateway.session_authorities import SessionAuthorities
    from hermes_runtime.adapters.state import ensure_home_session
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY

    root, homes = shared_homes
    home = homes['alpha']
    monkeypatch.setattr(hermes_state, 'DEFAULT_DB_PATH', hermes_state._IMPORT_DEFAULT_DB_PATH)
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=root / 'sessions'))
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_runtime_descriptor = {'instance_id': 'shared-legacy-fixture'}
    timestamp = datetime.now(timezone.utc)
    origin = SessionSource(platform=Platform.TELEGRAM, chat_id='42', chat_type='dm', user_id='42')
    existing = SessionEntry(HOME_SESSION_KEY, HOME_SESSION_ID, timestamp, timestamp,
                            origin=origin, platform=origin.platform, display_name='Existing main')
    legacy = json.dumps(existing.to_dict())
    try:
        with _profile_runtime_scope(home):
            ensure_home_session()
            db = runner.session_store._db
            db.save_gateway_routing_entry(HOME_SESSION_KEY, legacy, scope='/root/.hermes/sessions')
        authority = await _build_profile_authority(runner, 'alpha', home, register=False)
        from hermes_runtime.adapters.main_transport import bootstrap_main
        with _profile_runtime_scope(home):
            entry = bootstrap_main(authority)
            assert entry.session_key == 'agent:alpha:agentready:home'
            assert entry.session_id == HOME_SESSION_ID
            assert entry.origin.to_dict() == {**origin.to_dict(), 'profile': 'alpha'}
            assert entry.display_name == 'Existing main'
            assert authority.db.load_gateway_routing_entries(scope='/root/.hermes/sessions') == {HOME_SESSION_KEY: legacy}
    finally:
        runner.session_store.close_all_db_handles()
