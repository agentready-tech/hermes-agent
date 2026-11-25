"""A Bot Mode assignment crosses durable role Goals and resumes its exact worker."""

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hermes_runtime.adapters.goal_turn import _GoalJudgment
from hermes_runtime.sessions import HOME_SESSION_ID
from tests.hermes_runtime.test_external_wait_timer import _isolated_tick
from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_project_goal import _actor, _agent, _consume, _goal, _models, _open, _snapshot
from tests.tools.test_delegate_mail_live import _response
from tests.waiting import async_wait_until
from tools.delegate_project_store import ProjectStore

pytest_plugins = ["tests.hermes_runtime.test_main_schedule_tools"]

STAGES = ("Intake", "Lookup", "Verify", "Confirm")
CRITERIA = {
    "Intake": "Record the appointment requirements",
    "Lookup": "Find the existing request reference",
    "Verify": "Verify the request matches the appointment requirements",
    "Confirm": "Return the confirmed appointment for the verified request",
}
APPOINTMENT = "Confirm the appointment"
UNAVAILABLE = "No suitable slots are available."


def _report_values(evidence):
    return {identity: json.loads(item["value"])["summary"]
            for identity, item in evidence.items() if item["kind"] == "worker report"}


def _delegate_actions(payload):
    tool = next(tool["function"] for tool in payload["tools"] if tool["function"]["name"] == "delegate_task")
    return set(tool["parameters"]["properties"]["action"]["enum"])


def _as_goal(op, **fields):
    return _response(args={"action": "project_goal", "op": op, **fields})


def _spawn(key, goal, *, background=False):
    return _response(args={"action": "spawn", "background": background,
                           "tasks": [{"project": key, "goal": goal}]})


def _child_actor(db, coordinator, parent_goal):
    from hermes_runtime.adapters.goal_turn import GoalSegment

    return SimpleNamespace(session_id=coordinator["session_id"], _session_db=db,
                           _project_goal_segment=GoalSegment("role-assignment", True,
                               parent_goal_id=parent_goal, orchestrator_id=coordinator["subagent_id"]))


def _pending_event(db, match):
    row = db._read_one(
        """SELECT a.event_json FROM async_delegations a LEFT JOIN delegation_mail m ON m.message_id=a.delegation_id
           WHERE a.delivery_state='pending' AND a.event_json IS NOT NULL AND (
               json_extract(m.origin_json, '$.child_goal_event.event_id')=? OR a.delegation_id=?)
           ORDER BY a.dispatched_at LIMIT 1""", (match, match),
    )
    return json.loads(row[0]) if row else None


@pytest.mark.asyncio
@pytest.mark.parametrize("return_path", ["timer", "mail"])
async def test_role_goals_resume_the_same_worker_after_restart(main_home, monkeypatch, return_path):
    from cron.executions import latest_execution
    from cron.jobs import get_job
    from gateway.session_contract import SessionRef
    from gateway.session_controls import AuthorityConnection
    from hermes_runtime.adapters.goal_turn import recover
    from hermes_runtime.adapters.state import main_gateway_origin
    from hermes_runtime.standalone.events import admit_cron
    from tools.approval_context import reset_current_session_key, set_current_session_key
    from tools.delegate_mail import mail_action
    from tools.delegate_project_recovery import publish_recovery_wakeups

    home, db = main_home
    config = home / "config.yaml"
    config.write_text(config.read_text() + "delegation:\n  worktree_isolation: false\n")
    monkeypatch.setattr("agent.prompt_builder._probe_remote_backend", lambda *_: "Linux Docker fixture")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    runner, authority = await gateway_owner(home, db, monkeypatch, "child-goals")
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: True), {
        "user_id": "fixture-panel", "provider": "local", "profile_id": str(home),
        "instance_id": "child-goals", "native_bootstrap": True,
    })
    mounted = await connection.dispatch({"id": 1, "method": "session.resume",
        "params": {"session_id": HOME_SESSION_ID, "display_projection": True}})
    assert "error" not in mounted, mounted

    main_steps = iter(("open", "spawn", "ack", "aggregate"))
    coordinator_steps = deque((stage, op) for stage in STAGES for op in ("open", "spawn", "handoff"))
    coordinator_steps.extend(
        ("Confirm", op) for op in ("timer", "reject-old-evidence", "no-change", "verify", "aggregate")
    )
    calls, outputs, ids = [], [], {}
    checks, reports = [], {"Intake": "Appointment requirements recorded."}
    old_tool_id = "proof-from-prior-turn"
    deadline = (datetime.now(UTC) + timedelta(days=3)).isoformat()

    def verify(goals):
        return {pid: _GoalJudgment(evidence=tuple(ref for ref, report in _report_values(candidate["evidence"]).items()
                                                if report == reports.get(candidate["contract"]["outcome"])))
                for pid, candidate in goals.items()}

    def saved_report(agent, stage):
        saved = _snapshot(agent, ids[stage])
        assert saved["status"] == "done"
        [reference] = saved["final_evidence"]
        report = _report_values(saved["evidence"])[reference]
        assert report == reports[stage]
        return report

    def coordinator_response(agent, result):
        assert agent._project_goal_segment.parent_goal_id == ids["main"]
        stage, op = coordinator_steps.popleft()
        if op == "no-change":
            assert result["error"] == "No matching tool evidence was produced in this turn"
            return _response(UNAVAILABLE)
        assert "error" not in result, result
        if op == "open":
            if stage != "Intake":
                saved_report(agent, STAGES[STAGES.index(stage) - 1])
            return _as_goal("open", goal=stage, verification=CRITERIA[stage], slot=STAGES.index(stage) + 1)
        if op == "spawn":
            ids[stage] = result["project_id"]
            # Use the recorded upstream result when forming the next dispatch.
            context = f" for {saved_report(agent, 'Lookup')}" if stage in ("Verify", "Confirm") else ""
            return _spawn(stage, stage + context)
        if stage == "Confirm" and op == "handoff":
            ids["executor_session"] = result["projects"][0]["session_id"]
            return _response(args={"action": "external_wait", "op": "open", "project_id": ids["Confirm"],
                "mode": "resume_child", "reply_criterion": "A matching slot is available", "deadline": deadline,
                "return_path": "timer", "schedule": "every 15m", "prompt": "Check availability for the saved request"})
        if op == "timer":
            ids["timer"] = result["job_id"]
            return _response(UNAVAILABLE)
        if op == "reject-old-evidence":
            assert old_tool_id in agent._project_goal_segment.history_tool_results
            return _as_goal("checkpoint", project_id=ids["Confirm"], evidence_tool_call_id=old_tool_id)
        if op == "aggregate":
            reports[APPOINTMENT] = saved_report(agent, "Confirm")
            return _response(reports[APPOINTMENT])
        return _response("Stage recorded.")

    def worker_response(agent, messages):
        stage = db._read_one("SELECT project_key FROM delegated_projects WHERE subagent_id=?",
                             (agent._persistent_project_id,))[0]
        if stage == "Lookup":
            reports[stage] = f"Request {agent.session_id}"
        if stage in ("Verify", "Confirm"):
            assert reports["Lookup"] in json.dumps(messages)
        if stage == "Verify":
            reports[stage] = f"{reports['Lookup']} matches the requirements."
        if stage != "Confirm":
            return _response(reports[stage])
        checks.append(agent.session_id)
        if len(checks) == 1:
            return _response(UNAVAILABLE)
        source = "A matching slot is available" if return_path == "mail" and len(checks) == 3 else "Check availability for the saved request"
        assert source in messages[-1]["content"]
        if len(checks) == 2:
            return _response(UNAVAILABLE)
        reports[stage] = f"Appointment confirmed for {reports['Lookup']}."
        return _response(reports[stage])

    def model(agent, payload, *_args, **_kwargs):
        messages = payload["messages"]
        result = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else {}
        if agent.session_id != HOME_SESSION_ID:
            if agent._delegate_role == "orchestrator":
                assert {"project_goal", "external_wait"} <= _delegate_actions(payload)
                return coordinator_response(agent, result)
            return worker_response(agent, messages)
        assert "error" not in result, result
        assert {"project_goal", "external_wait"} <= _delegate_actions(payload)
        step = next(main_steps)
        calls.append(step)
        if step == "open":
            return _as_goal("open", goal=APPOINTMENT, verification="Confirmation includes the verified request reference")
        if step == "spawn":
            ids["main"] = result["project_id"]
            return _spawn("appointment", "Coordinate the appointment request", background=True)
        if step == "ack":
            ids["coordinator"] = result["projects"][0]["subagent_id"]
            ids["first_input"] = result["projects"][0]["message_id"]
            return _response("Принято.")
        assert reports[APPOINTMENT] in json.dumps(messages)
        return _response(reports[APPOINTMENT])

    async def execute(event, *_args):
        token = set_current_session_key(main_gateway_origin()["session_key"])
        agent = _agent(db)
        try:
            result = await asyncio.to_thread(agent.run_conversation, event.text)
            outputs.append(result["final_response"])
            return result["final_response"]
        finally:
            agent.close()
            reset_current_session_key(token)

    _models(monkeypatch, model, verify)
    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)
    submitted = await connection.dispatch({"id": 2, "method": "prompt.submit", "params": {
        "session_id": HOME_SESSION_ID, "submission_id": "confirm-appointment", "text": APPOINTMENT}})
    assert "error" not in submitted, submitted
    await authority._drain(SessionRef(str(home), HOME_SESSION_ID))

    async def consume(match):
        event = await async_wait_until(lambda: _pending_event(db, match), what=match)
        await _consume(runner, authority, home, event)

    await async_wait_until(
        lambda: db._read_one(
            "SELECT 1 FROM async_delegations WHERE delegation_id=? AND delivery_state='suppressed'",
            (ids["first_input"],),
        ),
        what="the waiting orchestrator's intermediate receipt settles without main admission",
    )
    assert _pending_event(db, ids["first_input"]) is None
    await async_wait_until(lambda: ids.get("timer"), what="native coordinator handoffs through confirmation")
    with ProjectStore(_actor(db)) as store:
        child_owner = _child_actor(db, store.get(ids["coordinator"]), ids["main"])
    await async_wait_until(lambda: _snapshot(child_owner, ids["Confirm"])["status"] == "waiting",
                           what="durably bound confirmation wait")
    assert calls == ["open", "spawn", "ack"]
    assert checks == [ids["executor_session"]]
    assert _snapshot(_actor(db), ids["main"])["status"] == "running"

    # A result in the orchestrator's prior transcript cannot prove a new checkpoint.
    db.append_message(child_owner.session_id, "assistant", None, tool_calls=[{
        "id": old_tool_id, "type": "function", "function": {"name": "terminal", "arguments": "{}"},
    }])
    db.append_message(child_owner.session_id, "tool", "Unrelated historical output", tool_name="terminal", tool_call_id=old_tool_id)

    job_id = ids["timer"]
    _isolated_tick(monkeypatch, job_id)
    first = latest_execution(job_id)["id"]
    # Both discovery and dispatch use the same durable mail after a gateway restart.
    runner, authority = await gateway_owner(home, db, monkeypatch, "child-goals-restarted")
    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)
    recover()
    publish_recovery_wakeups()
    assert get_job(job_id)["last_status"] == "admitted"
    first_event = f"{job_id}:{first}"
    await consume(first_event)
    await async_wait_until(lambda: len(checks) == 2, what="the saved worker handles the timer")
    assert admit_cron(get_job(job_id), execution_id=first)[0]
    assert checks == [ids["executor_session"]] * 2
    assert calls == ["open", "spawn", "ack"]

    if return_path == "timer":
        _isolated_tick(monkeypatch, job_id)
        await consume(f"{job_id}:{latest_execution(job_id)['id']}")
    else:
        _goal(child_owner, "select", project_id=ids["Confirm"])
        receipt = mail_action(child_owner, "message", ids["executor_session"], "A matching slot is available", "slot-open")
        assert mail_action(child_owner, "message", ids["executor_session"], "A matching slot is available", "slot-open") == receipt
        await consume(receipt["message_id"])

    def aggregate_input():
        row = db._read_one(
            """SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND
               json_extract(origin_json, '$.child_goal_event.event_id')=?""",
            (ids["coordinator"], f"{ids['Confirm']}:done"),
        )
        return row[0] if row else None

    aggregate = await async_wait_until(aggregate_input, what="the coordinator's aggregate of verified role results")
    await consume(aggregate)
    assert checks == [ids["executor_session"]] * 3
    assert calls == ["open", "spawn", "ack", "aggregate"]
    assert [output for output in outputs if output] == ["Принято.", reports[APPOINTMENT]]
    assert get_job(job_id) is None
    assert _snapshot(_actor(db), ids["main"])["status"] == "done"
    assert all(UNAVAILABLE not in (row["content"] or "") for row in db.get_messages(HOME_SESSION_ID))
    for stage in STAGES:
        state = _snapshot(child_owner, ids[stage])
        assert (state["status"], state["parent_goal_id"]) == ("done", ids["main"])
        assert list(_report_values(state["evidence"]).values()).count(reports[stage]) == 1


def _bind_timer(actor, project_id):
    from tools.delegate_external_wait import dispatch

    wait = json.loads(dispatch(actor, {
        "action": "external_wait", "op": "open", "project_id": project_id, "mode": "resume_child",
        "reply_criterion": "A matching slot is available", "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "return_path": "timer", "schedule": "every 15m", "prompt": "Check the saved appointment",
    }))
    assert wait["success"], wait
    return wait["job_id"]


def test_child_control_and_pending_inputs_are_scoped_by_parent_and_executor(main_home):
    from cron.jobs import get_job
    from hermes_runtime.standalone.events import admit_cron
    from tools.delegate_project_goal import close_goal, dispatch, mutate_goal

    _, db = main_home
    main = _actor(db)
    first, second = _open(main, "First assignment"), _open(main, "Second assignment", slot=2)
    with ProjectStore(main) as store:
        coordinator = store.create("coordinator", "Coordinate", "")
    owner = _child_actor(db, coordinator, first)
    other = _child_actor(db, coordinator, second)
    child = _open(owner, "First role")
    finishing = _open(owner, "A role with a committed result", slot=2)
    neighbor = _open(other, "Independent role")
    assert _open(owner, "First role") == child
    assert json.loads(dispatch(other, {"action": "project_goal", "op": "cancel", "project_id": child}))["error"]
    with ProjectStore(main) as store, pytest.raises(ValueError, match="Complete the child Goals"):
        close_goal(store, first, "done")

    with ProjectStore(owner) as store:
        worker = store.create("worker", "Act on the first assignment", "")
        unrelated = store.create("other", "Other worker", "")
        origin = {"goal_project_id": child}
        identity = store.enqueue(worker, "Continue the saved step", origin, "step")
        independent = store.enqueue(unrelated, "Other step", {"goal_project_id": neighbor}, "neighbor-step")
        first_job, second_job = _bind_timer(owner, child), _bind_timer(other, neighbor)
        _goal(owner, "pause", project_id=child)
        assert store.enqueue(worker, "Retried callback", origin, "step") == identity
        assert store.claim(worker) is None
        assert not admit_cron(get_job(first_job), execution_id="held-child-occurrence")[0]
        _goal(owner, "resume", project_id=child)
        _goal(main, "pause", project_id=first)
        assert not admit_cron(get_job(first_job), execution_id="held-parent-occurrence")[0]
        assert store.claim(worker) is None
        neighbor_claim = store.claim(unrelated)
        assert neighbor_claim["message_id"] == independent
        store.release(neighbor_claim)
        _goal(main, "resume", project_id=first)
        with pytest.raises(ValueError, match="already has an executor"):
            store.enqueue(unrelated, "Misaddressed callback", origin, "wrong")
        def start_closing(goal):
            goal.update(status="closing", outcome="done", final_evidence=[])
            return goal

        mutate_goal(store, finishing, start_closing, retire_pending=True)
        assert _snapshot(owner, finishing)["status"] == "closing"
        _goal(main, "cancel", project_id=first)
        assert store.message(identity)["state"] == "cancelled"
        assert store.message(independent)["state"] == "pending"
        assert get_job(first_job) is None and get_job(second_job) is not None
    assert _snapshot(owner, child)["status"] == "cancelled"
    assert _snapshot(owner, finishing)["status"] == "done"
    assert _snapshot(other, neighbor)["status"] == "ready"


@pytest.mark.parametrize("claimed", [False, True])
def test_child_timer_replaced_after_queuing_cannot_start_the_worker(main_home, claimed):
    from cron.jobs import get_job
    from hermes_runtime.standalone.events import admit_cron
    from tools.delegate_mail_work import MailboxDeferred, ensure_mail_input, stage_mail_input
    from tools.delegate_project_goal import close_wait, set_goal_paused

    _, db = main_home
    main = _actor(db)
    root = _open(main, APPOINTMENT)
    with ProjectStore(main) as store:
        coordinator = store.create("coordinator", "Coordinate", "")
    owner = _child_actor(db, coordinator, root)
    owner._delegate_depth = 1
    db.create_session(owner.session_id, source="delegate", parent_session_id=HOME_SESSION_ID)
    child = _open(owner, "Confirm the available slot")
    with ProjectStore(owner) as store:
        worker = store.create("worker", "Check for a slot", "")
        actor = SimpleNamespace(_session_db=db, session_id=worker["session_id"], _project_input_goal_id=child)
        db.create_session(actor.session_id, source="delegate", parent_session_id=owner.session_id)
        dispatch = store.enqueue(worker, "Book the appointment", {"goal_project_id": child})
        assert store.settle(worker, store.claim(worker), {"status": "completed", "summary": UNAVAILABLE})
        assert store.message(dispatch)["state"] == "completed"
        obsolete = _bind_timer(owner, child)
        assert admit_cron(get_job(obsolete), execution_id="original")[0]
        ensure_mail_input(owner, store, worker)
        pending = db._read_one("SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND state='pending'",
                               (worker["subagent_id"],))[0]
        message = store.claim(worker) if claimed else None
        assert not claimed or message["message_id"] == pending
        close_wait(store, child, "closed")
        new_job = _bind_timer(owner, child)
        if message:
            with pytest.raises(MailboxDeferred):
                stage_mail_input(actor, message, [])
            store.release(message)
        assert store.claim(worker) is None
        assert store.message(pending)["state"] == "cancelled"
        assert admit_cron(get_job(new_job), execution_id="replacement")[0]
        ensure_mail_input(owner, store, worker)
        valid = store.claim(worker)
        if claimed:
            set_goal_paused(store, child, True)
            with pytest.raises(MailboxDeferred):
                stage_mail_input(actor, valid, [])
            store.release(valid)
            assert store.claim(worker) is None
            assert store.message(valid["message_id"])["state"] == "pending"
            set_goal_paused(store, child, False)
            resumed = store.claim(worker)
            assert resumed["message_id"] == valid["message_id"]
            valid = resumed
        assert "Check the saved appointment" in stage_mail_input(actor, valid, [])[0]


def test_synchronous_orchestrator_uses_child_event_route_and_ignores_retired_result(main_home, monkeypatch):
    from tools.delegate_child_goals import publish_child_result
    from tools.delegate_mail_store import MailStore
    from tools.delegate_mail_work import ensure_mail_input, stage_mail_input
    from tools.delegate_project_goal import close_goal
    from tools.delegate_projects import _publish_completed

    _, db = main_home
    main = _actor(db)
    root = _open(main, APPOINTMENT)
    route = {"session_key": "agent:main:agentready:home", "origin_session_id": HOME_SESSION_ID,
             "platform": "agentready", "chat_id": HOME_SESSION_ID}
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", lambda: route)
    with ProjectStore(main) as store:
        coordinator = store.create("coordinator", "Coordinate", "")
        owner = _child_actor(db, coordinator, root)
        db.create_session(owner.session_id, source="delegate", parent_session_id=HOME_SESSION_ID)
        store.enqueue(coordinator, "Coordinate", {"goal_project_id": root, "wake": False, "mail_results": True})
        assert store.settle(coordinator, store.claim(coordinator), {"status": "completed", "summary": "Started"})

        blocked = _open(owner, "Obtain the approval")
        state = _goal(owner, "block", project_id=blocked, blocker="Awaiting approval", unblock="Record approval")
        stale = publish_child_result(blocked, state)["message_id"]
        _goal(owner, "cancel", project_id=blocked)
        ready = _open(owner, "Confirm the appointment", slot=2)
        with ProjectStore(owner) as children:
            finished = close_goal(children, ready, "done")
        active = publish_child_result(ready, finished)["message_id"]

        ensure_mail_input(main, store, coordinator)
        message = store.claim(coordinator)
        origin = json.loads(message["origin_json"])
        assert origin["wake"] and origin["session_key"] == route["session_key"]
        assert json.loads(message["options_json"])["agent_mail_id"] == active
        text, metadata = stage_mail_input(owner, message, [])
        db.append_message(owner.session_id, "user", text, platform_message_id=message["message_id"],
                          display_kind="agent_message", display_metadata=metadata)
        with MailStore(owner) as mail:
            mail.acknowledge_recorded()
        summary = "The confirmed appointment is ready"
        result = {"status": "completed", "summary": summary}
        assert store.settle(coordinator, message, result)
        assert not result.get("goal_continuing")
        _publish_completed(store, coordinator, store.message(message["message_id"]), result)
    assert db._read_one("SELECT delivered_at FROM delegation_mail WHERE message_id=?", (stale,))[0] is None
    assert _pending_event(db, message["message_id"])["summary"] == summary
