"""A selected worker's mailbox is independent of the current diagram page."""
from types import SimpleNamespace

import pytest

from tests.hermes_runtime.test_panel_assignments import _call_panel, _worker
from tests.hermes_runtime.test_panel_main_bootstrap import resume, runtime
from tests.hermes_runtime.test_project_goal import _actor, _open
from tools.delegate_mail import mail_action
from tools.delegate_mail_store import MailStore
from tools.delegate_project_store import ProjectStore

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.mark.asyncio
async def test_selected_worker_shows_offpage_mail_with_exact_scope_and_cursor(main_home, monkeypatch):
    home, db = main_home
    actor = _actor(db)
    goal = _open(actor, 'Primary', slot=1)
    other = _open(actor, 'Secondary', slot=2)
    with ProjectStore(actor) as store:
        peers = [_worker(store, db, f'Worker {i}', goal) for i in range(7)]
        unrelated = _worker(store, db, 'Other assignment worker', other)
    peer, selected = peers[0], peers[-1]
    receiver = SimpleNamespace(_session_db=db, session_id=peer['session_id'], _project_input_goal_id=goal)
    sender = SimpleNamespace(_session_db=db, session_id=selected['session_id'], _project_input_goal_id=goal)
    outgoing = mail_action(sender, 'message', peer['subagent_id'], 'Question for off-page peer', 'outgoing')
    incoming = mail_action(receiver, 'message', selected['subagent_id'], 'Reply from off-page peer', 'incoming')
    mail_action(sender, 'message', unrelated['subagent_id'], 'Outside selected assignment', 'unrelated')

    with MailStore(receiver) as store:
        delivery = store.delivery(set())
        db.append_message(peer['session_id'], 'user', delivery['content'],
                          platform_message_id='project-msg-associated-input',
                          display_kind=delivery['display_kind'],
                          display_metadata={**delivery['display_metadata'], 'agent_mail_delivery_id': delivery['message_id']})
        store.acknowledge_recorded()
    assert db._read_one('SELECT content FROM delegation_mail WHERE message_id=?', (outgoing['message_id'],))['content'] == ''

    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    graph = await _call_panel(connection, 'graph', project_id=goal, limit=6)
    assert len(graph['nodes']) == 6 and graph['page']['has_more']
    assert {row['id'] for row in graph['nodes']} == {row['subagent_id'] for row in peers[1:]}
    params = {'node_ids': [{field: row[field] for field in ('id', 'parent_id')} for row in graph['nodes']],
              'project_id': goal, 'selection': {'type': 'node', 'id': selected['subagent_id']}, 'limit': 1}
    latest = await _call_panel(connection, 'timeline', **params)
    assert latest['page']['has_more']
    assert [event['id'] for event in latest['events']] == ['mail:' + incoming['message_id']]
    assert latest['events'][0]['from_name'] == peer['project_key']
    older = await _call_panel(connection, 'timeline', **params, before=latest['page']['before'])
    assert not older['page']['has_more']
    assert [event['id'] for event in older['events']] == ['mail:' + outgoing['message_id']]
    assert older['events'][0]['to_name'] == peer['project_key']
    assert older['events'][0]['text'] == 'Question for off-page peer'

    params['selection']['type'] = 'edge'
    edge = await _call_panel(connection, 'timeline', **params)
    assert edge['events'] == []
