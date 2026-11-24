"""Scheduled replies survive queue and owner transitions without a new GET."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_external_wait_reply_probe import bind, body, deliver_main, row
from tests.hermes_runtime.test_external_wait_timer import _isolated_tick
from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_project_goal import _consume, _goal, _snapshot

pytest_plugins = ['tests.hermes_runtime.test_external_wait_reply_probe']


def _queued_events(db):
    return [json.loads(row[0]) for row in db._read_all(
        "SELECT event_json FROM async_delegations WHERE delegation_id LIKE 'main-cron-%' ORDER BY rowid",
    )]


def _final_slot(source, monkeypatch):
    from cron.jobs import get_job

    _, _, _, after, _ = source
    deadline = datetime.now(UTC) + timedelta(minutes=20)
    _, job_id = bind(source, deadline=deadline)
    assert datetime.fromisoformat(get_job(job_id)['next_run_at']) < deadline
    read = Mock(return_value=body(row('final-slot-reply', 'supplier-1', after + timedelta(minutes=1))))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    return deadline, job_id, read


def test_first_publication_failure_on_final_slot_recovers_the_saved_reply(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    actor, project, *_ = source
    deadline, job_id, read = _final_slot(source, monkeypatch)
    goal_turn.finish_segment(actor, 'Waiting for the verified reply.')
    assert _snapshot(actor, project)['status'] == 'waiting'
    with monkeypatch.context() as failing:
        failing.setattr('hermes_runtime.standalone.events._admit_scheduled_prompt',
                        Mock(side_effect=OSError('outbox write failed after observation')))
        _isolated_tick(failing, job_id)
    assert datetime.fromisoformat(get_job(job_id)['next_run_at']) > deadline
    inbox = HomeInbox()
    assert inbox.pending() == []
    goal_turn.recover()
    [resumed] = inbox.pending()
    assert 'final-slot-reply' in resumed[1]
    goal_turn.recover()
    assert inbox.pending() == [resumed]
    read.assert_called_once()


@pytest.mark.asyncio
async def test_panel_resume_retries_a_transient_publication_failure(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox
    from tests.hermes_runtime.test_panel_assignments import _call_panel
    from tests.hermes_runtime.test_panel_main_bootstrap import resume, runtime

    actor, project, _, _, config = source
    deadline, job_id, read = _final_slot(source, monkeypatch)
    with monkeypatch.context() as clock:
        _isolated_tick(clock, job_id)
    assert datetime.fromisoformat(get_job(job_id)['next_run_at']) > deadline
    inbox = HomeInbox()
    [first] = inbox.pending()
    _, connection = runtime(config.parent, actor._session_db, monkeypatch)
    assert 'error' not in await resume(connection)
    await _call_panel(connection, 'control', project_id=project, action='pause')
    model = Mock(side_effect=AssertionError('paused reply reached model'))
    assert deliver_main(actor, inbox, first, model)['_notification_reply_muted']
    assert inbox.pending() == []

    with monkeypatch.context() as failing:
        failing.setattr('hermes_runtime.standalone.events._admit_scheduled_prompt',
                        Mock(side_effect=OSError('transient outbox failure on Panel resume')))
        await _call_panel(connection, 'control', project_id=project, action='resume')
    assert inbox.pending() == []
    assert _snapshot(actor, project)['status'] == 'pending'
    goal_turn.recover()
    [resumed] = inbox.pending()
    assert 'final-slot-reply' in resumed[1]
    read.assert_called_once()


@pytest.mark.asyncio
async def test_cancelling_one_native_queued_turn_keeps_future_reply_polls_running(source, monkeypatch):
    from cron.jobs import get_job
    from gateway.run import _format_gateway_process_notification
    from gateway.session_controls import AuthorityConnection
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.events import admit_cron
    from hermes_state_runtime import list_session_admissions

    actor, _, _, after, config = source
    _, job_id = bind(source)
    home, db = config.parent, actor._session_db
    runner, authority = await gateway_owner(home, db, monkeypatch, 'reply-recovery')
    first = row('first-reply', 'supplier-1', after + timedelta(minutes=1))
    second = row('subsequent-reply', 'supplier-1', after + timedelta(minutes=2))
    read = Mock(side_effect=[body(first), body(first, second)])
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)

    assert admit_cron(get_job(job_id), execution_id='initial')[0]
    [event] = _queued_events(db)
    assert await runner._deliver_completion_notification(_format_gateway_process_notification(event), event) is True
    [admission] = list_session_admissions(db, session_id=HOME_SESSION_ID, pending_only=False)
    assert admission['status'] == 'queued'
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: True), {
        'user_id': 'fixture-panel', 'provider': 'local', 'profile_id': str(home),
        'instance_id': 'reply-recovery', 'native_bootstrap': True,
    })
    mounted = await connection.dispatch({'id': 1, 'method': 'session.resume',
                                         'params': {'session_id': HOME_SESSION_ID, 'display_projection': True}})
    assert 'error' not in mounted, mounted
    cancelled = await connection.dispatch({'id': 2, 'method': 'prompt.cancel', 'params': {
        'session_id': HOME_SESSION_ID, 'admission_id': admission['admission_id'],
    }})
    assert cancelled['result']['outcome'] == 'cancelled'
    assert admit_cron(get_job(job_id), execution_id='next-slot')[0]
    first_event, later = _queued_events(db)
    assert first_event['delegation_id'] == event['delegation_id']
    assert later['main_project_event']['metadata']['reply_ref']['message_id'] == 'subsequent-reply'
    assert read.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['before_turn', 'outbox_drop'])
async def test_reply_retries_failed_handoffs_once_before_the_next_timer(source, monkeypatch, failure):
    from cron.jobs import get_job
    from gateway.session_results import record_unexecuted_failure
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.events import admit_cron
    from hermes_state_runtime import list_session_admissions
    from tools.async_delegation import claim_completion_delivery, drop_completion_delivery

    actor, _, _, after, config = source
    _, job_id = bind(source)
    home, db = config.parent, actor._session_db
    runner, authority = await gateway_owner(home, db, monkeypatch, 'reply-failure')
    reply = row('verified-reply', 'supplier-1', after + timedelta(minutes=1))
    read = Mock(return_value=body(reply))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)

    async def fail_before_turn(*_args, **_kwargs):
        record_unexecuted_failure('The model agent could not be initialized')

    monkeypatch.setattr(runner, '_handle_message_with_agent', fail_before_turn)

    async def fail(event):
        if failure == 'before_turn':
            await _consume(runner, authority, home, event)
            admission = list_session_admissions(db, session_id=HOME_SESSION_ID, pending_only=False)[-1]
            assert (admission['status'], admission['outcome']) == ('terminal', 'failed')
        else:
            identity = event['delegation_id']
            assert claim_completion_delivery(identity, 'fixture-claim')
            assert drop_completion_delivery(identity, 'fixture-claim')

    assert admit_cron(get_job(job_id), execution_id='first-slot')[0]
    [first] = _queued_events(db)
    await fail(first)
    goal_turn.recover()
    _, retry = _queued_events(db)
    assert retry['main_project_event']['metadata']['reply_ref']['message_id'] == 'verified-reply'
    await fail(retry)
    goal_turn.recover()
    goal_turn.recover()
    assert _queued_events(db) == [first, retry]

    assert admit_cron(get_job(job_id), execution_id='next-slot')[0]
    _, _, next_slot = _queued_events(db)
    assert next_slot['main_project_event']['metadata']['reply_ref']['message_id'] == 'verified-reply'
    read.assert_called_once()
