"""Owned persistent delegates survive reconnect; their display never exposes private context."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.hermes_runtime.test_panel_main_bootstrap import runtime, resume

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def project(db, *, parent='agentready_home', key='Report'):
    from tools.delegate_project_store import ProjectStore
    with ProjectStore(SimpleNamespace(_session_db=db, session_id=parent)) as store:
        item = store.create(key, 'Prepare the synthetic report', 'PRIVATE kickoff context')
        message = store.enqueue(item, 'PRIVATE kickoff input', {})
        claimed = store.claim(item)
        db.create_session(item['session_id'], source='delegate', parent_session_id=parent)
        db.append_message(item['session_id'], 'system', 'PRIVATE system prompt')
        db.append_message(item['session_id'], 'user', 'PRIVATE kickoff input')
        db.append_message(item['session_id'], 'assistant', '', reasoning='PRIVATE reasoning')
        db.append_message(item['session_id'], 'tool', 'Synthetic tool result', tool_name='read_file', tool_call_id='fixture-call')
        db.append_message(item['session_id'], 'assistant', 'Synthetic final result', reasoning='PRIVATE second thought')
        store.settle(item, claimed, {'status': 'completed', 'summary': 'Synthetic final result', 'session_id': item['session_id']})
    return item, message


async def call(connection, method='list', **params):
    return await connection.dispatch({'id': 9, 'method': 'panel.subagents.' + method, 'params': params})


@pytest.mark.asyncio
async def test_cold_owner_lists_completed_children_and_reads_only_public_actions(main_home, monkeypatch):
    home, db = main_home
    item, _message = project(db)
    before = db.get_messages(item['session_id'])
    for _restart in range(2):
        authority, connection = runtime(home, db, monkeypatch)
        assert 'error' not in await resume(connection)
        listed = await call(connection)
        assert 'error' not in listed, listed
        card = listed['result']['subagents'][0]
        assert card['subagent_id'] == item['subagent_id']
        assert (card['status'], card['last_status']) == ('idle', 'completed')
        history = await call(connection, 'history', subagent_id=item['subagent_id'])
        assert 'error' not in history, history
        assert [row['role'] for row in history['result']['messages']] == ['tool', 'assistant']
        assert [row['text'] for row in history['result']['messages']] == ['Synthetic tool result', 'Synthetic final result']
        assert 'PRIVATE' not in json.dumps([listed, history])
        assert db.get_messages(item['session_id']) == before
        assert len(authority.sessions) == 1


@pytest.mark.asyncio
async def test_foreign_parent_profile_and_missing_native_reader_are_refused(main_home, monkeypatch):
    home, db = main_home
    db.create_session('foreign-parent', source='cli')
    foreign, _ = project(db, parent='foreign-parent')
    _authority, connection = runtime(home, db, monkeypatch)
    assert 'error' in await call(connection)  # Main must first be mounted on this authenticated transport.
    await resume(connection)
    assert not (await call(connection))['result']['subagents']
    response = await call(connection, 'history', subagent_id=foreign['subagent_id'])
    assert response['error']['data']['reason'] == 'not_found'
    for mutation in ('non-native', 'foreign-profile', 'no-read'):
        actor, native = connection.actor, connection.native_owner
        if mutation == 'non-native':
            connection.native_owner = False
        elif mutation == 'foreign-profile':
            connection.actor = replace(actor, profile_id=str(home / 'foreign'))
        else:
            connection.actor = replace(actor, capabilities=frozenset())
        assert 'error' in await call(connection)
        connection.actor, connection.native_owner = actor, native
    for params in ({'session_id': 'foreign-parent'}, {'profile': 'foreign'}, {'path': '/private'}, {'limit': True}):
        assert 'error' in await call(connection, **params)


@pytest.mark.asyncio
async def test_paging_bounds_and_reasoning_secret_filter(main_home, monkeypatch):
    home, db = main_home
    item, _ = project(db)
    second, _ = project(db, key='Second report')
    _authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    first = (await call(connection, limit=1))['result']
    assert first['page']['has_more']
    next_page = (await call(connection, before=first['page']['before'], limit=1))['result']
    assert {first['subagents'][0]['subagent_id'], next_page['subagents'][0]['subagent_id']} == {item['subagent_id'], second['subagent_id']}
    db.append_message(item['session_id'], 'assistant', [{'type': 'thinking', 'text': 'PRIVATE reasoning block'},
                                                       {'type': 'text', 'text': '<think>PRIVATE inline thought</think>Public answer'}])
    db.append_message(item['session_id'], 'tool', 'Authorization: Bearer sk-fixturesecret012345678901234567890123456789', tool_name='http')
    result = (await call(connection, 'history', subagent_id=item['subagent_id'], limit=1))['result']
    assert result['page']['has_more'] and len(result['messages']) == 1
    assert 'sk-fixturesecret' not in json.dumps(result)
    page = (await call(connection, 'history', subagent_id=item['subagent_id'], before=result['page']['before'], limit=1))['result']
    assert page['messages'][0]['text'] == 'Public answer'
    assert 'PRIVATE' not in json.dumps(page)


@pytest.mark.asyncio
async def test_public_invalidation_has_no_text_and_survives_parent_generation_change(main_home, monkeypatch):
    from hermes_runtime.adapters.main_subagents import changed
    home, db = main_home
    authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    seen = []
    live = authority.sessions['agentready_home']
    live.event_stream.observers.add(seen.append)
    turn = SimpleNamespace(_approval_owner=(authority, 'agentready_home', -1))
    for kind in ('subagent.start', 'subagent.tool', 'subagent.complete', 'subagent.changed'):
        changed(turn, kind)
    for kind in ('subagent.thinking', 'subagent.text', 'message.delta'):
        changed(turn, kind)
    changed(SimpleNamespace(_approval_owner=(authority, 'foreign', 1)), 'subagent.tool')
    assert len(seen) == 4
    assert all(frame['params']['payload'] == {} for frame in seen)
    assert all(frame['params']['type'] == 'panel.subagents.changed' for frame in seen)
    monkeypatch.setattr(live.event_stream, 'publish', lambda *_args, **_kw: (_ for _ in ()).throw(RuntimeError('observer failed')))
    changed(turn, 'subagent.complete')  # An observer failure cannot break task completion.


@pytest.mark.asyncio
async def test_reused_old_project_precedes_new_idle_projects(main_home, monkeypatch):
    from tools.delegate_project_store import ProjectStore
    home, db = main_home
    old, _ = project(db, key='Old active project')
    for i in range(21):
        project(db, key=f'New idle project {i}')
    with ProjectStore(SimpleNamespace(_session_db=db, session_id='agentready_home')) as store:
        store.enqueue(old, 'Synthetic next request', {})
    _authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    first = (await call(connection))['result']
    assert first['subagents'][0]['subagent_id'] == old['subagent_id']
    assert first['subagents'][0]['status'] == 'pending'
    second = (await call(connection, before=first['page']['before']))['result']
    assert len({card['subagent_id'] for card in first['subagents'] + second['subagents']}) == 22
    with ProjectStore(SimpleNamespace(_session_db=db, session_id='agentready_home')) as store:
        claimed = store.claim(old)
        store.settle(old, claimed, {'status': 'completed', 'summary': 'New completion'})
    refreshed = (await call(connection, tracked=[old['subagent_id']]))['result']
    assert old['subagent_id'] not in {card['subagent_id'] for card in refreshed['subagents']}
    assert refreshed['tracked'][0]['status'] == 'idle'
    assert refreshed['tracked'][0]['last_status'] == 'completed'


@pytest.mark.asyncio
async def test_compaction_plain_brackets_and_pending_tool_invocations(main_home, monkeypatch):
    home, db = main_home
    item, _ = project(db)
    sid = item['session_id']
    _authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    initial = (await call(connection, 'history', subagent_id=item['subagent_id']))['result']
    final_identity = next(row['display_id'] for row in initial['messages'] if row['text'] == 'Synthetic final result')
    db.append_message(sid, 'tool', '[INFO] a literal log line', tool_name='terminal')
    for _epoch in range(2):
        history = db.get_messages_as_conversation(sid)
        db.archive_and_compact(sid, [{'role': 'user', 'content': 'PRIVATE compressed context'}] + history[-2:], tail_count=2)
    db.append_message(sid, 'assistant', 'PRIVATE tool-call commentary', tool_calls=[{
        'id': 'pending-tool', 'type': 'function', 'function': {'name': 'browser_exec', 'arguments': '{"password":"PRIVATE"}'}}])
    db.append_message(sid, 'tool', 'large output ' * 10000, tool_name='terminal')
    db.append_message(sid, 'assistant', 'PRIVATE oversized invocation commentary', tool_calls=[{
        'id': 'oversized-tool', 'type': 'function', 'function': {'name': 'browser_exec', 'arguments': 'x' * 100000}}])
    result = (await call(connection, 'history', subagent_id=item['subagent_id']))['result']
    texts = [row['text'] for row in result['messages']]
    assert texts.count('[INFO] a literal log line') == 1
    assert texts.count('Synthetic final result') == 1
    assert next(row['display_id'] for row in result['messages'] if row['text'] == 'Synthetic final result') == final_identity
    assert 'PRIVATE' not in json.dumps(result)
    assert any(row.get('tool_call_id') == 'pending-tool' and row['status'] == 'started' for row in result['messages'])
    assert any(row['truncated'] for row in result['messages'])
    assert len(json.dumps(result).encode()) < 181000


@pytest.mark.asyncio
async def test_main_and_child_compression_keep_ownership_and_child_history(main_home, monkeypatch):
    home, db = main_home
    item, _ = project(db)
    db.publish_compression_child(parent_session_id='agentready_home', child_session_id='main-tip',
        source='agentready', messages=[{'role': 'user', 'content': 'PRIVATE main summary'}], require_compression_lease=False)
    tail = db.get_messages(item['session_id'])[-1]
    db.publish_compression_child(parent_session_id=item['session_id'], child_session_id='child-tip',
        source='delegate', messages=[{'role': 'user', 'content': 'PRIVATE child summary'}, tail], require_compression_lease=False)
    db.append_message('child-tip', 'assistant', 'New child final')
    _authority, connection = runtime(home, db, monkeypatch)
    assert 'error' not in await resume(connection)
    assert (await call(connection))['result']['subagents'][0]['subagent_id'] == item['subagent_id']
    result = (await call(connection, 'history', subagent_id=item['subagent_id']))['result']
    texts = [row['text'] for row in result['messages']]
    assert texts.count('Synthetic final result') == 1 and 'New child final' in texts
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.asyncio
async def test_disabled_managed_adapter_refuses_typed_without_breaking_other_reads(main_home, monkeypatch):
    from hermes_runtime.adapters import hermes
    home, db = main_home
    _authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    monkeypatch.setattr(hermes, '_home', None)
    assert (await call(connection))['error']['data']['reason'] == 'permission_denied'
    assert (await connection.dispatch({'id': 1, 'method': 'ping'}))['result'] == {'pong': True}


@pytest.mark.asyncio
async def test_tracked_cards_require_ownership_and_bounded_unique_ids(main_home, monkeypatch):
    home, db = main_home
    db.create_session('foreign-parent', source='cli')
    foreign, _ = project(db, parent='foreign-parent')
    _authority, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    for identity in (foreign['subagent_id'], 'project-' + '0' * 32):
        response = await call(connection, tracked=[identity])
        assert response['error']['data']['reason'] == 'not_found'
    for ids in ([foreign['subagent_id']] * 101, ['bad'], [False], [foreign['subagent_id']] * 2):
        assert 'error' in await call(connection, tracked=ids)
