"""A bound timer checks an existing read-only connector before admitting a turn."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_external_wait_timer import _isolated_tick, _timer
from tests.hermes_runtime.test_project_goal import _actor, _open
from tests.tools.test_delegate_external_wait import _args, _call
from utils import fast_safe_load

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.fixture
def source(main_home):
    home, db = main_home
    config = home / 'config.yaml'
    settings = fast_safe_load(config.read_text())
    api = {'base_url': 'https://chat.example.invalid/api', 'operations': [{
        'name': 'thread_history', 'description': 'Read the exact thread', 'method': 'GET',
        'path': 'threads/{thread}', 'input_schema': {'type': 'object', 'properties': {'path': {'type': 'object'}}},
    }]}
    settings['mcp_servers'] = {'fixture-chat': {'hermes_api': api}}
    config.write_text(json.dumps(settings))
    actor = _actor(db)
    project = _open(actor, 'Obtain a reply from the supplier')
    after = datetime.now(UTC) - timedelta(minutes=3)
    probe = {'connector': 'fixture-chat', 'operation': 'thread_history',
             'arguments': {'path': {'thread': 'approved-thread'}}, 'items_path': ['messages'],
             'id_path': ['id'], 'sender_path': ['author', 'id'], 'time_path': ['sentAt'],
             'sender': 'supplier-1', 'after': after.isoformat()}
    return actor, project, probe, after, config


def bind(source):
    actor, project, probe, *_ = source
    wait = _call(actor, **_args(project), reply_probe=probe)
    assert wait.get('success'), wait
    timer = _timer(actor, {'action': 'create', 'project_id': project, 'wait_id': wait['wait_id'],
                           'schedule': 'every 15m', 'prompt': 'Read the matching message in the approved thread.'})
    assert timer.get('success'), timer
    return wait['wait_id'], timer['job_id']


def row(identity, sender, instant):
    return {'id': identity, 'author': {'id': sender}, 'sentAt': instant.isoformat()}


def body(*rows):
    return 200, json.dumps({'messages': rows}).encode()


def test_due_timer_is_quiet_until_the_exact_sender_replies(source, monkeypatch):
    from cron import jobs
    from cron.executions import latest_execution
    from hermes_runtime.adapters.inbox import HomeInbox

    actor, project, _, after, *_ = source
    wait_id, job_id = bind(source)
    unrelated = row('other-1', 'other-user', after + timedelta(minutes=1))
    older = row('older-1', 'supplier-1', after - timedelta(minutes=1))
    reply = row('reply-1', 'supplier-1', after + timedelta(minutes=1))
    read = Mock(side_effect=[body(older, unrelated), body(older, unrelated, reply), body(reply)])
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.turns.forward_to_active_turn', Mock(return_value=False))
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    inbox = HomeInbox()

    for expected in ('no_change', 'admitted', 'no_change'):
        with monkeypatch.context() as clock:
            _isolated_tick(clock, job_id)
        assert jobs.get_job(job_id)['last_status'] == expected
        assert latest_execution(job_id)['delivery_outcome'] == ('admitted' if expected == 'admitted' else 'suppressed')
        if read.call_count == 1:
            assert inbox.pending() == []
    [(_identity, prompt)] = inbox.pending()
    metadata = json.loads(prompt.splitlines()[1])['metadata']
    ref = {'kind': 'polled_reply', 'message_id': 'reply-1'}
    assert metadata['reply_ref'] == ref
    assert metadata['project_id'] == project and metadata['wait_id'] == wait_id and metadata['project_goal']
    assert read.call_count == 3
    assert all(call.args[0].method == 'GET' and call.args[0].full_url.endswith('/threads/approved-thread')
               for call in read.call_args_list)
    forged = _call(actor, op='close', project_id=project, wait_id=wait_id, reason='reply',
                   reply_ref={**ref, 'message_id': 'other-1'})
    assert forged.get('error')
    closed = _call(actor, op='close', project_id=project, wait_id=wait_id, reason='reply', reply_ref=ref)
    assert closed['state'] == 'CLOSED' and jobs.get_job(job_id) is None


def test_observed_reply_survives_uncertain_handoff_and_replay(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone import events

    _, _, _, after, *_ = source
    _, job_id = bind(source)
    reply = row('reply-1', 'supplier-1', after + timedelta(minutes=1))
    read = Mock(return_value=body(reply))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    original = events._admit_scheduled_prompt

    def lose_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('lost the receipt after saving the wake')

    with monkeypatch.context() as interrupted:
        interrupted.setattr(events, '_admit_scheduled_prompt', lose_receipt)
        assert not events.admit_cron(get_job(job_id), execution_id='before-restart')[0]
    inbox = HomeInbox()
    [accepted] = inbox.pending()
    assert events.admit_cron(get_job(job_id), execution_id='after-restart')[0]
    assert inbox.pending() == [accepted]
    read.assert_called_once()
    assert 'no_change' in events.admit_cron(get_job(job_id), execution_id='next-slot')[1]
    assert inbox.pending() == [accepted]


def test_failed_and_changed_reads_are_internal_and_keep_the_timer(source, monkeypatch):
    from cron.jobs import get_job
    from cron.executions import latest_execution
    from hermes_runtime.adapters.inbox import HomeInbox

    _, _, _, _, config = source
    _, job_id = bind(source)
    read = Mock(return_value=(503, b'{"error":"connector unavailable"}'))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    with monkeypatch.context() as clock:
        _isolated_tick(clock, job_id)
    assert get_job(job_id)['last_status'] == 'error'
    assert latest_execution(job_id)['delivery_outcome'] == 'suppressed'
    assert HomeInbox().pending() == []

    changed = fast_safe_load(config.read_text())
    changed['mcp_servers']['fixture-chat']['hermes_api']['operations'][0]['path'] = 'different-threads/{thread}'
    config.write_text(json.dumps(changed))
    with monkeypatch.context() as clock:
        _isolated_tick(clock, job_id)
    read.assert_called_once()
    assert latest_execution(job_id)['delivery_outcome'] == 'suppressed'
    assert HomeInbox().pending() == []


def test_polled_child_reply_uses_the_saved_worker_and_respects_parent_pause(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.events import admit_cron
    from tests.hermes_runtime.test_child_goals import _child_actor
    from tests.hermes_runtime.test_project_goal import _goal
    from tools.delegate_mail_work import ensure_mail_input, stage_mail_input
    from tools.delegate_project_store import ProjectStore

    main, root, probe, after, *_ = source
    db = main._session_db
    with ProjectStore(main) as store:
        coordinator = store.create('coordinator', 'Coordinate supplier work', '')
    owner = _child_actor(db, coordinator, root)
    owner._delegate_depth = 1
    db.create_session(owner.session_id, source='delegate', parent_session_id=HOME_SESSION_ID)
    child = _open(owner, 'Verify a supplier response')
    with ProjectStore(owner) as store:
        worker = store.create('supplier-worker', 'Consult the supplier', '')
        agent = SimpleNamespace(_session_db=db, session_id=worker['session_id'], _project_input_goal_id=child)
        db.create_session(agent.session_id, source='delegate', parent_session_id=owner.session_id)
        store.enqueue(worker, 'Request a reply', {'goal_project_id': child})
        assert store.settle(worker, store.claim(worker), {'status': 'completed', 'summary': 'The request was delivered'})

    wait = _call(owner, **{**_args(child), 'mode': 'resume_child'}, reply_probe=probe,
                 schedule='every 15m', prompt='Check the saved supplier thread.')
    assert wait.get('success'), wait
    job_id = wait['job_id']
    reply = row('reply-child', 'supplier-1', after + timedelta(minutes=1))
    read = Mock(side_effect=[body(), body(reply), body(reply)])
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    _goal(main, 'pause', project_id=root)
    assert not admit_cron(get_job(job_id), execution_id='paused')[0]
    read.assert_not_called()
    _goal(main, 'resume', project_id=root)
    assert 'no_change' in admit_cron(get_job(job_id), execution_id='empty')[1]
    assert admit_cron(get_job(job_id), execution_id='reply')[0]
    assert 'no_change' in admit_cron(get_job(job_id), execution_id='repeat')[1]
    with ProjectStore(owner) as store:
        ensure_mail_input(owner, store, worker)
        message = store.claim(worker)
        assert message is not None
        assert 'reply-child' in stage_mail_input(agent, message, [])[0]
    [mail] = db._read_all("SELECT target_session_id, origin_json FROM delegation_mail WHERE kind='child_goal'")
    assert mail['target_session_id'] == worker['session_id']
    event = json.loads(mail['origin_json'])['child_goal_event']
    assert event['project_id'] == child and event['wait_id'] == wait['wait_id']
    assert event['reply_ref'] == {'kind': 'polled_reply', 'message_id': 'reply-child'}
