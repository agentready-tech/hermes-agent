"""Native coordination mail and worker reports respect their Project Goal's lifecycle."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_runtime.adapters import goal_turn
from hermes_runtime.sessions import HOME_SESSION_ID
from tests.hermes_runtime.test_child_goals import _child_actor
from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_panel_assignments import _call_panel, _dispatch, _worker
from tests.hermes_runtime.test_panel_main_bootstrap import resume, runtime
from tests.hermes_runtime.test_project_goal import _actor, _agent, _consume, _goal, _models, _open, _snapshot, _wake
from tests.tools.test_delegate_mail_live import _response
from tools.delegate_mail import mail_action, publish_pending_mail
from tools.delegate_mail_store import MailStore
from tools.delegate_mail_work import MailboxDeferred, ensure_mail_input, stage_mail_input
from tools.delegate_project_goal import read_goal
from tools.delegate_project_store import ProjectStore, project_input_goal_id

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools', 'tests.hermes_runtime.test_shared_profile_host']


def _mail_row(db, identity):
    return db._read_one('SELECT * FROM delegation_mail WHERE message_id=?', (identity,))


def _delivery(store, worker, db, expected, *, goal_id=None):
    claimed = store.claim(worker)
    assert project_input_goal_id(claimed) == goal_id
    receiver = SimpleNamespace(session_id=worker['session_id'], _session_db=db, _project_input_goal_id=goal_id)
    content, metadata = stage_mail_input(receiver, claimed, [])
    assert metadata['agent_message_ids'] == [expected]
    db.append_message(receiver.session_id, 'user', content, platform_message_id=claimed['message_id'],
                      display_kind='agent_message', display_metadata=metadata)
    with MailStore(receiver) as mail:
        mail.acknowledge_recorded()
    store.settle(worker, claimed, {'status': 'completed'})
    assert _mail_row(db, expected)['delivered_at'] is not None
    return metadata


def _assert_stale_wake(actor, db, identity):
    saved = db._read_one('SELECT event_json FROM async_delegations WHERE delegation_id=?', (identity,))
    early = json.loads(saved['event_json'])
    token = goal_turn.admitted_input.set({'admission_id': 'mail-wake-admission', 'payload': {
        'local_automation_v1': {'identity': json.dumps(['async_delegation', identity, ''])},
    }})
    try:
        proceed = Mock(side_effect=AssertionError('Stale mail must not call the model'))
        result = goal_turn.run_segment(actor, SimpleNamespace(), proceed, early['summary'])
        assert result['completed'] and result['_notification_reply_muted']
        proceed.assert_not_called()
    finally:
        goal_turn.admitted_input.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('already_claimed', [False, True])
async def test_queued_mail_is_held_during_pause_and_resumes_with_original_delivery(main_home, monkeypatch, already_claimed):
    home, db = main_home
    main = _actor(db)
    paused = _open(main, 'Held task', slot=1)
    neighbor = _open(main, 'Separate task', slot=2)
    with ProjectStore(main) as projects:
        worker = _worker(projects, db, 'shared worker', paused)
        _dispatch(projects, db, worker, paused, 'First accepted work')
        _dispatch(projects, db, worker, neighbor, 'Most recent, unrelated work')

    # The gateway has admitted an old route whose last known owner was another Goal.
    monkeypatch.setattr(MailStore, 'routing_origin', lambda *_: {'wake': True, 'goal_project_id': neighbor})
    main._agentready_active_turn.goal_segment.touch(paused)
    first = mail_action(main, 'message', worker['subagent_id'], 'Hold for the exact task', 'first')['message_id']
    receiver = SimpleNamespace(session_id=worker['session_id'], _session_db=db)
    with MailStore(receiver) as inbox:
        original_delivery = inbox.delivery(set())['message_id']
    original_hash = _mail_row(db, first)['content_hash']
    if already_claimed:
        with ProjectStore(main) as projects:
            ensure_mail_input(main, projects, worker)
            early_claim = projects.claim(worker)
            assert project_input_goal_id(early_claim) == paused

    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    await _call_panel(connection, 'control', project_id=paused, action='pause')
    _assert_stale_wake(main, db, first)
    if already_claimed:
        with pytest.raises(MailboxDeferred):
            stage_mail_input(receiver, early_claim, [])
        with ProjectStore(main) as projects:
            projects.release(early_claim)
    with ProjectStore(main) as projects:
        ensure_mail_input(main, projects, worker)
        assert projects.claim(worker) is None

    # Paused work on the same recipient cannot hold either a neighboring Goal or
    # a legacy conversation whose copied route mentions the paused Goal.
    main._agentready_active_turn.goal_segment.touch(neighbor)
    other = mail_action(main, 'message', worker['subagent_id'], 'Neighbor still works', 'other')['message_id']
    copy_of_route = SimpleNamespace(session_id=main.session_id, _session_db=db)
    monkeypatch.setattr(MailStore, 'routing_origin', lambda *_: {'wake': False, 'goal_project_id': paused})
    legacy = mail_action(copy_of_route, 'message', worker['subagent_id'], 'Unscoped mail', 'legacy')['message_id']
    with ProjectStore(main) as projects:
        ensure_mail_input(main, projects, worker)
        _delivery(projects, worker, db, other, goal_id=neighbor)
        ensure_mail_input(main, projects, worker)
        _delivery(projects, worker, db, legacy)
        assert projects.claim(worker) is None
    assert _mail_row(db, first)['delivered_at'] is None

    wakes = Mock()
    monkeypatch.setattr('hermes_runtime.standalone.events._admit_scheduled_prompt', wakes)
    await _call_panel(connection, 'control', project_id=paused, action='resume')
    assert publish_pending_mail() == 0  # The original native event cannot be reinserted.
    with ProjectStore(main) as projects:
        wake = read_goal(projects, paused)['wake_id']
        assert wake and wakes.call_args.args[1:] == (wake, {'project_id': paused})
        ensure_mail_input(main, projects, worker)
        assert _delivery(projects, worker, db, first, goal_id=paused)['agent_mail_delivery_id'] == original_delivery
        assert projects.claim(worker) is None
    recorded = _mail_row(db, first)
    assert (recorded['delivery_id'], recorded['content_hash'], recorded['content']) == (original_delivery, original_hash, '')


@pytest.mark.asyncio
async def test_cancelled_goal_mail_never_reopens_an_existing_or_new_wake(main_home, monkeypatch):
    home, db = main_home
    main = _actor(db)
    goal = _open(main, 'Cancelled task')
    with ProjectStore(main) as projects:
        worker = _worker(projects, db, 'worker', goal)
    monkeypatch.setattr(MailStore, 'routing_origin', lambda *_: {'wake': True})
    first = mail_action(main, 'message', worker['subagent_id'], 'Sent before cancel', 'first')['message_id']
    with ProjectStore(main) as projects:
        ensure_mail_input(main, projects, worker)
    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    await _call_panel(connection, 'control', project_id=goal, action='cancel')
    _assert_stale_wake(main, db, first)

    late = mail_action(main, 'message', worker['subagent_id'], 'Sent by a late worker', 'late')['message_id']
    assert publish_pending_mail() == 0
    assert db._read_one('SELECT 1 FROM async_delegations WHERE delegation_id=?', (late,)) is None
    with ProjectStore(main) as projects:
        ensure_mail_input(main, projects, worker)
        assert projects.claim(worker) is None
    receiver = SimpleNamespace(session_id=worker['session_id'], _session_db=db)
    with MailStore(receiver) as inbox:
        assert inbox.pending() == []
    assert _mail_row(db, first)['delivered_at'] is None


@pytest.mark.asyncio
async def test_resumed_mail_wake_is_retired_when_independent_turn_delivers_reserved_id(main_home, monkeypatch):
    from tools.delegate_project_recovery import publish_recovery_wakeups

    home, db = main_home
    main = _actor(db)
    goal = _open(main, 'Resume only if mail is pending')
    monkeypatch.setattr('tools.delegate_projects._origin', lambda: {'wake': True})
    monkeypatch.setattr(MailStore, 'routing_origin', lambda *_: {'wake': True})
    monkeypatch.setattr('tools.delegate_project_recovery._owner_liveness', lambda: lambda *_: False)
    with ProjectStore(main) as projects:
        worker = _worker(projects, db, 'worker', goal)
        receipt = mail_action(main, 'message', worker['subagent_id'], 'Deliver this once', 'once')['message_id']
        ensure_mail_input(main, projects, worker)
        old_wake = projects.claim(worker)
        receiver = SimpleNamespace(session_id=worker['session_id'], _session_db=db)
        _, metadata = stage_mail_input(receiver, old_wake, [])
        reserved = old_wake['message_id']
        assert metadata['agent_mail_delivery_id'] == reserved
        projects.release(old_wake)
    assert publish_recovery_wakeups() == 1
    recovery = db._read_one("SELECT delegation_id FROM async_delegations WHERE delegation_id LIKE 'project-recovery-%'")[0]

    _, connection = runtime(home, db, monkeypatch)
    await resume(connection)
    await _call_panel(connection, 'control', project_id=goal, action='pause')
    with ProjectStore(main) as projects:
        independent = projects.enqueue(worker, 'Independent task', {'wake': False})
        active = projects.claim(worker)
        assert active['message_id'] == independent
    await _call_panel(connection, 'control', project_id=goal, action='resume')

    with MailStore(receiver) as inbox:
        delivered = inbox.delivery(set())
        assert delivered['message_id'] == reserved
        assert delivered['display_metadata'] == {'agent_message_ids': [receipt]}
        db.append_message(receiver.session_id, 'user', delivered['content'], platform_message_id=reserved,
                          display_kind=delivered['display_kind'], display_metadata=delivered['display_metadata'])
        inbox.acknowledge_recorded()
    assert _mail_row(db, receipt)['delivered_at'] is not None
    with pytest.raises(MailboxDeferred):
        stage_mail_input(receiver, old_wake, [])
    _assert_stale_wake(main, db, recovery)
    with ProjectStore(main) as projects:
        projects.settle(worker, active, {'status': 'completed'})
        assert publish_recovery_wakeups() == 0
        ensure_mail_input(main, projects, worker)
        assert projects.claim(worker) is None
        retired = projects.message(reserved)
        assert retired['state'] == 'cancelled' and retired['published'] and retired['result_json'] is None


def test_mail_recovery_keeps_admitted_stage_and_pending_descendant(main_home):
    _, db = main_home
    main = _actor(db)
    goal = _open(main, 'Deliver through the coordinator')
    with ProjectStore(main) as projects:
        coordinator = _worker(projects, db, 'coordinator', goal)
    db.patch_session_model_config(coordinator['session_id'], {'_delegate_from': main.session_id})
    receiver = SimpleNamespace(session_id=coordinator['session_id'], _session_db=db)
    with ProjectStore(receiver) as projects:
        leaf = _worker(projects, db, 'leaf', goal)
    db.patch_session_model_config(leaf['session_id'], {'_delegate_from': receiver.session_id})
    receipt = mail_action(main, 'message', leaf['subagent_id'], 'For the leaf', 'leaf')['message_id']
    with ProjectStore(main) as projects:
        ensure_mail_input(main, projects, coordinator)
        wake = projects.claim(coordinator)
        assert stage_mail_input(receiver, wake, []) == (wake['content'], None)
        assert _mail_row(db, receipt)['delivered_at'] is None
    with ProjectStore(receiver) as projects:
        ensure_mail_input(receiver, projects, leaf)
        wake = projects.claim(leaf)
        target = SimpleNamespace(session_id=leaf['session_id'], _session_db=db)
        content, metadata = stage_mail_input(target, wake, [])
        db.append_message(target.session_id, 'user', content, platform_message_id=wake['message_id'],
                          display_kind='agent_message', display_metadata=metadata)
        with MailStore(target) as mail:
            mail.acknowledge_recorded()
        projects.release(wake)
        assert projects.claim(leaf)['message_id'] == wake['message_id']


BLOCKED_CHILD_NEXT_STEP = "Review the saved report and record the outstanding approval separately."


@pytest.fixture
def blocked_child_assignment(main_home):
    return _assignment_with_child(main_home[1])


def _assignment_with_child(db):
    main = _actor(db)
    root = _open(main, "Review the finished work and collect its approval")
    with ProjectStore(main) as store:
        coordinator = store.create("coordinator", "Coordinate the review", "")
    owner = _child_actor(db, coordinator, root)
    db.create_session(owner.session_id, source="delegate", parent_session_id=HOME_SESSION_ID)
    child = _open(owner, "Confirm the external approval")
    return main, root, coordinator, owner, child


def _block(owner, child):
    return _goal(owner, "block", project_id=child, blocker="Awaiting the user's authorization",
                 unblock="The user grants access", notify=True)


def _archive_executor_with_saved_wake(owner, child):
    from tools.delegate_project_goal import mutate_goal

    def schedule(goal):
        goal.update(status="ready", wake_id=f"{child}:1", wake_step="Verify the saved spec")
        return goal

    with ProjectStore(owner) as store:
        worker = store.create("spec", "Write the spec", "")
        store.enqueue(worker, "Write the spec", {"goal_project_id": child})
        assert store.settle(worker, store.claim(worker), {"status": "completed", "summary": "Spec written."})
        mutate_goal(store, child, schedule)
        store.pause(worker["subagent_id"], archive=True)


def _save_running_step(main, root, *, stale=False):
    from tools.delegate_project_goal import add_evidence, mutate_goal

    def save(goal):
        add_evidence(goal, "project-msg-saved", "worker report", "Review evidence saved before recovery.")
        goal.update(status="running", checked_revision=goal["revision"], next_step=BLOCKED_CHILD_NEXT_STEP,
                    step_revision=0 if stale else goal["revision"])
        return goal

    with ProjectStore(main) as store:
        mutate_goal(store, root, save)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["explicit", "stranded", "archived_executor", "active_sibling"])
async def test_native_report_wakes_parent_once(
    main_home, blocked_child_assignment, monkeypatch, case,
):
    from hermes_runtime.adapters.state import main_gateway_origin
    from tools.delegate_projects import _publish_completed

    home, db = main_home
    main, root, coordinator, owner, child = blocked_child_assignment
    monkeypatch.setattr("agent.prompt_builder._probe_remote_backend", lambda *_: "Linux Docker fixture")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    runner, authority = await gateway_owner(home, db, monkeypatch, "blocked-child-report")
    explicit_block = case in {"explicit", "active_sibling"}
    if case == "archived_executor":
        _archive_executor_with_saved_wake(owner, child)
    if case == "active_sibling":
        stage = _open(owner, "Complete the independent review", slot=2)
        with ProjectStore(owner) as store:
            worker = store.create("reviewer", "Review the separate change", "")
            store.enqueue(worker, "Review the separate change", {"goal_project_id": stage})
    with ProjectStore(main) as store:
        message_id = store.enqueue(coordinator, "Return the review findings",
                                   {**main_gateway_origin(), "goal_project_id": root, "wake": True})
        message = store.claim(coordinator)
        owner._project_goal_segment.input_id = message_id
        if explicit_block:
            _block(owner, child)
        receipt = {"status": "completed", "summary": "QA has started. Approval is outstanding."}
        assert store.settle(coordinator, message, receipt)
        assert not receipt.get("goal_continuing")
        _publish_completed(store, coordinator, store.message(message_id), receipt)

    def verify(goals):
        report = goals[root]["evidence"][message_id]
        assert report["kind"] == "worker report"
        assert "Approval is outstanding" in report["value"]
        if explicit_block:
            assert "Awaiting the user's authorization" in report["value"]
            assert "The user grants access" in report["value"]
        return {root: goal_turn._GoalJudgment(missing_evidence="The approval receipt", next_step=BLOCKED_CHILD_NEXT_STEP)}

    response = "Approval is still needed. I’ll check the recorded decision."
    model = Mock(return_value=_response(response))
    _models(monkeypatch, model, verify)
    outputs = []

    async def execute(event, *_args):
        agent = _agent(db)
        try:
            result = await asyncio.to_thread(agent.run_conversation, event.text)
            outputs.append(result["final_response"])
            return result["final_response"]
        finally:
            agent.close()

    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)
    await _consume(runner, authority, home, _wake(db, message_id))
    saved = _snapshot(main, root)
    assert saved["next_step"] == BLOCKED_CHILD_NEXT_STEP and message_id in saved["evidence"]
    if case == "active_sibling":
        assert saved["status"] == "running" and saved["wake_id"] is None
        assert outputs == [response]
    else:
        assert saved["status"] == "ready" and saved["wake_id"]
        await _consume(runner, authority, home, _wake(db, "main-goal-"))
        assert outputs == [response, ""]
        assert _snapshot(main, root)["status"] == "pending"
    goal_turn.recover()
    assert db._read_one(
        "SELECT 1 FROM delegation_mail WHERE json_extract(origin_json,'$.child_goal_event.project_id')=?",
        (child,),
    ) is None
    expected_status = {"explicit": "blocked", "active_sibling": "blocked",
                       "stranded": "pending", "archived_executor": "ready"}
    assert _snapshot(owner, child)["status"] == expected_status[case]


@pytest.mark.parametrize("condition", [
    "stalled", "archived executor", "active child", "paused child", "queued dispatch", "stale step",
])
def test_recovery_uses_only_a_verified_parent_step(blocked_child_assignment, monkeypatch, condition):
    from hermes_runtime.adapters.inbox import HomeInbox

    main, root, coordinator, owner, child = blocked_child_assignment
    if condition == "archived executor":
        _archive_executor_with_saved_wake(owner, child)
    else:
        _block(owner, child)
    if condition == "active child":
        active = _open(owner, "Complete the other review stage", slot=2)
        with ProjectStore(owner) as store:
            worker = store.create("reviewer", "Complete the other review stage", "")
            store.enqueue(worker, "Review the changes", {"goal_project_id": active})
    if condition == "paused child":
        paused = _open(owner, "Wait for the separate stage to be resumed", slot=2)
        _goal(owner, "pause", project_id=paused)
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)

    if condition == "queued dispatch":
        with ProjectStore(main) as store:
            store.enqueue(coordinator, "Finish the other review stage", {"goal_project_id": root, "wake": True})
    _save_running_step(main, root, stale=condition == "stale step")
    before = _snapshot(main, root)
    goal_turn.recover()
    after = _snapshot(main, root)
    inbox = HomeInbox()
    if condition != "stalled":
        assert after == before and inbox.pending() == []
    else:
        assert after["status"] == "ready" and after["wake_id"]
        [saved] = inbox.pending()
        assert BLOCKED_CHILD_NEXT_STEP in saved[1]
        goal_turn.recover()
        assert inbox.pending() == [saved]
    assert _snapshot(owner, child)["status"] == ("ready" if condition == "archived executor" else "blocked")


@pytest.mark.asyncio
async def test_shared_gateway_replays_stalled_goal_without_a_viewer(shared_homes, monkeypatch):
    from queue import Queue

    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.run_runtime import _build_profile_authority
    from gateway.session_authorities import SessionAuthorities, owner_scope
    from tools.process_registry import process_registry

    root, homes = shared_homes
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr(process_registry, "completion_queue", Queue())
    runner = GatewayRunner(GatewayConfig(multiplex_profiles=True, sessions_dir=root / "sessions"))
    runner._primary_profile_name = "default"
    runner._served_profile_homes = homes
    runner.session_authorities = SessionAuthorities(root, multiplexed=True)
    runner.session_runtime_descriptor = {"instance_id": "shared-goal-recovery"}
    try:
        alpha = await _build_profile_authority(runner, "alpha", homes["alpha"], register=False)
        bravo = await _build_profile_authority(runner, "bravo", homes["bravo"], register=False)
        with owner_scope(alpha):
            main, assignment, _, owner, child = _assignment_with_child(alpha.db)
            _block(owner, child)
            _save_running_step(main, assignment)

        await asyncio.to_thread(runner._sweep_orphaned_completion_ledgers)
        with owner_scope(alpha):
            saved = _snapshot(main, assignment)
            assert saved["status"] == "ready" and saved["wake_id"]
            assert _snapshot(owner, child)["status"] == "blocked"
        wake = _wake(alpha.db, "main-goal-")
        assert wake["goal_project_id"] == assignment and wake["session_key"] == "agent:alpha:agentready:home"
        assert f"Saved next step: {BLOCKED_CHILD_NEXT_STEP}" in wake["summary"]
        assert not bravo.db._read_all("SELECT 1 FROM async_delegations")

        await asyncio.to_thread(runner._sweep_orphaned_completion_ledgers)
        assert _wake(alpha.db, "main-goal-") == wake
        assert sum(e["delegation_id"] == wake["delegation_id"] for e in process_registry.completion_queue.queue) == 1
    finally:
        runner.session_store.close_all_db_handles()
