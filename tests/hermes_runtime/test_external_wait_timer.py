"""Offline AGE-6 lifecycle through native cron and main admission."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_main_schedule_tools import _tool
from tests.tools.test_delegate_external_wait import _args, _call

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


def _timer(actor, arguments):
    from agent.tool_executor import _resolve_sequential_dispatch, _ToolCallRef

    ref = _ToolCallRef('cronjob_manage', arguments, actor.session_id, 'fixture-create', [])
    return json.loads(_resolve_sequential_dispatch(actor, ref, []).execute(arguments))


def _wait(db):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from tools.delegate_project_store import ProjectStore

    actor = SimpleNamespace(_session_db=db, session_id=HOME_SESSION_ID, _agentready_active_turn=SimpleNamespace())
    with ProjectStore(actor) as store:
        project = store.create('order', 'Check the existing order', '')['subagent_id']
    wait = _call(actor, **_args(project))
    assert wait['success'], wait
    ids = {'project_id': project, 'wait_id': wait['wait_id']}
    definition = {'action': 'create', **ids, 'schedule': 'every 15m', 'prompt': 'Review the exact supplier order'}
    return actor, ids, definition


def _isolated_tick(monkeypatch, job_id):
    from cron import jobs, scheduler, scheduler_tick

    due = datetime.fromisoformat(jobs.get_job(job_id)['next_run_at'])
    monkeypatch.setattr(jobs, '_hermes_now', lambda: due)
    monkeypatch.setattr(scheduler, '_hermes_now', lambda: due)
    monkeypatch.setattr(scheduler, '_launch_external_cron_worker', Mock(return_value=False))
    monkeypatch.setattr(scheduler_tick, '_pre_dispatch_housekeeping', Mock())
    for name in ('_maybe_run_worktree_maintenance', '_sweep_mcp_orphans', '_sweep_stale_inflight_for_tick'):
        monkeypatch.setattr(scheduler, name, Mock())
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', Mock(return_value=None))
    assert scheduler.tick(verbose=False) == 1
    assert datetime.fromisoformat(jobs.get_job(job_id)['next_run_at']) > due


@pytest.mark.asyncio
async def test_exact_timer_runs_same_native_main_then_closes_only_its_job(main_home, monkeypatch):
    from cron.jobs import get_job, list_jobs
    from cron.executions import latest_execution
    from gateway.run import _format_gateway_process_notification
    from gateway.session_contract import SessionRef
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.events import admit_cron
    from hermes_state_runtime import list_session_admissions
    from run_agent import AIAgent
    from tests.tools.test_delegate_mail_live import _response
    from tools.delegate_external_wait import selected_timer_proof

    home, db = main_home
    actor, ids, definition = _wait(db)
    assert selected_timer_proof(actor) is False
    assert _timer(actor, {**definition, 'schedule': 'every 5h'}).get('error')
    assert _tool('create', **{k: v for k, v in definition.items() if k != 'action'}).get('error')
    timer = _timer(actor, definition)
    assert timer.get('success'), timer
    job_id = timer['job_id']
    assert _timer(actor, definition) == timer
    assert selected_timer_proof(actor) is True
    other = _tool('create', schedule='every 1h', prompt='Unrelated periodic task')['job_id']
    assert {job['id'] for job in list_jobs()} == {job_id, other}

    runner, authority = await gateway_owner(home, db, monkeypatch, 'ordinary-wait-fixture')
    native_runs = []
    original_context = 'The existing project finding'
    reply_text = 'Order reviewed in the same session'
    model_rounds = []

    def offline_model(agent, payload, *_args, **_kwargs):
        assert agent.session_id == HOME_SESSION_ID
        history = json.dumps(payload['messages'])
        assert original_context in history and definition['prompt'] in history
        model_rounds.append(agent.session_id)
        if len(model_rounds) == 1:
            return _response(args={'action': 'external_wait', 'op': 'select', **ids})
        result = json.loads(payload['messages'][-1]['content'])
        assert result['job_id'] == job_id and result['wait_id'] == ids['wait_id']
        return _response(reply_text)

    monkeypatch.setattr(AIAgent, '_interruptible_api_call', offline_model)
    monkeypatch.setattr(AIAgent, '_interruptible_streaming_api_call', offline_model)

    async def offline_model_boundary(event, _source, key, _generation):
        sid = runner.session_store.peek_session_id(key)
        assert sid == HOME_SESSION_ID
        assert any(row['content'] == original_context for row in db.get_messages(sid))
        assert definition['prompt'] in event.text
        envelope = next(json.loads(line) for line in event.text.splitlines() if line.startswith('{"event_id":'))
        assert {k: envelope['metadata'][k] for k in ids} == ids
        native_runs.append(sid)
        agent = AIAgent(model='gpt-4.1', provider='custom', base_url='http://127.0.0.1:9/v1',
                        api_key='inert', api_mode='chat_completions', session_id=sid, session_db=db,
                        enabled_toolsets=['delegation'], skip_context_files=True, load_soul_identity=False,
                        skip_memory=True, quiet_mode=True)
        try:
            response = await asyncio.to_thread(agent.run_conversation, event.text)
            assert response['completed'] and response['final_response'] == reply_text, response
            return response['final_response']
        finally:
            agent.close()

    monkeypatch.setattr(runner, '_handle_message_with_agent', offline_model_boundary)
    with monkeypatch.context() as clock:
        await asyncio.to_thread(_isolated_tick, clock, job_id)
    execution = latest_execution(job_id)
    assert execution['delivery_outcome'] == 'admitted'
    assert get_job(job_id)['last_status'] == 'admitted'
    assert native_runs == []
    assert admit_cron(get_job(job_id), execution_id=execution['id'])[0]
    with db._read_ctx() as conn:
        rows = conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id LIKE 'main-cron-%'").fetchall()
    [row] = rows
    event = json.loads(row['event_json'])
    formatted = _format_gateway_process_notification(event)
    assert await runner._deliver_completion_notification(formatted, event) is True
    assert await runner._deliver_completion_notification(formatted, event) is None
    await asyncio.wait_for(authority._drain(SessionRef(str(home), HOME_SESSION_ID)), timeout=10)
    [admission] = list_session_admissions(db, session_id=HOME_SESSION_ID, pending_only=False)
    assert (admission['status'], admission['outcome']) == ('terminal', 'completed')
    assert native_runs == [HOME_SESSION_ID]
    assert model_rounds == [HOME_SESSION_ID, HOME_SESSION_ID]
    assert db.get_messages(HOME_SESSION_ID)[-1]['content'] == reply_text
    with db._read_ctx() as conn:
        assert [row[0] for row in conn.execute('SELECT id FROM sessions')] == [HOME_SESSION_ID]

    assert _call(actor, op='close', reason='reply', **ids, reply_ref={
        'kind': 'session_admission', 'admission_id': admission['admission_id']}).get('error')
    prior = get_job(job_id)
    done = _call(actor, op='close', reason='closed', **ids)
    assert done['state'] == 'CLOSED', done
    assert get_job(job_id) is None and get_job(other) is not None
    assert not admit_cron(prior, execution_id='prior-generation')[0]


def test_wait_retry_after_interrupted_resume_reuses_its_exact_record(main_home, monkeypatch):
    from cron import jobs
    from cron.job_definition import import_job_definitions
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron

    _, db = main_home
    actor, ids, definition = _wait(db)
    with monkeypatch.context() as m:
        m.setattr(jobs, 'resume_job', Mock(side_effect=OSError('fixture commit interrupted')))
        assert _timer(actor, definition).get('error')
    [paused] = jobs.list_jobs(include_disabled=True)
    job_id = paused['id']
    assert not paused['enabled']
    assert not admit_cron(paused, execution_id='paused')[0]
    assert HomeInbox().pending() == []
    assert _timer(actor, definition)['job_id'] == job_id
    [active] = jobs.list_jobs()
    assert active['enabled']
    untagged = {k: v for k, v in active.items() if k != 'external_wait'}
    with pytest.raises(ValueError, match='owning profile'):
        import_job_definitions({job_id: untagged}, paused_reason='fixture import')
    assert jobs.get_job(job_id)['external_wait']['state'] == 'bound'
    assert _call(actor, op='close', reason='closed', **ids)['state'] == 'CLOSED'
    with pytest.raises(ValueError, match='owning profile'):
        import_job_definitions({job_id: untagged}, paused_reason='fixture import')
    assert jobs.get_job(job_id) is None
