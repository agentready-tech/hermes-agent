"""Panel reads only owned delegation histories and controls the exact AGE-6 Goal."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_runtime.sessions import HOME_SESSION_ID
from tests.hermes_runtime.test_external_wait_timer import _timer
from tests.hermes_runtime.test_panel_main_bootstrap import resume, runtime
from tests.hermes_runtime.test_panel_subagents import call
from tests.hermes_runtime.test_project_goal import _actor, _open
from tests.tools.test_delegate_external_wait import _args, _call
from tools.delegate_project_store import ProjectStore

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def _worker(store, db, name, creator):
    node = store.create(name, name, '', workspace={'goal_owner_id': creator})
    child = node['session_id']
    db.create_session(child, source='delegate', parent_session_id=store.parent_id,
                      model_config={'_delegate_from': store.parent_id})
    return node


def _dispatch(store, db, node, goal, text):
    message = store.enqueue(node, text, {'goal_project_id': goal})
    claimed = store.claim(node)
    assert claimed['message_id'] == message
    child = node['session_id']
    db.append_message(child, 'user', text, platform_message_id=message)
    db.append_message(child, 'tool', text + ' output', tool_name='local_test', tool_call_id=message)
    db.append_message(child, 'assistant', text + ' result', finish_reason='stop')
    store.settle(node, claimed, {'status': 'completed', 'summary': text + ' result'})
    return message


async def _call_panel(connection, operation, **payload):
    response = await call(connection, operation, **payload)
    assert 'error' not in response, response
    return response['result']


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_block", [True, False])
async def test_graph_marks_a_stopped_orchestrator_as_blocked(main_home, monkeypatch, explicit_block):
    from tests.hermes_runtime.test_child_goals import _child_actor
    from tests.hermes_runtime.test_project_goal import _goal

    home, db = main_home
    main = _actor(db)
    assignment = _open(main, "Complete the review and QA")
    with ProjectStore(main) as store:
        coordinator = _worker(store, db, "orchestrator", assignment)
        owner = _child_actor(db, coordinator, assignment)
        stage = _open(owner, "Run QA")
        message_id = store.enqueue(coordinator, "Coordinate QA", {"goal_project_id": assignment, "wake": True})
        message = store.claim(coordinator)
        owner._project_goal_segment.input_id = message_id
        if explicit_block:
            _goal(owner, "block", project_id=stage, blocker="QA access is missing",
                  unblock="The stand owner authorizes access", notify=True)
        assert store.settle(coordinator, message, {"status": "completed", "summary": "QA is outstanding."})

    _, connection = runtime(home, db, monkeypatch)
    assert "error" not in await resume(connection)
    graph = await _call_panel(connection, "graph", project_id=assignment)
    [node] = [node for node in graph["nodes"] if node["id"] == coordinator["subagent_id"]]
    assert node["status"] == "blocked"


@pytest.mark.asyncio
async def test_owned_graph_and_paged_timeline_use_exact_dispatch_receipts(main_home, monkeypatch):
    from tests.hermes_runtime.test_child_goals import _child_actor
    from tools.delegate_mail import mail_action

    home, db = main_home
    actor = _actor(db)
    first = _open(actor, 'First assignment', slot=1)
    second = _open(actor, 'Second assignment', slot=2)
    with ProjectStore(actor) as store:
        shared = _worker(store, db, 'shared worker', first)
        _dispatch(store, db, shared, first, 'first dispatched')
        _dispatch(store, db, shared, second, 'second dispatched')
        dedicated = _worker(store, db, 'dedicated worker', first)
        _dispatch(store, db, dedicated, first, 'orchestrate')
        _dispatch(store, db, dedicated, second, 'orchestrate second')
    coordinator = _child_actor(db, dedicated, first)
    other_coordinator = _child_actor(db, dedicated, second)
    child_goal = _open(coordinator, 'Scoped role goal')
    other_child_goal = _open(other_coordinator, 'Other role goal')
    with ProjectStore(coordinator) as store:
        leaf = _worker(store, db, 'leaf-first', first)
        _dispatch(store, db, leaf, first, 'Secondary')
        role = _worker(store, db, 'role worker', child_goal)
        _dispatch(store, db, role, child_goal, 'Scoped role')
        _dispatch(store, db, role, other_child_goal, 'Other role')
    mail_action(coordinator, 'message', role['subagent_id'], 'First role coordination', 'first-role-mail')
    mail_action(other_coordinator, 'message', role['subagent_id'], 'Other role coordination', 'other-role-mail')
    db.create_session('root-one-shot', source='subagent', parent_session_id=HOME_SESSION_ID)
    db.append_message('root-one-shot', 'user', 'Unlinked direct request')
    db.create_session('unrelated-parent', source='cli')
    db.create_session('unrelated-child', source='subagent', parent_session_id='unrelated-parent')

    _, connection = runtime(home, db, monkeypatch)
    assert 'error' in await call(connection, 'assignments')
    assert 'error' not in await resume(connection)
    listing = await _call_panel(connection, 'assignments', limit=1)
    assert listing['goals'][0]['project_id'] == second
    assert (await _call_panel(connection, 'assignments', before=listing['page']['before']))['goals'][0]['project_id'] == first
    first_scope = await _call_panel(connection, 'graph', project_id=first)
    assert first_scope['goal']['verification'] == 'Return a verified result'
    nodes = {node['id']: node['parent_id'] for node in first_scope['nodes']}
    assert nodes == {shared['subagent_id']: 'main', dedicated['subagent_id']: 'main',
                     leaf['subagent_id']: dedicated['subagent_id'], role['subagent_id']: dedicated['subagent_id']}
    assert next(node for node in first_scope['nodes'] if node['id'] == role['subagent_id'])['status'] == 'completed'
    assert 'root-one-shot' in {row['id'] for row in (await _call_panel(connection, 'graph'))['nodes']}
    args = {'project_id': first, 'tools': True, 'node_ids': [{'id': key, 'parent_id': value} for key, value in nodes.items()]}
    newest = await _call_panel(connection, 'timeline', **args, limit=2)
    older = await _call_panel(connection, 'timeline', **args, before=newest['page']['before'])
    events = older['events'] + newest['events']
    assert len({row['id'] for row in events}) == len(events)
    texts = {row['text'] for row in events}
    assert {'first dispatched', 'first dispatched result', 'first dispatched output', 'Secondary result',
            'Scoped role result', 'Scoped role output', 'First role coordination'}.issubset(texts)
    assert all('second dispatched' not in value and 'Other role' not in value for value in texts)
    assert 'error' in await call(connection, 'timeline', node_ids=[{'id': 'unrelated-child', 'parent_id': 'main'}])


@pytest.mark.asyncio
async def test_pause_resume_and_cancel_preserve_exact_timer_and_other_goals(main_home, monkeypatch):
    from cron.jobs import get_job
    from cron.wait_binding import admitted_binding
    from hermes_runtime.adapters.goal_turn import finish_segment

    home, db = main_home
    actor = _actor(db)
    first = _open(actor, 'Wait for supplier', slot=1)
    other = _open(actor, 'Keep working', slot=2)
    wait = _call(actor, **_args(first))
    timer_id = _timer(actor, {'action': 'create', 'project_id': first, 'wait_id': wait['wait_id'],
                             'schedule': 'every 15m', 'prompt': 'Check the supplier thread'})['job_id']
    # Leave both targets durable but only evaluate the selected wait.
    actor._agentready_active_turn.goal_segment.project_ids = {first}
    finish_segment(actor, 'Waiting.')
    with ProjectStore(actor) as store:
        worker = _worker(store, db, 'shared', first)
        held = store.enqueue(worker, 'waited task', {'goal_project_id': first})
        unaffected = store.enqueue(worker, 'unaffected task', {'goal_project_id': other})
    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    owner = connection.actor
    connection.actor = replace(owner, capabilities=frozenset({'session:read'}))
    assert 'error' in await call(connection, 'control', project_id=first, action='pause')
    connection.actor = owner

    card = (await _call_panel(connection, 'graph', project_id=first))['goal']
    assert card['status'] == 'waiting' and card['next_check_at'] == get_job(timer_id)['next_run_at']
    paused = await _call_panel(connection, 'control', project_id=first, action='pause')
    assert paused['goal']['status'] == 'paused'
    job = get_job(timer_id)
    with pytest.raises(ValueError), admitted_binding(job):
        raise AssertionError('Paused timer reached admission')
    with ProjectStore(actor) as store:
        active = store.claim(worker)
        assert active['message_id'] == unaffected
        store.settle(worker, active, {'status': 'completed', 'summary': 'Done'})
        assert store.claim(worker) is None and store.message(held)['state'] == 'pending'

    resumed = await _call_panel(connection, 'control', project_id=first, action='resume')
    assert resumed['goal']['project_id'] == first
    with ProjectStore(actor) as store:
        from tools.delegate_project_goal import read_goal
        wake = read_goal(store, first)['wake_id']
        assert wake
    with admitted_binding(job):
        assert get_job(timer_id)['id'] == timer_id
    with ProjectStore(actor) as store:
        active = store.claim(worker)
        assert active['message_id'] == held
        store.release(active)

    await _call_panel(connection, 'control', project_id=first, action='pause')
    cancelled = await _call_panel(connection, 'control', project_id=first, action='cancel')
    assert cancelled['goal']['status'] == 'cancelled'
    assert get_job(timer_id) is None
    with ProjectStore(actor) as store:
        assert store.message(held)['state'] == 'cancelled'
        assert store.message(unaffected)['state'] == 'completed'
    listing = await _call_panel(connection, 'assignments')
    assert [goal['project_id'] for goal in listing['goals']] == [other, first]


@pytest.mark.asyncio
async def test_nested_worker_inherits_each_accepted_dispatch_and_recovered_input(main_home, monkeypatch):
    from tools import delegate_projects, delegate_tool

    home, db = main_home
    owner = _actor(db)
    first = _open(owner, 'First', slot=1)
    second = _open(owner, 'Second', slot=2)
    monkeypatch.setattr(delegate_tool, '_load_config', dict)
    monkeypatch.setattr(delegate_tool, '_resolve_delegation_credentials', lambda *_: {
        'provider': 'custom', 'base_url': None, 'api_key': None, 'api_mode': None,
    })
    monkeypatch.setattr(delegate_tool, '_build_child_preserving_parent_tools', lambda **kw: SimpleNamespace(
        _session_db=db, session_id=kw['session_id'], ephemeral_system_prompt='worker',
        _progress_identity_ref={}, _delegate_depth=1))
    monkeypatch.setattr(delegate_projects, 'recover_projects', lambda *_: [])
    monkeypatch.setattr('tools.delegate_project_store._owner_liveness', lambda: lambda *_: False)
    task = [{'project': 'same-nested-worker', 'goal': 'Same instruction'}]

    with ProjectStore(owner) as store:
        worker = _worker(store, db, 'shared coordinator', first)
        gone_id = store.enqueue(worker, 'First dispatch', {'goal_project_id': first})
        assert store.claim(worker)['message_id'] == gone_id
        db.append_message(worker['session_id'], 'user', 'First dispatch', platform_message_id=gone_id)
        next_input = store.enqueue(worker, 'Second dispatch', {'goal_project_id': second})
        recovering = store.claim(worker)
        assert recovering['recovering'] and recovering['message_id'] == gone_id
        child, _ = delegate_projects._build_child(owner, store, worker, recovering)
        first_child = delegate_projects.project_action(child, 'spawn', tasks=task)['projects'][0]
        assert child._delegate_replaying_input
        assert delegate_projects.project_action(child, 'spawn', tasks=task)['projects'][0]['message_id'] == first_child['message_id']
        store.settle(worker, recovering, {'status': 'completed', 'summary': 'First queued'})
        fresh = store.claim(worker)
        assert fresh['message_id'] == next_input
        child, _ = delegate_projects._build_child(owner, store, worker, fresh)
        second_child = delegate_projects.project_action(child, 'spawn', tasks=task)['projects'][0]
        store.settle(worker, fresh, {'status': 'completed', 'summary': 'Second queued'})
        assert first_child['subagent_id'] == second_child['subagent_id']
        assert first_child['message_id'] != second_child['message_id']

    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    graph_first = await _call_panel(connection, 'graph', project_id=first)
    graph_second = await _call_panel(connection, 'graph', project_id=second)
    for graph in [graph_first, graph_second]:
        assert {row['id'] for row in graph['nodes']} == {worker['subagent_id'], first_child['subagent_id']}
    await _call_panel(connection, 'control', project_id=first, action='pause')
    with ProjectStore(child) as store:
        nested = store.get(first_child['subagent_id'])
        active = store.claim(nested)
        assert active['message_id'] == second_child['message_id']
        store.settle(nested, active, {'status': 'completed', 'summary': 'Second done'})
        assert store.claim(nested) is None
        assert json.loads(store.message(first_child['message_id'])['origin_json'])['goal_project_id'] == first
        assert json.loads(store.message(second_child['message_id'])['origin_json'])['goal_project_id'] == second
    await _call_panel(connection, 'control', project_id=first, action='cancel')
    with ProjectStore(child) as store:
        assert store.message(first_child['message_id'])['state'] == 'cancelled'
        assert store.message(second_child['message_id'])['state'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('live_owner', [True, False])
async def test_recovery_wake_obeys_pause_and_resumes_only_abandoned_running_input(main_home, monkeypatch, live_owner):
    from hermes_runtime.adapters import goal_turn
    from tools.delegate_project_goal import read_goal
    from tools.delegate_project_recovery import publish_recovery_wakeups

    home, db = main_home
    actor = _actor(db)
    target = _open(actor, 'Resume this specific assignment', slot=1)
    other = _open(actor, 'Continue independently', slot=2)
    with ProjectStore(actor) as store:
        worker = _worker(store, db, 'shared worker', target)
        linked = store.enqueue(worker, 'Abandoned dispatch', {'goal_project_id': target, 'wake': True})
        assert store.claim(worker)['message_id'] == linked
        unrelated_worker = _worker(store, db, 'other worker', other)
        unrelated = store.enqueue(unrelated_worker, 'Other pending work', {'goal_project_id': other, 'wake': True})
    monkeypatch.setattr('tools.delegate_project_recovery._owner_liveness', lambda: lambda *_: False)
    monkeypatch.setattr('tools.delegate_project_store._owner_liveness', lambda: lambda *_: live_owner)
    assert publish_recovery_wakeups() == 2
    recovery_events = [json.loads(row['event_json']) for row in db._read_all(
        "SELECT event_json FROM async_delegations WHERE delegation_id LIKE 'project-recovery-%'")]
    early = next(event for event in recovery_events if event['goal_project_id'] == target)
    neighbor = next(event for event in recovery_events if event['goal_project_id'] == other)
    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)

    def admission(event):
        return {'admission_id': 'recovery-admission', 'payload': {'local_automation_v1': {
            'identity': json.dumps(['async_delegation', event['delegation_id'], ''])}}}

    async def assert_quiet(action):
        await _call_panel(connection, 'control', project_id=target, action=action)
        token = goal_turn.admitted_input.set(admission(early))
        try:
            model = Mock(side_effect=AssertionError('Obsolete recovery must not call the model'))
            result = goal_turn.run_segment(actor, SimpleNamespace(), model, early['summary'])
            assert result['completed'] and result['_notification_reply_muted']
            model.assert_not_called()
        finally:
            goal_turn.admitted_input.reset(token)

    await assert_quiet('pause')
    token = goal_turn.admitted_input.set(admission(neighbor))
    try:
        segment, stale = goal_turn._input_segment(actor, {}, [])
        assert not stale and segment.bound_project_ids == {other}
    finally:
        goal_turn.admitted_input.reset(token)

    # The same abandoned receipt cannot be published again. Resume from a new
    # native Goal wake when the claim has died, and let a live owner report normally.
    assert publish_recovery_wakeups() == 0
    wakes = Mock()
    monkeypatch.setattr('hermes_runtime.standalone.events._admit_scheduled_prompt', wakes)
    await _call_panel(connection, 'control', project_id=target, action='resume')
    with ProjectStore(actor) as store:
        goal = read_goal(store, target)
        assert store.message(unrelated)['state'] == 'pending'
        if live_owner:
            assert goal['status'] == 'running' and goal['wake_id'] is None
            wakes.assert_not_called()
            assert store.claim(worker) is None
        else:
            assert goal['status'] == 'ready' and goal['wake_id'] != early['delegation_id']
            assert wakes.call_args.args[1:] == (goal['wake_id'], {'project_id': target})
            assert store.claim(worker)['message_id'] == linked
    await assert_quiet('cancel')


def _deliver_mail(db, receiver, suffix=''):
    from tools.delegate_mail_store import MailStore

    with MailStore(receiver) as store:
        body = store.delivery(set())
        db.append_message(receiver.session_id, 'user', body['content'] + suffix, platform_message_id=body['message_id'],
                          display_kind=body['display_kind'], display_metadata=body['display_metadata'])
        store.acknowledge_recorded()


@pytest.mark.asyncio
async def test_assignment_mail_recovers_persisted_body_and_projects_exact_scope(main_home, monkeypatch):
    from tools.delegate_mail import mail_action
    from tools.delegate_mail_store import MailStore

    home, db = main_home
    actor = _actor(db)
    first = _open(actor, 'First', slot=1)
    other = _open(actor, 'Other', slot=2)
    with ProjectStore(actor) as store:
        shared = _worker(store, db, 'shared worker', first)
        _dispatch(store, db, shared, first, 'First dispatch')
        _dispatch(store, db, shared, other, 'Other dispatch')
        peer = _worker(store, db, 'peer', first)
        _dispatch(store, db, peer, first, 'Peer dispatch')
    actor._agentready_active_turn.goal_segment.touch(first)
    sent = mail_action(actor, 'message', shared['subagent_id'], 'First mail after ack', 'first-mail')
    actor._agentready_active_turn.goal_segment.touch(other)
    mail_action(actor, 'message', shared['subagent_id'], 'Other private assignment', 'other-mail')
    shared_agent = SimpleNamespace(_session_db=db, session_id=shared['session_id'], _project_input_goal_id=first)
    peer_agent = SimpleNamespace(_session_db=db, session_id=peer['session_id'])
    peer_message = mail_action(shared_agent, 'message', peer['subagent_id'], 'Sibling conversation', 'peer-mail')
    with MailStore(peer_agent) as store:
        store.send(shared['subagent_id'], 'Synthetic service result', kind='result', wake=False)
        store.send(shared['subagent_id'], 'Unscoped peer mail', wake=False)
    _deliver_mail(db, shared_agent, '\n\nRelevant memory (verify its sources):\nPRIVATE context')
    _deliver_mail(db, peer_agent)
    assert db._read_one('SELECT content FROM delegation_mail WHERE message_id=?', (sent['message_id'],))['content'] == ''

    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    params = {'node_ids': [{'id': row['subagent_id'], 'parent_id': 'main'} for row in (shared, peer)]}
    node = {'type': 'node', 'id': shared['subagent_id']}
    scoped = await _call_panel(connection, 'timeline', **params, project_id=first, selection=node)
    mail = {event['id']: event for event in scoped['events'] if event['kind'] == 'message'}
    assert set(mail) == {'mail:' + sent['message_id'], 'mail:' + peer_message['message_id']}
    assert mail['mail:' + sent['message_id']]['text'] == 'First mail after ack'
    assert (mail['mail:' + peer_message['message_id']]['from'], mail['mail:' + peer_message['message_id']]['to']) == (
        shared['subagent_id'], peer['subagent_id'])
    edge = await _call_panel(connection, 'timeline', **params, project_id=first, selection={**node, 'type': 'edge'})
    assert [row['text'] for row in edge['events'] if row['kind'] == 'message'] == ['First mail after ack']
    overall = await _call_panel(connection, 'timeline', **params)
    assert {row['text'] for row in overall['events'] if row['kind'] == 'message'} == {
        'First mail after ack', 'Other private assignment', 'Sibling conversation', 'Unscoped peer mail'}
    assert '[Agent message ' not in json.dumps(overall)
    assert 'PRIVATE context' not in json.dumps(overall)


@pytest.mark.asyncio
async def test_one_shot_cards_project_actual_completion_and_legacy_unknown(main_home, monkeypatch):
    from tools.delegate_tool_child_run import _ChildRun

    home, db = main_home
    for status in ('failed', 'interrupted', 'completed', 'unknown'):
        sid = 'one-shot-' + status
        db.create_session(sid, source='subagent', parent_session_id=HOME_SESSION_ID)
        child = SimpleNamespace(_session_db=db, session_id=sid)
        if status != 'unknown':
            run = _ChildRun(child, None, 0, 'Fixture check', None, None)
            entry = {'status': status, 'duration_seconds': 0}
            if status == 'failed':
                run.finish_failed(entry, None, preview='Fixture failure')
            else:
                run.emit_complete({}, entry, 0)
        db.end_session(sid, 'agent_close')
    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    graph = await _call_panel(connection, 'graph')
    assert {node['id']: node['status'] for node in graph['nodes']} == {
        'one-shot-' + status: status for status in ('failed', 'interrupted', 'completed', 'unknown')}


@pytest.mark.asyncio
async def test_child_pagination_uses_equal_pages_without_overlap(main_home, monkeypatch):
    home, db = main_home
    actor = _actor(db)
    goal = _open(actor, 'Page secondary delegates')
    with ProjectStore(actor) as store:
        root = _worker(store, db, 'coordinator', goal)
    with ProjectStore(SimpleNamespace(_session_db=db, session_id=root['session_id'])) as store:
        for index in range(14):
            _worker(store, db, f'node-{index}', goal)
    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    first = await _call_panel(connection, 'graph', project_id=goal)
    initial, *children = first['nodes']
    second = await _call_panel(connection, 'graph', project_id=goal, parent_id=initial['id'], before=initial['child_page']['before'])
    third = await _call_panel(connection, 'graph', project_id=goal, parent_id=initial['id'], before=second['page']['before'])
    assert [len(children), len(second['nodes']), len(third['nodes'])] == [6, 6, 2]
    assert len({node['id'] for node in children + second['nodes'] + third['nodes']}) == 14
