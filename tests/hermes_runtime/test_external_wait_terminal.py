"""Terminal AGE-6 transitions use the owning native store and ordinary tick."""

import json
from datetime import datetime, timedelta
from threading import Event
from unittest.mock import Mock

import pytest

from tests.hermes_runtime.test_external_wait_timer import _timer, _wait
from tests.hermes_runtime.test_main_schedule_tools import _tool
from tests.tools.test_delegate_external_wait import _call

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.fixture
def bound_wait(main_home):
    from cron.jobs import get_job

    _, db = main_home
    actor, ids, definition = _wait(db)
    job = get_job(_timer(actor, definition)['job_id'])
    neighbor = get_job(_tool('create', schedule='every 5h', prompt='Unrelated reminder')['job_id'])
    return actor, ids, job, neighbor


def _saved(actor, ids):
    from tools.delegate_project_store import ProjectStore

    with ProjectStore(actor) as store:
        project = store.get(ids['project_id'])
    return project['archived'], json.loads(project['workspace_json'])['external_wait']


def _assert_closed(actor, ids, job, neighbor, reason, *, archived=False):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron

    is_archived, wait = _saved(actor, ids)
    assert bool(is_archived) is archived
    assert (wait['state'], wait['close_reason'], wait['wait_id'], wait['job_id']) == (
        'CLOSED', reason, ids['wait_id'], job['id'])
    assert get_job(job['id']) is None
    assert get_job(neighbor['id']) == neighbor
    assert not admit_cron(job, execution_id='inert-stale-fire')[0]
    assert HomeInbox().pending() == []


def _tick_at(monkeypatch, when):
    from cron import jobs, scheduler, scheduler_tick

    monkeypatch.setattr(jobs, '_hermes_now', lambda: when)
    monkeypatch.setattr(scheduler, '_hermes_now', lambda: when)
    monkeypatch.setattr(scheduler_tick, '_pre_dispatch_housekeeping', Mock())
    for name in ('_maybe_run_worktree_maintenance', '_sweep_mcp_orphans', '_sweep_stale_inflight_for_tick'):
        monkeypatch.setattr(scheduler, name, Mock())
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', Mock(return_value=None))
    dispatch = Mock(side_effect=AssertionError('No terminal wait should be dispatched'))
    monkeypatch.setattr(scheduler, '_submit_with_guard', dispatch)
    assert scheduler.tick(verbose=False) == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize('paused', [False, True])
def test_ordinary_tick_retires_expired_timer_even_when_paused(bound_wait, monkeypatch, paused):
    actor, ids, job, neighbor = bound_wait
    if paused:
        assert _tool('pause', job_id=job['id'])['success']
    _, wait = _saved(actor, ids)
    deadline = datetime.fromisoformat(wait['deadline'])
    _tick_at(monkeypatch, deadline)
    _assert_closed(actor, ids, job, neighbor, 'expired')
    _tick_at(monkeypatch, deadline + timedelta(minutes=1))
    _assert_closed(actor, ids, job, neighbor, 'expired')


def _control(actor, action, ids):
    from run_agent import AIAgent

    return json.loads(AIAgent._dispatch_delegate_task(actor, {'action': action, 'subagent_id': ids['project_id']}))


def test_project_pause_and_run_cancel_are_not_wait_cancel_or_access_loss(bound_wait):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron

    actor, ids, job, neighbor = bound_wait
    event = Event()
    event.set()
    assert admit_cron(dict(job), cancel_event=event, execution_id='inert-cancelled-fire')[3] == 'cancelled'
    assert _control(actor, 'stop', ids)['status'] == 'paused'
    assert not admit_cron(dict(job), execution_id='inert-paused-project')[0]
    denied = _call(actor, op='close', reason='access_lost', **ids)
    assert denied.get('error'), denied
    assert _saved(actor, ids)[1]['state'] == 'BOUND'
    assert get_job(job['id']) == job
    assert HomeInbox().pending() == []

    for _ in range(2):
        assert _call(actor, op='close', reason='cancelled', **ids)['state'] == 'CLOSED'
        _assert_closed(actor, ids, job, neighbor, 'cancelled')


def test_native_archive_retires_exact_wait_after_a_temporary_pause(bound_wait):
    actor, ids, job, neighbor = bound_wait
    assert _control(actor, 'stop', ids)['status'] == 'paused'
    for _ in range(2):
        assert _control(actor, 'archive', ids)['status'] == 'archived'
        _assert_closed(actor, ids, job, neighbor, 'closed', archived=True)


def test_expiry_retries_unconfirmed_removal_without_wake(bound_wait, monkeypatch):
    from cron import jobs
    from hermes_runtime.adapters.inbox import HomeInbox

    actor, ids, job, neighbor = bound_wait
    deadline = datetime.fromisoformat(_saved(actor, ids)[1]['deadline'])
    with monkeypatch.context() as failed:
        failed.setattr(jobs, 'remove_job', Mock(return_value=False))
        _tick_at(failed, deadline)
    assert _saved(actor, ids)[1]['state'] == 'CLOSING'
    assert jobs.get_job(job['id']) is not None
    assert jobs.get_job(neighbor['id']) == neighbor
    assert HomeInbox().pending() == []

    _tick_at(monkeypatch, deadline + timedelta(minutes=1))
    _assert_closed(actor, ids, job, neighbor, 'expired')


def test_expiry_does_not_remove_when_project_and_timer_disagree(bound_wait, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from tools.delegate_project_store import ProjectStore

    actor, ids, job, neighbor = bound_wait
    deadline = datetime.fromisoformat(_saved(actor, ids)[1]['deadline'])
    with ProjectStore(actor) as store:
        store.mutate_workspace(ids['project_id'], lambda ws, _conn: ws['external_wait'].update(job_id=neighbor['id']))
    _tick_at(monkeypatch, deadline)
    assert _saved(actor, ids)[1]['state'] == 'BOUND'
    assert get_job(job['id']) is not None
    assert get_job(neighbor['id']) == neighbor
    assert HomeInbox().pending() == []
