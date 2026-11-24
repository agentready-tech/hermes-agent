"""Regressions for verified Goal wakes, reply delivery and quiet presentation."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_external_wait_reply_probe import bind, body, deliver_main, row
from tests.hermes_runtime.test_project_goal import _actor, _agent, _goal, _models, _open, _snapshot
from tests.tools.test_delegate_mail_live import _response

pytest_plugins = ['tests.hermes_runtime.test_external_wait_reply_probe']


def test_retry_uses_original_inbox_receipt_after_telegram_route_is_bound(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.sessions import HOME_SESSION_ID, HOME_SESSION_KEY
    from hermes_runtime.standalone import events

    actor, _, _, after, _ = source
    _, job_id = bind(source)
    read = Mock(return_value=body(row('reply-1', 'supplier-1', after + timedelta(minutes=1))))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    original = events._admit_scheduled_prompt

    def lose_receipt(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('the handoff response was lost')

    with monkeypatch.context() as failing:
        failing.setattr(events, '_admit_scheduled_prompt', lose_receipt)
        assert not events.admit_cron(get_job(job_id), execution_id='before')[0]
    inbox = HomeInbox()
    [accepted] = inbox.pending()
    event_id = json.loads(accepted[1].splitlines()[1])['event_id']

    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', lambda: {
        'platform': 'telegram', 'chat_id': '42', 'session_key': HOME_SESSION_KEY,
        'origin_session_id': HOME_SESSION_ID,
    })
    assert events.admit_cron(get_job(job_id), execution_id='after')[0]
    assert inbox.pending() == [accepted]
    second = actor._session_db._read_one(
        "SELECT delegation_id FROM async_delegations WHERE json_extract(event_json, '$.main_project_event.event_id')=?",
        (event_id,),
    )
    assert second is None, f'Found a second durable admission for {event_id}: {second}'


def test_unpublished_staged_reply_respects_connector_disable(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone import events

    _, _, _, after, config = source
    _, job_id = bind(source)
    read = Mock(return_value=body(row('reply-1', 'supplier-1', after + timedelta(minutes=1))))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    with monkeypatch.context() as failing:
        failing.setattr(events, '_admit_scheduled_prompt', Mock(side_effect=OSError('crashed before queue commit')))
        assert not events.admit_cron(get_job(job_id), execution_id='before')[0]
    assert HomeInbox().pending() == []
    changed = json.loads(config.read_text())
    changed['mcp_servers']['fixture-chat']['enabled'] = False
    config.write_text(json.dumps(changed))
    events.admit_cron(get_job(job_id), execution_id='after')
    assert HomeInbox().pending() == []
    read.assert_called_once()


def test_a_step_repeats_when_a_turn_has_produced_new_evidence(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, 'Review all pages of the supplier report')
    step = 'Read the next page of the report'
    _goal(actor, 'checkpoint', project_id=goal, next_step=step)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    goal_turn.finish_segment(actor, 'Queued.')
    inbox = HomeInbox()
    [pending] = inbox.pending()
    old = _snapshot(actor, goal)['wake_id']
    turn = SimpleNamespace(inbox=inbox)
    actor._agentready_active_turn = turn
    actor._session_messages = [{'role': 'tool', 'name': 'terminal', 'tool_call_id': 'first-page',
                                'content': 'Page 1 read, next page token is 2.'}]
    monkeypatch.setattr(goal_turn, '_goal_judgments', lambda goals: {
        goal: goal_turn._GoalJudgment(missing_evidence='Page 2 is absent', next_step=step),
    })

    def model(_prompt, **_kwargs):
        _goal(actor, 'checkpoint', project_id=goal, evidence_tool_call_id='first-page', next_step=step)
        return {'completed': True, 'final_response': goal_turn.finish_segment(actor, 'Read page one.')}

    goal_turn.run_segment(actor, turn, model, pending[1], goal_records=[pending], conversation_history=[])
    saved = _snapshot(actor, goal)
    assert saved['revision'] == saved['checked_revision'] == 1
    assert saved['status'] == 'ready' and saved['wake_id'] != old, saved


def test_identical_tool_output_under_new_call_ids_cannot_restart_the_wake_budget(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    project = _open(actor, 'Confirm the finished build')
    steps = ('Read the build status in CI', 'Read the artifact index for the exact revision')
    _goal(actor, 'checkpoint', project_id=project, next_step=steps[0])
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    monkeypatch.setattr('hermes_cli.goals.DEFAULT_MAX_TURNS', 2)
    inbox = HomeInbox()
    goal_turn.finish_segment(actor, 'Queued.')

    for index in range(3):
        [pending] = inbox.pending()
        call_id = f'check-{index}'
        next_step = steps[(index + 1) % len(steps)]
        monkeypatch.setattr(goal_turn, '_goal_judgments', Mock(return_value={
            project: goal_turn._GoalJudgment(missing_evidence='Build outcome', next_step=next_step),
        }))
        actor._session_messages = [{'role': 'tool', 'name': 'terminal', 'tool_call_id': call_id,
                                    'content': 'No confirmed build result is available.'}]

        def model(_prompt, **_kwargs):
            _goal(actor, 'checkpoint', project_id=project, evidence_tool_call_id=call_id, next_step=next_step)
            return {'completed': True, 'final_response': goal_turn.finish_segment(actor, 'No result.')}

        assert deliver_main(actor, inbox, pending, Mock(side_effect=model))['_notification_reply_muted']

    saved = _snapshot(actor, project)
    assert saved['status'] == 'pending' and saved['revision'] == saved['checked_revision'] == 1
    assert inbox.pending() == []


@pytest.mark.parametrize('restart_on_resume', [False, True])
def test_resume_recovers_the_reply_that_reached_the_inbox_before_pause(source, monkeypatch, restart_on_resume):
    from cron.jobs import get_job, update_job
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron
    from tools.delegate_project_goal import set_goal_paused
    from tools.delegate_project_store import ProjectStore

    actor, project, _, after, _ = source
    _, job_id = bind(source)
    reply = row('reply-1', 'supplier-1', after + timedelta(minutes=1))
    read = Mock(return_value=body(reply))
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    assert admit_cron(get_job(job_id), execution_id='before-pause')[0]

    inbox = HomeInbox()
    [before_pause] = inbox.pending()
    with ProjectStore(actor) as store:
        deadline = datetime.fromisoformat(store.read_workspace(project)['external_wait']['deadline'])
    _goal(actor, 'pause', project_id=project)
    proceed = Mock(return_value={'completed': True})
    assert deliver_main(actor, inbox, before_pause, proceed)['_notification_reply_muted']
    proceed.assert_not_called()
    assert inbox.pending() == []

    update_job(job_id, {'next_run_at': (deadline + timedelta(minutes=1)).isoformat()})
    if restart_on_resume:
        with ProjectStore(actor) as store:
            # The durable resume can commit just before the gateway exits.
            set_goal_paused(store, project, False)
        goal_turn.recover()
    else:
        _goal(actor, 'resume', project_id=project)
    [after_resume] = inbox.pending()
    assert after_resume[0] != before_pause[0]
    assert 'reply-1' in after_resume[1]
    read.assert_called_once()
    deliver_main(actor, inbox, after_resume, proceed)
    proceed.assert_called_once()
    assert 'no_change' in admit_cron(get_job(job_id), execution_id='repeat')[1]
    assert inbox.pending() == []


def test_message_from_other_thread_does_not_match_a_conversation_wait(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone import events

    _, _, probe, after, config = source
    changed = json.loads(config.read_text())
    operation = changed['mcp_servers']['fixture-chat']['hermes_api']['operations'][0]
    operation['path'] = 'messages'
    operation['name'] = 'workspace_history'
    operation['description'] = 'Reads all threads from a workspace'
    config.write_text(json.dumps(changed))
    probe.update(operation='workspace_history', arguments={})
    _, job_id = bind(source)
    wrong_conversation = {**row('wrong-reply', 'supplier-1', after + timedelta(minutes=1)), 'thread': 'different-thread'}
    monkeypatch.setattr('tools.connectors.http_api.response_data', Mock(return_value=body(wrong_conversation)))
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    events.admit_cron(get_job(job_id), execution_id='different-thread')
    assert HomeInbox().pending() == []


def test_goal_wake_diagnosis_does_not_enter_panel_history_during_human_turn(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.main_history import history_page
    from hermes_runtime.sessions import HOME_SESSION_ID

    home, db = main_home
    actor = _actor(db)
    goal = _open(actor, 'Check the report')
    _goal(actor, 'checkpoint', project_id=goal, deliverable='Saved report draft.')
    verifier_text = 'INTERNAL_EVIDENCE_9843'
    result = Mock(return_value={
        goal: goal_turn._GoalJudgment(missing_evidence=verifier_text, next_step='Read the build receipt'),
    })
    monkeypatch.setattr('agent.prompt_builder._probe_remote_backend', lambda *_: 'Linux Docker fixture')
    monkeypatch.setenv('HERMES_GATEWAY_SESSION', '1')
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    monkeypatch.setattr(goal_turn, '_goal_judgments', result)
    goal_turn.finish_segment(actor, 'Queued.')
    answers = iter([_response(args={'action': 'project_goal', 'op': 'list'})])
    _models(monkeypatch, lambda *_args, **_kwargs: next(answers, _response('Answered the user.')), result)
    agent = _agent(db)
    try:
        response = agent.run_conversation('What projects are there?')
    finally:
        agent.close()
    assert response['final_response'] == 'Answered the user.'
    projection = history_page(db, HOME_SESSION_ID, home)
    assert verifier_text not in json.dumps(projection)
    assert 'What projects are there?' in json.dumps(projection)
    assert verifier_text in json.dumps(db.get_messages(HOME_SESSION_ID))


def test_pausing_the_timer_during_its_get_prevents_publication(source, monkeypatch):
    from cron.jobs import get_job, pause_job, resume_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone import events

    _, _, _, after, _ = source
    _, job_id = bind(source)

    def pause_during_get(_request):
        assert pause_job(job_id)['state'] == 'paused'
        return body(row('reply-1', 'supplier-1', after + timedelta(minutes=1)))

    monkeypatch.setattr('tools.connectors.http_api.response_data', pause_during_get)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    events.admit_cron(get_job(job_id), execution_id='paused-during-get')
    assert get_job(job_id)['state'] == 'paused'
    assert HomeInbox().pending() == []
    resume_job(job_id)
    assert events.admit_cron(get_job(job_id), execution_id='after-timer-resume')[0]
    [accepted] = HomeInbox().pending()
    assert 'reply-1' in accepted[1]


def test_replacing_the_account_behind_an_identical_get_url_requires_rebinding(source, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone import events

    _, _, _, after, config = source
    changed = json.loads(config.read_text())
    entry = changed['mcp_servers']['fixture-chat']
    entry['hermes_api']['auth'] = {'type': 'bearer'}
    variable = 'HERMES_INTEGRATION_0123456789AB_API_KEY'
    entry['hermes_api_headers'] = {'Authorization': 'Bearer ${' + variable + '}'}
    config.write_text(json.dumps(changed))

    secrets = config.parent / '.env'
    secrets.write_text(variable + '=fixture-first-account\n')
    _, job_id = bind(source)
    secrets.write_text(variable + '=fixture-second-account\n')

    def second_account(request):
        assert request.get_header('Authorization') == 'Bearer fixture-second-account'
        return body(row('wrong-account-reply', 'supplier-1', after + timedelta(minutes=1)))

    read = Mock(side_effect=second_account)
    monkeypatch.setattr('tools.connectors.http_api.response_data', read)
    monkeypatch.setattr('hermes_runtime.adapters.state.main_gateway_origin', dict)
    events.admit_cron(get_job(job_id), execution_id='after-account-replacement')
    assert HomeInbox().pending() == []
    read.assert_not_called()
