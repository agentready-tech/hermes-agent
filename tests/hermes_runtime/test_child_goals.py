"""A Bot Mode assignment crosses durable role Goals and an exact native QA wake."""

import asyncio
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from hermes_runtime.sessions import HOME_SESSION_ID
from tests.hermes_runtime.test_external_wait_timer import _isolated_tick
from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_project_goal import _actor, _agent, _consume, _goal, _models, _open, _snapshot
from tests.tools.test_delegate_mail_live import _response
from tests.waiting import async_wait_until
from tools.delegate_project_store import ProjectStore

pytest_plugins = ["tests.hermes_runtime.test_main_schedule_tools"]

STAGES = ("Spec", "Dev", "Review", "QA")
SHA = "a91dd44c2bcb37f2d0755255d79ceb49dc10ccad"
REPORTS = {
    "Spec": "Acceptance criteria recorded.",
    "Dev": f"Commit {SHA}. Unit tests passed (28/28).",
    "Review": f"Reviewed {SHA}. Approved the same commit.",
    "QA": f"Validated {SHA} on the assigned staging environment.",
}


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
async def test_role_goals_resume_the_same_qa_after_restart(main_home, monkeypatch, return_path):
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
    config.write_text(config.read_text() + "delegation:\n  max_spawn_depth: 2\n  worktree_isolation: false\n")
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
    coordinator_steps.extend((("QA", "timer"), ("QA", "no-change"), ("QA", "verify"), ("QA", "aggregate")))
    calls, outputs, ids = [], [], {}
    qa_checks = []
    deadline = (datetime.now(UTC) + timedelta(days=3)).isoformat()

    def verify(goals):
        return {pid: tuple(ref for ref, evidence in candidate["evidence"].items()
                           if any(report in evidence["value"] for report in REPORTS.values())
                           or "Overall verified on staging" in evidence["value"])
                for pid, candidate in goals.items()}

    def model(agent, payload, *_args, **_kwargs):
        messages = payload["messages"]
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            assert "error" not in result, result
        if agent.session_id == HOME_SESSION_ID:
            step = next(main_steps)
            calls.append(step)
            if step == "open":
                return _as_goal("open", goal="Deliver verified change", verification="QA verified exact reviewed SHA")
            if step == "spawn":
                ids["main"] = result["project_id"]
                return _spawn("delivery", "Coordinate the four release stages", background=True)
            if step == "ack":
                ids["coordinator"] = result["projects"][0]["subagent_id"]
                ids["first_input"] = result["projects"][0]["message_id"]
                return _response("Принято.")
            assert f"Overall verified on staging. SHA {SHA}" in json.dumps(messages)
            return _response(f"Проверено на стенде. SHA {SHA}.")

        if agent._delegate_role == "orchestrator":
            assert agent._project_goal_segment.parent_goal_id == ids["main"]
            stage, op = coordinator_steps.popleft()
            if op == "open":
                if stage != "Spec":
                    prior = STAGES[STAGES.index(stage) - 1]
                    saved = _snapshot(agent, ids[prior])
                    assert saved["status"] == "done"
                    assert any(REPORTS[prior] in item["value"] for item in saved["evidence"].values())
                return _as_goal("open", goal=f"{stage} stage", verification=REPORTS[stage], slot=STAGES.index(stage) + 1)
            if op == "spawn":
                ids[stage] = result["project_id"]
                context = f" for exact SHA {SHA}" if stage in ("Review", "QA") else ""
                return _spawn(stage, stage + context)
            if stage == "QA" and op == "handoff":
                ids["qa_session"] = result["projects"][0]["session_id"]
                return _response(args={"action": "external_wait", "op": "open", "project_id": ids["QA"],
                    "mode": "resume_child", "reply_criterion": "Staging is available", "deadline": deadline,
                    "return_path": "timer", "schedule": "every 15m", "prompt": "Recheck the assigned QA stand"})
            if op == "timer":
                ids["qa_timer"] = result["job_id"]
                return _response("Waiting for the assigned staging environment.")
            if op == "no-change":
                return _response("Waiting for the assigned staging environment.")
            if op == "aggregate":
                return _response(f"Overall verified on staging. SHA {SHA}.")
            return _response("Stage recorded.")

        stage = db._read_one("SELECT project_key FROM delegated_projects WHERE subagent_id=?",
                             (agent._persistent_project_id,))[0]
        if stage in ("Review", "QA"):
            assert SHA in json.dumps(messages)
        if stage != "QA":
            return _response(REPORTS[stage])
        qa_checks.append(agent.session_id)
        if len(qa_checks) == 1:
            return _response("Staging is occupied.")
        source = "Assigned QA stand is available" if return_path == "mail" and len(qa_checks) == 3 else "Recheck the assigned QA stand"
        assert source in messages[-1]["content"]
        return _response("Staging is occupied." if len(qa_checks) == 2 else REPORTS[stage])

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
        "session_id": HOME_SESSION_ID, "submission_id": "four-stage-release", "text": "Deliver verified change"}})
    assert "error" not in submitted, submitted
    await authority._drain(SessionRef(str(home), HOME_SESSION_ID))

    async def consume(match):
        event = await async_wait_until(lambda: _pending_event(db, match), what=match)
        await _consume(runner, authority, home, event)

    await consume(ids["first_input"])
    await async_wait_until(lambda: ids.get("qa_timer"), what="native coordinator handoffs through QA")
    with ProjectStore(_actor(db)) as store:
        child_owner = _child_actor(db, store.get(ids["coordinator"]), ids["main"])
    await async_wait_until(lambda: _snapshot(child_owner, ids["QA"])["status"] == "waiting",
                           what="durably bound QA wait")
    assert calls == ["open", "spawn", "ack"]
    assert qa_checks == [ids["qa_session"]]
    assert _snapshot(_actor(db), ids["main"])["status"] == "running"

    job_id = ids["qa_timer"]
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
    await async_wait_until(lambda: len(qa_checks) == 2, what="the saved QA worker handles the timer")
    assert admit_cron(get_job(job_id), execution_id=first)[0]
    assert qa_checks == [ids["qa_session"]] * 2
    assert calls == ["open", "spawn", "ack"]

    if return_path == "timer":
        _isolated_tick(monkeypatch, job_id)
        await consume(f"{job_id}:{latest_execution(job_id)['id']}")
    else:
        _goal(child_owner, "select", project_id=ids["QA"])
        receipt = mail_action(child_owner, "message", ids["qa_session"], "Assigned QA stand is available", "stand-free")
        assert mail_action(child_owner, "message", ids["qa_session"], "Assigned QA stand is available", "stand-free") == receipt
        await consume(receipt["message_id"])

    def aggregate_input():
        row = db._read_one(
            """SELECT message_id FROM delegated_project_messages WHERE subagent_id=? AND
               json_extract(origin_json, '$.child_goal_event.event_id')=?""",
            (ids["coordinator"], f"{ids['QA']}:done"),
        )
        return row[0] if row else None

    aggregate = await async_wait_until(aggregate_input, what="the coordinator's aggregate of verified role results")
    await consume(aggregate)
    assert qa_checks == [ids["qa_session"]] * 3
    assert calls == ["open", "spawn", "ack", "aggregate"]
    assert [output for output in outputs if output] == ["Принято.", f"Проверено на стенде. SHA {SHA}."]
    assert get_job(job_id) is None
    assert _snapshot(_actor(db), ids["main"])["status"] == "done"
    assert all("Staging is occupied" not in (row["content"] or "") for row in db.get_messages(HOME_SESSION_ID))
    for stage in STAGES:
        state = _snapshot(child_owner, ids[stage])
        assert (state["status"], state["parent_goal_id"]) == ("done", ids["main"])
        assert len([ev for ev in state["evidence"].values() if REPORTS[stage] in ev["value"]]) == 1


def test_child_control_and_pending_inputs_are_scoped_by_parent_and_executor(main_home):
    from cron.jobs import get_job
    from hermes_runtime.standalone.events import admit_cron
    from tools.delegate_external_wait import dispatch as external_wait
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

    def bind_timer(actor, project_id):
        wait = json.loads(external_wait(actor, {
            "action": "external_wait", "op": "open", "project_id": project_id, "mode": "resume_child",
            "reply_criterion": "Stand is available", "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "return_path": "timer", "schedule": "every 15m", "prompt": "Check the stand",
        }))
        assert wait["success"], wait
        return wait["job_id"]

    with ProjectStore(owner) as store:
        worker = store.create("worker", "Act on the first assignment", "")
        unrelated = store.create("other", "Other worker", "")
        origin = {"goal_project_id": child}
        identity = store.enqueue(worker, "Continue the saved step", origin, "step")
        independent = store.enqueue(unrelated, "Other step", {"goal_project_id": neighbor}, "neighbor-step")
        first_job, second_job = bind_timer(owner, child), bind_timer(other, neighbor)
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
