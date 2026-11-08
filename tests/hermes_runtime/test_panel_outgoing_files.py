"""Synthetic native main file delivery. No providers, browser or model are contacted."""
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest


class RemoteFiles:
    """Isolated backend filesystem; actual generic extraction is used, never a host-path read."""
    cwd = '/workspace'

    def __init__(self, root):
        self.root = root
        self.calls = []
        self.fail = False

    def execute(self, command, **kwargs):
        self.calls.append(command)
        if self.fail or self.cwd not in command:
            return {'returncode': 1, 'output': ''}
        # Only this fake transport maps its own virtual namespace. Production uses backend.execute.
        command = command.replace(self.cwd, str(self.root))
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True, timeout=5, check=False)
        return {'returncode': result.returncode, 'output': result.stdout}

    def fetch_file(self, path, target, **kwargs):
        from tools.environments.base import BaseEnvironment
        return BaseEnvironment.fetch_file(self, path, target, **kwargs)


@pytest.fixture
def files(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from gateway import media_delivery as main_file_source
    home = tmp_path / 'own-home'; home.mkdir()
    workspace = tmp_path / 'remote-only'; workspace.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    environment = RemoteFiles(workspace)
    calls = []
    def producing(selected, route, task_id=None):
        assert selected == home and route == 'own-producing-route'
        calls.append((selected, route))
        return nullcontext((environment, '/workspace'))
    original_source = main_file_source.producing_environment
    monkeypatch.setattr(main_file_source, 'producing_environment', producing)
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('own-main', source='agentready')
    db.create_session('foreign', source='agentready')
    yield SimpleNamespace(home=home, workspace=workspace, environment=environment, db=db, calls=calls, original_source=original_source)
    db.close()


def rows(files, session='own-main'):
    # Use actual native display assembler; content/API history remain separate.
    from gateway.session_controls import display_projection
    return display_projection(files.db.get_resume_conversations(session)[1], str(files.home))


def retain(files, names=('report.csv',)):
    from hermes_runtime.standalone.main_files import retain_row
    content = 'Synthetic files\n' + '\n'.join('MEDIA:/workspace/' + name for name in names)
    row_id = files.db.append_message('own-main', 'assistant', content, api_content=content,
                                    display_metadata={'keep': 'existing'})
    marker = retain_row(files.db, 'own-main', row_id, files.home, 'own-producing-route')
    return row_id, content, marker


def test_remote_csv_pdf_are_retained_and_replayed_without_host_fallback(files, tmp_path, monkeypatch):
    from hermes_runtime.standalone.main_files import download, public_files, retain_row
    from hermes_runtime.standalone.panel_chat import MainChat
    from hermes_runtime.adapters import hermes
    bodies = {'report.csv': b'item,count\nsynthetic,2\n', 'report.pdf': b'%PDF-1.4\nsynthetic-only\n%%EOF\n'}
    for name, data in bodies.items(): (files.workspace / name).write_bytes(data)
    rid, original, marker = retain(files, tuple(bodies))
    assert len(marker['files']) == 2 and 'error' not in marker
    monkeypatch.setattr(hermes, 'managed_home', lambda: files.home)
    class Native:
        async def rpc(self, method, **params):
            assert method == 'session.resume' and params == {'session_id': 'agentready_home', 'display_projection': True}
            return {'session_id': 'agentready_home', 'execution_generation': 3, 'messages': [],
                    'display_messages': rows(files)}
    async def exercise():
        chat = MainChat(Native())
        snap = await chat.snapshot()
        row = snap['messages'][0]
        assert row['attachments'] == public_files(marker, rid)
        assert 'MEDIA:' not in row['text'] and 'reference' not in json.dumps(snap)
        assert 'display_messages' not in snap and str(files.home) not in json.dumps(snap)
        for descriptor in row['attachments']:
            result = await chat.dispatch({'method': 'panel.attachment.download', 'params': {'session_id': 'agentready_home', 'id': descriptor['id']}})
            assert base64.b64decode(result['data_base64']) == bodies[descriptor['name']]
        return snap
    snapshot = asyncio.run(exercise())
    # Reconnect yields identical handles; overwriting originals cannot change published bytes.
    for name in bodies: (files.workspace / name).write_bytes(b'overwritten original')
    calls = len(files.environment.calls)
    assert retain_row(files.db, 'own-main', rid, files.home, 'own-producing-route') == marker
    assert len(files.environment.calls) == calls
    assert asyncio.run(exercise()) == snapshot
    with files.db._read_ctx() as conn:
        content, api_content = conn.execute('SELECT content,api_content FROM messages WHERE id=?', (rid,)).fetchone()
    assert content == original and api_content == original
    # Native producer → Node consumer test consumes these synthetic bytes, never private history.
    output = files.home.parent / 'native-wire.json'
    output.write_text(json.dumps({'snapshot': snapshot, 'downloads': [download(rows(files), item['id'], files.home) for item in marker['files']]}))


def _refusal_written_file(files, name, content):
    (files.workspace / name).write_bytes(content)
    return name


def _refusal_link(files, link):
    target = files.workspace / 'real.csv'
    target.write_bytes(b'synthetic')
    path = files.workspace / 'report.csv'
    link(target, path)
    return path.name


def _refusal_oversize(files):
    path = files.workspace / 'report.csv'
    with path.open('wb') as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    return path.name


def _refusal_fifo(files):
    path = files.workspace / 'report.csv'
    os.mkfifo(path)
    return path.name


def _refusal_backend(files):
    files.environment.fail = True
    return 'report.csv'


_REFUSAL_SETUPS = {
    'missing': lambda files: 'report.csv',
    'symlink': lambda files: _refusal_link(files, lambda source, destination: destination.symlink_to(source)),
    'secret': lambda files: _refusal_written_file(files, '.env.csv', b'synthetic-not-secret'),
    'outside': lambda files: '../foreign.csv',
    'oversize': _refusal_oversize,
    'fifo': _refusal_fifo,
    'hardlink': lambda files: _refusal_link(files, os.link),
    'backend-failed': _refusal_backend,
}


@pytest.mark.parametrize('setup', list(_REFUSAL_SETUPS.values()), ids=list(_REFUSAL_SETUPS))
def test_refused_sources_never_publish_a_descriptor(files, setup):
    name = setup(files)
    _, _, marker = retain(files, (name,))
    assert marker['files'] == [] and marker['error']
    assert '/workspace' not in marker['error'] and 'synthetic-not-secret' not in marker['error']


@pytest.mark.parametrize('kind', ['six', 'batch'])
def test_batch_limits_are_atomic(files, kind):
    names = [f'report{i}.csv' for i in range(6 if kind == 'six' else 3)]
    for name in names:
        with (files.workspace / name).open('wb') as stream: stream.truncate(1 if kind == 'six' else 8 * 1024 * 1024)
    _, _, marker = retain(files, names)
    assert marker['files'] == [] and marker['error']


def test_download_rejects_foreign_rows_home_and_modified_retained_bytes(files, tmp_path):
    from hermes_runtime.standalone.main_files import download
    (files.workspace / 'report.csv').write_bytes(b'synthetic')
    _, _, marker = retain(files)
    identity = marker['files'][0]['id']
    with pytest.raises(ValueError): download(rows(files, 'foreign'), identity, files.home)
    with pytest.raises(ValueError): download(rows(files), '/workspace/report.csv', files.home)
    foreign = tmp_path / 'foreign-home'; foreign.mkdir()
    with pytest.raises(ValueError): download(rows(files), identity, foreign)
    Path(marker['files'][0]['reference']['path']).write_bytes(b'changed')
    with pytest.raises(ValueError): download(rows(files), identity, files.home)


def test_wrong_row_receipt_and_raw_download_selectors_are_refused(files, monkeypatch):
    from hermes_runtime.standalone.main_files import retain_row
    from hermes_runtime.standalone.panel_chat import MainChat
    foreign_row = files.db.append_message('foreign', 'assistant', 'MEDIA:/workspace/report.csv')
    with pytest.raises(ValueError): retain_row(files.db, 'own-main', foreign_row, files.home, 'own-producing-route')
    chat = MainChat(None); chat.session_id = 'agentready_home'
    for params in ({'session_id': 'foreign', 'id': 'att-' + 'a'*32},
                   {'session_id': 'agentready_home', 'id': 'att-'+'a'*32, 'path': '/workspace/report.csv'}):
        with pytest.raises(ValueError): asyncio.run(chat.dispatch({'method': 'panel.attachment.download', 'params': params}))


def test_retained_outgoing_files_are_held_by_display_metadata(files):
    from gateway.session_ingress_media import _history_mentions
    (files.workspace / 'report.csv').write_bytes(b'synthetic')
    _, _, marker = retain(files)
    ref = marker['files'][0]['reference']
    with files.db._read_ctx() as conn:
        assert _history_mentions(conn, [ref]) == {ref['path']}


@pytest.mark.parametrize('platform', ['agentready', 'telegram'])
def test_settlement_binds_actual_receipt_before_live_publication_without_holding_stream_lock(files, monkeypatch, platform):
    import threading
    from gateway import session_results, session_settlement_recovery as recovery
    from hermes_runtime.adapters import hermes, state
    from hermes_runtime.adapters.main_transport import register_platform
    from gateway.config import Platform
    register_platform()
    (files.workspace / 'report.csv').write_bytes(b'synthetic live file')
    rid = files.db.append_message('own-main', 'assistant', 'MEDIA:/workspace/report.csv')
    captured = {'result': {'persisted_turn': {'final_assistant_row_id': rid, 'row_ids': [rid]}}}
    stream_lock = threading.RLock(); events = []
    live = SimpleNamespace(source=SimpleNamespace(platform=Platform(platform)), route='own-producing-route',
        settling={}, controls=SimpleNamespace(snapshot=lambda *args: None),
        event_stream=SimpleNamespace(lock=stream_lock, publish=lambda sid, payload: events.append(payload)))
    authority = SimpleNamespace(db=files.db, profile_id=str(files.home), epoch='fixture-epoch', sessions={'own-main': live}, pending_deliveries={})
    ref = SimpleNamespace(session_id='own-main')
    monkeypatch.setattr(hermes, 'enabled', lambda: True)
    monkeypatch.setattr(hermes, 'managed_home', lambda: files.home)
    monkeypatch.setattr(state, 'resolve_home', lambda sid: 'own-main')
    monkeypatch.setattr(session_results, 'prepare_result', lambda *args: None)
    monkeypatch.setattr(session_results, 'finish_result', lambda *args, **kwargs: ({'outcome': 'completed'}, 'MEDIA:/workspace/report.csv'))
    monkeypatch.setattr(recovery, '_release_media', lambda *args: None)
    from gateway import media_delivery as main_file_source
    original = main_file_source.producing_environment
    def producing(home, route, task_id=None):
        assert not stream_lock._is_owned(), 'Backend I/O must never hold the owner event lock'
        return original(home, route, task_id)
    monkeypatch.setattr(main_file_source, 'producing_environment', producing)
    recovery.commit_and_publish(authority, live, ref, {'admission_id': 'fixture', 'request_id': 'fixture'},
        'MEDIA:/workspace/report.csv', 'completed', captured, {})
    assert len(events) == 1 and len(events[0]['attachments']) == 1
    assert events[0]['attachments'][0]['row_id'] == rid
    assert events[0]['attachments'][0]['name'] == 'report.csv'
    assert 'reference' not in json.dumps(events)


def test_model_api_bytes_and_saved_prefix_are_unchanged_by_retaining_a_file(files):
    from agent.turn_context import build_api_messages
    from hermes_runtime.standalone.main_files import retain_row
    (files.workspace / 'report.csv').write_bytes(b'synthetic')
    files.db.update_system_prompt('own-main', 'Saved system prefix')
    rid = files.db.append_message('own-main', 'assistant', 'MEDIA:/workspace/report.csv', api_content='Original exact wire')
    agent = SimpleNamespace(_current_turn_timestamp=1700000000, ephemeral_system_prompt='',
        _copy_reasoning_content_for_api=lambda *args: None, _should_sanitize_tool_calls=lambda: False)
    def payload():
        messages = files.db.get_resume_conversations('own-main')[0]
        return build_api_messages(agent, messages, current_turn_user_idx=None, ext_prefetch_cache=None,
            plugin_user_context=None, moa_config=None, active_system_prompt='Saved system prefix')
    before = payload()
    retain_row(files.db, 'own-main', rid, files.home, 'own-producing-route')
    assert payload() == before
    assert files.db.get_session('own-main')['system_prompt'] == 'Saved system prefix'


def test_remote_same_path_never_reads_the_host_copy_even_when_backend_fails(files, tmp_path, monkeypatch):
    from gateway import media_delivery as main_file_source
    from hermes_runtime.standalone import main_files
    host = tmp_path / 'same-path'; host.mkdir()
    (host / 'report.csv').write_bytes(b'host bytes must never be delivered')
    (files.workspace / 'report.csv').write_bytes(b'own remote-only bytes')
    files.environment.cwd = str(host)
    monkeypatch.setattr(main_file_source, 'producing_environment', lambda home, route, task_id=None: nullcontext((files.environment, str(host))))
    rid = files.db.append_message('own-main', 'assistant', 'MEDIA:' + str(host / 'report.csv'))
    marker = main_files.retain_row(files.db, 'own-main', rid, files.home, 'own-producing-route')
    delivered = main_files.download(rows(files), marker['files'][0]['id'], files.home)
    assert base64.b64decode(delivered['data_base64']) == b'own remote-only bytes'
    files.environment.fail = True
    rid = files.db.append_message('own-main', 'assistant', 'MEDIA:' + str(host / 'report.csv'))
    marker = main_files.retain_row(files.db, 'own-main', rid, files.home, 'own-producing-route')
    assert marker['files'] == [] and marker['error']


def test_backend_resolution_uses_explicit_home_and_actual_session_route(files, tmp_path, monkeypatch):
    import hermes_constants
    from tools import terminal_tool, terminal_tool_lifecycle
    foreign = tmp_path / 'foreign-home'; foreign.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(foreign))
    seen = []
    def config():
        assert hermes_constants.get_hermes_home() == files.home
        return {'env_type': 'kubernetes', 'kubernetes_namespace': 'own', 'kubernetes_pod': 'own-sandbox', 'kubernetes_container': 'sandbox'}
    files.environment.namespace = 'own'
    files.environment.pod = 'own-sandbox'
    files.environment.container = 'sandbox'
    def backend(route):
        assert hermes_constants.get_hermes_home() == files.home
        seen.append(route)
        return files.environment
    monkeypatch.setattr(terminal_tool, '_get_env_config', config)
    monkeypatch.setattr(terminal_tool_lifecycle, 'ensure_task_env', backend)
    with files.original_source(files.home, 'own-producing-route') as selected:
        assert selected == (files.environment, '/workspace')
    assert seen == ['own-producing-route']
    assert hermes_constants.get_hermes_home() == foreign
    monkeypatch.setattr(terminal_tool_lifecycle, 'ensure_task_env', lambda route: None)
    with pytest.raises(ValueError), files.original_source(files.home, 'own-producing-route'):
        pass


@pytest.mark.parametrize('folder', ['workspace', 'cache/scratch', 'cache/images', 'cache/audio'])
def test_explicit_native_artifact_is_authorized_then_staged_under_strict_delivery(files, monkeypatch, folder):
    from gateway import media_delivery, media_policy
    from gateway.platforms import base
    artifact = files.home / folder / 'requested.txt'; artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b'synthetic native artifact')
    monkeypatch.setattr(media_policy, 'media_delivery_strict', lambda: True)
    monkeypatch.setattr(media_policy, 'media_delivery_trust_recent', lambda: False)
    def forbidden_backend(*args, **kwargs):
        raise AssertionError('An exact native source must never switch to the backend')
    monkeypatch.setattr(media_delivery, 'producing_environment', forbidden_backend)
    prepared = media_delivery.prepare_files([str(artifact)], files.home, 'own-producing-route')
    assert prepared[0]['origin']['kind'] == 'native-artifact'
    assert prepared[0]['origin']['home'] == str(files.home)
    ref = prepared[0]['reference']
    assert Path(ref['path']).read_bytes() == b'synthetic native artifact'
    assert str(artifact) != ref['path']
    assert base.validate_media_delivery_path(ref['path']) == ref['path']


@pytest.mark.parametrize('name', ['.env', 'auth.json', 'config.yaml', 'state.db'])
def test_secret_or_history_identity_in_native_scratch_is_denied_before_staging(files, name):
    from gateway.media_delivery import prepare_files
    scratch = files.home / 'cache/scratch'; scratch.mkdir(parents=True)
    path = scratch / name; path.write_bytes(b'synthetic protected identity only')
    with pytest.raises(ValueError): prepare_files([str(path)], files.home, 'own-producing-route')


def test_native_missing_or_symlink_never_changes_to_another_source(files, monkeypatch):
    from gateway import media_delivery
    workspace = files.home / 'workspace'; workspace.mkdir()
    source = workspace / 'missing.txt'
    def forbidden(*args, **kwargs): raise AssertionError('No source fallback')
    monkeypatch.setattr(media_delivery, 'producing_environment', forbidden)
    with pytest.raises((OSError, ValueError)):
        media_delivery.prepare_files([str(source)], files.home, 'own-producing-route')
    target = files.home / 'config.yaml'; target.write_bytes(b'synthetic protected file')
    source.symlink_to(target)
    with pytest.raises(ValueError): media_delivery.prepare_files([str(source)], files.home, 'own-producing-route')


def test_foreign_home_and_generic_denied_path_are_never_read_from_the_host(files, tmp_path):
    from gateway.media_delivery import prepare_files
    foreign = tmp_path / 'foreign-home/workspace'; foreign.mkdir(parents=True)
    path = foreign / 'other.txt'; path.write_bytes(b'foreign synthetic bytes')
    with pytest.raises(ValueError): prepare_files([str(path)], files.home, 'own-producing-route')
    files.environment.calls.clear()
    with pytest.raises(ValueError): prepare_files(['/etc/forbidden.txt'], files.home, 'own-producing-route')
    assert files.environment.calls == []


def test_local_generic_strict_read_policy_is_not_bypassed_by_staging(files, tmp_path, monkeypatch):
    from gateway import media_delivery, media_policy
    arbitrary = tmp_path / 'arbitrary.txt'; arbitrary.write_bytes(b'synthetic non-artifact')
    monkeypatch.setattr(media_delivery, 'producing_environment', lambda *args: nullcontext((None, None)))
    monkeypatch.setattr(media_policy, 'media_delivery_strict', lambda: True)
    monkeypatch.setattr(media_policy, 'media_delivery_trust_recent', lambda: False)
    with pytest.raises(ValueError): media_delivery.prepare_files([str(arbitrary)], files.home, 'own-producing-route')


def test_existing_telegram_adapter_and_panel_read_the_same_retained_bytes(files, monkeypatch):
    from unittest.mock import AsyncMock
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.main_files import retain_row, download
    artifact = files.home / 'cache/scratch/requested.csv'; artifact.parent.mkdir(parents=True)
    original = b'item,count\nsynthetic,2\n'; artifact.write_bytes(original)
    row_id = files.db.append_message('own-main', 'assistant', 'MEDIA:' + str(artifact))
    marker = retain_row(files.db, 'own-main', row_id, files.home, 'own-producing-route')
    ref = marker['files'][0]['reference']
    monkeypatch.setattr(hermes, 'enabled', lambda: True)
    monkeypatch.setattr(hermes, 'managed_home', lambda: files.home)
    paths = BasePlatformAdapter.filter_media_delivery_paths([(ref['path'], False)], session_key='own-producing-route')
    assert paths == [(ref['path'], False)]
    artifact.write_bytes(b'overwritten source')
    uploaded = []
    async def provider_boundary(**kwargs):
        uploaded.append(kwargs['document'].read())
        return SimpleNamespace(message_id=99)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='synthetic-token'))
    adapter._bot = AsyncMock()
    adapter._bot.send_document.side_effect = provider_boundary
    result = asyncio.run(adapter.send_document('12345', paths[0][0]))
    assert result.success and uploaded == [original]
    panel = download(rows(files), marker['files'][0]['id'], files.home)
    assert base64.b64decode(panel['data_base64']) == uploaded[0]
    assert panel['sha256'] == ref['sha256']



def test_active_scoped_media_cache_roots_are_real_and_deduplicated(files, tmp_path, monkeypatch):
    from gateway.media_delivery import own_home
    from gateway.platforms import base
    registered = tmp_path / 'registered-profile'
    monkeypatch.setattr(base, '_profile_dirs', lambda: [files.home, registered, files.home])
    with own_home(files.home):
        roots = base._profile_cache_roots()
    assert roots.count(files.home / 'cache/documents') == 1
    assert roots.count(files.home / base.CONNECTOR_DOWNLOADS_SUBDIR) == 1
    assert registered / 'cache/documents' in roots
    assert files.home / 'cache' not in roots
    assert files.home / 'cache/scratch' not in roots
    assert files.home / 'workspace' not in roots


def test_panel_limit_does_not_suppress_telegram_25_mib_preparation(files, monkeypatch):
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter
    from hermes_runtime.adapters import hermes, state
    from hermes_runtime.standalone.main_files import settle_files
    artifact = files.home / 'workspace/large.pdf'
    artifact.parent.mkdir()
    with artifact.open('wb') as stream:
        stream.write(b'%PDF-1.4 synthetic transfer fixture only\n')
        stream.truncate(25 * 1024 * 1024)
    with artifact.open('rb') as stream:
        expected = hashlib.file_digest(stream, 'sha256').hexdigest()
    response = 'Synthetic file\nMEDIA:' + str(artifact)
    row_id = files.db.append_message('own-main', 'assistant', response)
    captured = {'result': {'persisted_turn': {'final_assistant_row_id': row_id}}}
    live = SimpleNamespace(source=SimpleNamespace(platform=Platform.TELEGRAM), route='own-producing-route')
    authority = SimpleNamespace(db=files.db, profile_id=str(files.home), sessions={'own-main': live})
    monkeypatch.setattr(hermes, 'enabled', lambda: True)
    monkeypatch.setattr(hermes, 'managed_home', lambda: files.home)
    monkeypatch.setattr(state, 'resolve_home', lambda sid: 'own-main')
    delivered = settle_files(authority, live, SimpleNamespace(session_id='own-main'),
                             {'outcome': 'completed'}, captured, response)
    assert delivered is None
    assert captured['result']['panel_files']['attachments'] == []
    assert captured['result']['panel_files']['attachment_error']
    prepared = BasePlatformAdapter.filter_media_delivery_paths([(str(artifact), False)],
                                                              session_key='own-producing-route')
    assert len(prepared) == 1
    staged = Path(prepared[0][0])
    assert staged != artifact and staged.stat().st_size == 25 * 1024 * 1024
    artifact.write_bytes(b'overwritten original')
    with staged.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected
