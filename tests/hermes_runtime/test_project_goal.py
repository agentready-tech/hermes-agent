"""Project Goals follow native main admissions, worker receipts and exact AGE-6 timers."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_runtime.adapters.goal_turn import GoalSegment, admitted_input
from hermes_runtime.sessions import HOME_SESSION_ID
from tests.hermes_runtime.test_external_wait_timer import _isolated_tick, _timer
from tests.hermes_runtime.test_main_cron_wake import owner as gateway_owner
from tests.hermes_runtime.test_main_schedule_tools import _tool
from tests.tools.test_delegate_external_wait import _args, _call
from tests.tools.test_delegate_mail_live import _response
from tools.delegate_project_goal import dispatch, read_goal
from tools.delegate_project_store import ProjectStore

pytest_plugins = ["tests.hermes_runtime.test_main_schedule_tools"]


@pytest.fixture(autouse=True)
def offline_main(monkeypatch):
    monkeypatch.setattr("agent.prompt_builder._probe_remote_backend", lambda *_: "Linux Docker fixture")
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")


def _actor(db):
    turn = SimpleNamespace(goal_segment=GoalSegment("accepted-user-input", False))
    return SimpleNamespace(_session_db=db, session_id=HOME_SESSION_ID, _agentready_active_turn=turn)


def _goal(actor, op, **args):
    answer = json.loads(dispatch(actor, {"action": "project_goal", "op": op, **args}))
    assert answer.get("success"), answer
    return answer


def _open(actor, name, **source):
    return _goal(actor, "open", goal=name, verification="Return a verified result", **source)["project_id"]


def _snapshot(actor, project_id):
    with ProjectStore(actor) as store:
        return read_goal(store, project_id)


def _agent(db, *, toolsets=("delegation", "cronjob")):
    from run_agent import AIAgent

    return AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1",
                   api_key="inert", api_mode="chat_completions", session_id=HOME_SESSION_ID, session_db=db,
                   enabled_toolsets=list(toolsets), skip_context_files=True, load_soul_identity=False,
                   skip_memory=True, quiet_mode=True)


def _models(monkeypatch, offline_model, verifier):
    from run_agent import AIAgent

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", offline_model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", offline_model)
    monkeypatch.setattr("hermes_runtime.adapters.goal_turn._goal_judgments", verifier)


def _deny_decisions(monkeypatch):
    spy = Mock(side_effect=AssertionError("Goal-owned work must not enter Continuation Guard"))
    monkeypatch.setattr("agent.continuation_guard.build_continuation_nudge", spy)
    return spy


def test_project_goal_uses_fenced_verdict_with_exact_evidence(main_home, monkeypatch):
    from hermes_runtime.adapters.goal_turn import finish_segment

    _, db = main_home
    actor = _actor(db)
    done = _open(actor, "Return the verified result")
    other = _open(actor, "Finish the second assignment", slot=2)
    _goal(actor, "checkpoint", project_id=done, deliverable="The verified result")
    _goal(actor, "checkpoint", project_id=other, deliverable="The current draft")
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)

    def judge(**kwargs):
        goals = json.loads(kwargs["messages"][-1]["content"])
        assert set(goals) == {done, other}
        [receipt] = goals[done]["evidence"]
        verdict = json.dumps({done: [receipt], other: [receipt]})
        return _response(f"```json\n{verdict}\n```")

    reviewer = Mock(side_effect=judge)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", reviewer)
    finish_segment(actor, "The verified result")

    reviewer.assert_called_once()
    assert _snapshot(actor, done)["status"] == "done"
    assert _snapshot(actor, other)["status"] == "ready"


def test_exhausted_goal_budget_stays_blocked_and_quiet(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    project_id = _open(actor, "Complete the saved work")
    monkeypatch.setattr("hermes_cli.goals.DEFAULT_MAX_TURNS", 1)
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    goal_turn.finish_segment(actor, "Queued.")
    inbox = HomeInbox()
    [wake] = inbox.pending()
    turn = SimpleNamespace(inbox=inbox)
    actor._agentready_active_turn = turn

    def model(_prompt, **_kwargs):
        return {"completed": True, "final_response": goal_turn.finish_segment(actor, ".")}

    result = goal_turn.run_segment(actor, turn, model, wake[1], goal_records=[wake], conversation_history=[])

    state = _snapshot(actor, project_id)
    assert state["status"] == "blocked"
    assert state["attempts_without_progress"] == 1
    assert state["blocker"] and state["wake_id"] is None
    assert result["final_response"] == ""
    assert result["_notification_reply_muted"]


@pytest.mark.asyncio
async def test_admitted_question_and_multiple_accepted_assignments(main_home, monkeypatch):
    from gateway.session_contract import SessionRef
    from gateway.session_controls import AuthorityConnection

    home, db = main_home
    runner, authority = await gateway_owner(home, db, monkeypatch, "goal-intake")
    connection = AuthorityConnection(authority, SimpleNamespace(write=lambda _frame: True), {
        "user_id": "fixture-panel", "provider": "local", "profile_id": str(home),
        "instance_id": "goal-intake", "native_bootstrap": True,
    })
    mounted = await connection.dispatch({"id": 1, "method": "session.resume",
        "params": {"session_id": HOME_SESSION_ID, "display_projection": True}})
    assert "error" not in mounted, mounted

    initial = {"action": "project_goal", "op": "open",
               "goal": "Write the review", "verification": "Provide the review text"}
    outcomes = []
    rounds = iter(["open", "repeat", "second", "checkpoint-first", "checkpoint-second", "answer"])
    verification = Mock(side_effect=lambda goals: {pid: tuple(g["evidence"]) for pid, g in goals.items()})

    def model(agent, payload, *_args, **_kwargs):
        if payload["messages"][-1].get("content") == "How does review work?":
            return _response("Review starts with evidence.")
        step = next(rounds)
        if step == "open" or step == "repeat":
            return _response(args=initial)
        if step == "second":
            return _response(args={**initial, "slot": 2, "goal": "Write the handoff note"})
        if step.startswith("checkpoint"):
            row = json.loads(payload["messages"][-1]["content"])
            assert row["success"]
            if step == "checkpoint-first":
                with ProjectStore(agent) as store:
                    assert len(store.projects()) == 2
                    first = next(p["subagent_id"] for p in store.projects() if p["goal"] == "Write the review")
                return _response(args={"action": "project_goal", "op": "checkpoint", "project_id": first,
                    "deliverable": "Review result with supporting findings"})
            # Its own confirmed draft, attached to the distinct Goal.
            with ProjectStore(agent) as store:
                second = next(p["subagent_id"] for p in store.projects() if p["goal"] == "Write the handoff note")
            return _response(args={"action": "project_goal", "op": "checkpoint", "project_id": second,
                                   "deliverable": "Prepared handoff with evidence"})
        return _response("The review and handoff are ready.")

    async def execute(event, *_args):
        assert admitted_input.get() is not None
        agent = _agent(db)
        try:
            result = await asyncio.to_thread(agent.run_conversation, event.text)
            outcomes.append(result)
            return result["final_response"]
        finally:
            agent.close()

    _models(monkeypatch, model, verification)
    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)

    async def submit(nonce, text):
        request = {"id": 2, "method": "prompt.submit", "params": {
            "session_id": HOME_SESSION_ID, "submission_id": nonce, "text": text}}
        one = await connection.dispatch(request)
        two = await connection.dispatch(request)
        assert one["result"]["admission_id"] == two["result"]["admission_id"]
        await authority._drain(SessionRef(str(home), HOME_SESSION_ID))
        return one["result"]["admission_id"]

    await submit("question", "How does review work?")
    assert outcomes[-1]["final_response"] == "Review starts with evidence."
    with ProjectStore(_actor(db)) as store:
        assert store.projects() == []
    verification.assert_not_called()
    guard = _deny_decisions(monkeypatch)
    source = await submit("two-tasks", "Write the review and the handoff note")
    assert outcomes[-1]["final_response"] == "The review and handoff are ready."
    with ProjectStore(_actor(db)) as store:
        projects = store.projects()
        assert len(projects) == 2
        for row in projects:
            state = read_goal(store, row["subagent_id"])
            assert state["status"] == "done" and state["final_evidence"]
            assert state["source"]["id"] == source
    verification.assert_called_once()
    assert set(verification.call_args.args[0]) == {row["subagent_id"] for row in projects}
    guard.assert_not_called()


def test_tracker_identity_and_stale_source_are_scoped(main_home):
    _, db = main_home
    actor = _actor(db)
    first = _open(actor, "Prepare rollout evidence", tracker_ref="workspace/issue-42")
    actor._agentready_active_turn.goal_segment = GoalSegment("later-accepted-input", False)
    assert _open(actor, "Prepare rollout evidence", tracker_ref="workspace/issue-42") == first
    assert _open(actor, "Prepare rollout evidence", tracker_ref="other-workspace/issue-42") != first
    actor._agentready_active_turn.goal_segment = GoalSegment(None, True)
    refused = json.loads(dispatch(actor, {"action": "project_goal", "op": "open",
        "goal": "Invent an assignment", "verification": "Pretend there is proof"}))
    assert refused.get("error")


async def _consume(runner, authority, home, event):
    from gateway.run import _format_gateway_process_notification
    from gateway.session_contract import SessionRef

    formatted = _format_gateway_process_notification(event)
    assert await runner._deliver_completion_notification(formatted, event) is True
    assert await runner._deliver_completion_notification(formatted, event) is None
    await authority._drain(SessionRef(str(home), HOME_SESSION_ID))


def _wake(db, prefix):
    with db._read_ctx() as conn:
        [row] = conn.execute("SELECT event_json FROM async_delegations WHERE delegation_id LIKE ?", (prefix + "%",)).fetchall()
    return json.loads(row[0])


@pytest.mark.asyncio
async def test_worker_report_resumes_only_its_goal_then_retires_its_timer(main_home, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.goal_turn import finish_segment, recover
    from tools.delegate_projects import _publish_completed

    home, db = main_home
    actor = _actor(db)
    project_id = _open(actor, "Verify the completed work")
    unrelated = _open(actor, "Follow up with another contact", slot=2)
    waits = {}
    for pid in (project_id, unrelated):
        wait = _call(actor, **_args(pid))
        ids = {"project_id": pid, "wait_id": wait["wait_id"]}
        waits[pid] = _timer(actor, {**ids, "action": "create", "schedule": "every 15m",
                                  "prompt": "Review the exact source"})["job_id"]
    finish_segment(actor, "Waiting.")

    runner, authority = await gateway_owner(home, db, monkeypatch, "goal-continuation")
    other = _snapshot(actor, unrelated)
    # Retire the first phase timer on the worker receipt. The next phase has a
    # concrete action. A separate timer will be bound for final cleanup below.
    related_wait = _call(actor, op="select", project_id=project_id,
                         wait_id=get_job(waits[project_id])["external_wait"]["wait_id"])
    assert related_wait["state"] == "BOUND"
    assert _call(actor, op="close", project_id=project_id, wait_id=related_wait["wait_id"],
                 reason="closed")["state"] == "CLOSED"
    with ProjectStore(actor) as store:
        worker = store.create("verification-worker", "Gather preliminary findings", "")
        from hermes_runtime.adapters.state import main_gateway_origin
        message_id = store.enqueue(worker, "Check preliminary evidence", {
            **main_gateway_origin(), "goal_project_id": project_id, "wake": True,
        }, "verification-first-step")
        message = store.claim(worker)
        assert message["message_id"] == message_id
        receipt = {"status": "completed", "summary": "Preliminary finding. Final verification is outstanding."}
        assert store.settle(worker, message, receipt)
        _publish_completed(store, worker, store.message(message_id), receipt)

    rounds = iter(["plan", "quiet", "evidence", "done"])
    calls = []

    def verifier(goals):
        assert list(goals) == [project_id]
        evidence = goals[project_id]["evidence"]
        calls.append(evidence)
        return {project_id: tuple(k for k, v in evidence.items() if v["kind"] == "deliverable")}

    def model(agent, payload, *_args, **_kwargs):
        step = next(rounds)
        if step == "plan":
            assert "Preliminary finding" in json.dumps(payload["messages"])
            return _response(args={"action": "project_goal", "op": "checkpoint", "project_id": project_id,
                                   "stage": "Preliminary evidence collected", "next_step": "Verify the final result"})
        if step == "quiet":
            return _response("I will proceed to the final check.")
        if step == "evidence":
            assert _snapshot(actor, project_id)["next_step"] == "Verify the final result"
            return _response(args={"action": "project_goal", "op": "checkpoint", "project_id": project_id,
                                   "deliverable": "Final verified result"})
        return _response("Final verified result.")

    captured = []

    async def execute(event, *_args):
        agent = _agent(db)
        try:
            result = await asyncio.to_thread(agent.run_conversation, event.text)
            captured.append(result)
            return result["final_response"]
        finally:
            agent.close()

    _models(monkeypatch, model, verifier)
    guard = _deny_decisions(monkeypatch)
    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)
    await _consume(runner, authority, home, _wake(db, message_id))
    assert captured[-1]["final_response"] == "" and captured[-1]["_notification_reply_muted"]
    assert db.get_messages(HOME_SESSION_ID)[-1]["display_kind"] == "hidden"
    after_worker = _snapshot(actor, project_id)
    assert after_worker["status"] == "ready"
    assert message_id in after_worker["evidence"]

    new_wait = _call(actor, **{**_args(project_id), "reply_criterion": "Await final verification"})
    job_id = _timer(actor, {"action": "create", "project_id": project_id, "wait_id": new_wait["wait_id"],
                            "schedule": "every 15m", "prompt": "Recheck final verification"})["job_id"]
    # Restart recovery must preserve the identity of a wake already accepted by the outbox.
    ready = _wake(db, "main-goal-")
    recover()
    assert _wake(db, "main-goal-") == ready
    await _consume(runner, authority, home, ready)
    assert captured[-1]["final_response"] == "Final verified result."
    assert db.get_messages(HOME_SESSION_ID)[-1].get("display_kind") != "hidden"
    assert _snapshot(actor, project_id)["status"] == "done"
    assert _snapshot(actor, unrelated) == other
    assert get_job(job_id) is None and get_job(waits[unrelated]) is not None
    assert len(calls) == 2 and message_id in calls[0]
    guard.assert_not_called()


@pytest.fixture
def browser_main(main_home):
    home, db = main_home
    config = home / "config.yaml"
    config.write_text(config.read_text() + "browser:\n  backend: browser-use\n")
    agent = _agent(db, toolsets=("delegation", "cronjob", "browser", "terminal"))
    try:
        assert "browser_exec" in agent.valid_tool_names
        yield agent, _actor(db)
    finally:
        agent.close()


def _admitted_turn(agent, input_id, prompt):
    token = admitted_input.set({"admission_id": input_id, "payload": {}})
    try:
        return agent.run_conversation(prompt)
    finally:
        admitted_input.reset(token)


def test_one_shot_browser_read_runs_without_goal(browser_main, monkeypatch):
    from tools import browser_use_cli

    agent, actor = browser_main
    call = _response(args={"code": "print('No new replies')"})
    call.choices[0].message.tool_calls[0].function.name = "browser_exec"
    read = Mock(return_value=json.dumps({"success": True, "text": "No new replies"}))
    verify_goal = Mock()

    def model(_agent, payload, *_ignored, **_kwargs):
        if payload["messages"][-1]["role"] == "user":
            return call
        if read.called:
            return _response("There are no new replies.")
        return _response("The browser did not run.")

    monkeypatch.setattr(browser_use_cli, "browser_exec", read)
    monkeypatch.setattr("agent.continuation_guard.build_continuation_nudge", lambda *_a, **_kw: None)
    _models(monkeypatch, model, verify_goal)
    result = _admitted_turn(agent, "one-shot-chat-read", "Look in the chat for new replies now.")

    read.assert_called_once()
    assert result["completed"]
    assert result["final_response"] == "There are no new replies."
    verify_goal.assert_not_called()
    with ProjectStore(actor) as store:
        assert store.projects() == []


def test_browser_send_resumes_goal_accepted_before_confirmation(browser_main, monkeypatch):
    from cron.jobs import get_job
    from tools import browser_use_cli

    agent, actor = browser_main
    planned, sent = {}, []
    rounds = iter(["open", "block", "confirm", "resume", "send", "checkpoint", "wait", "timer", "answer"])

    def execute(**_kwargs):
        project_id = planned["project_id"]
        sent.append(project_id)
        saved = _snapshot(actor, project_id)
        assert saved["status"] == "ready"
        assert saved["source"]["id"] == "original-assignment"
        return json.dumps({"success": True, "message_id": "approval-request"})

    def model(_agent, payload, *_ignored, **_kwargs):
        step = next(rounds)
        if step == "open":
            return _response(args={"action": "project_goal", "op": "open", "goal": "Get approval in chat",
                                   "verification": "Approval received in the chat after the request"})
        if step == "confirm":
            return _response("The approval request is drafted. Send it?")
        if step == "resume":
            assert payload["messages"][-1]["content"] == "Send it."
            return _response(args={"action": "project_goal", "op": "resume", "project_id": planned["project_id"]})
        content = next(row["content"] for row in reversed(payload["messages"]) if row["role"] == "tool")
        if step == "checkpoint":
            assert "approval-request" in content
            return _response(args={"action": "project_goal", "op": "checkpoint", "project_id": planned["project_id"],
                                   "evidence_tool_name": "browser_exec", "stage": "Approval requested",
                                   "next_step": "Check the chat for approval"})
        if step == "wait":
            return _response(args={"action": "external_wait", **_args(planned["project_id"]),
                                   "reply_criterion": "Approval received"})
        result = json.loads(content)
        assert result["success"], result
        if step == "block":
            planned["project_id"] = result["project_id"]
            return _response(args={"action": "project_goal", "op": "block", "project_id": result["project_id"],
                                   "blocker": "The draft is awaiting permission to send",
                                   "unblock": "The user confirms the message may be sent"})
        if step == "send":
            assert result["project_id"] == planned["project_id"] and result["status"] == "ready"
            response = _response(args={"code": "print('approval request sent')"})
            call = response.choices[0].message.tool_calls[0]
            call.id = "send-approval-request"
            call.function.name = "browser_exec"
            return response
        project_id = planned["project_id"]
        if step == "timer":
            response = _response(args={"action": "create", "project_id": project_id, "wait_id": result["wait_id"],
                                       "schedule": "every 15m", "prompt": "Check the chat for approval"})
            response.choices[0].message.tool_calls[0].function.name = "cronjob_manage"
            return response
        planned["timer"] = result["job_id"]
        return _response("Approval requested. I’ll follow up in the same chat.")

    monkeypatch.setattr(browser_use_cli, "browser_exec", execute)
    _models(monkeypatch, model, lambda _goals: {})
    first = _admitted_turn(agent, "original-assignment", "Get approval in chat. Ask me before sending.")
    project_id = planned["project_id"]
    assert first["final_response"] == "The approval request is drafted. Send it?"
    assert _snapshot(actor, project_id)["status"] == "blocked"
    assert not sent

    result = _admitted_turn(agent, "send-confirmation", "Send it.")
    assert result["completed"], result["final_response"]
    assert sent == [project_id], "browser_exec must run exactly once with a saved Goal"
    assert result["final_response"] == "Approval requested. I’ll follow up in the same chat."
    goal = _snapshot(actor, project_id)
    assert goal["status"] == "waiting"
    assert "tool:send-approval-request" in goal["evidence"]
    assert get_job(planned["timer"])["external_wait"]["project_id"] == project_id
    with ProjectStore(actor) as store:
        assert [project["subagent_id"] for project in store.projects()] == [project_id]


@pytest.mark.asyncio
async def test_exact_timer_no_change_is_quiet_and_keeps_next_slot(main_home, monkeypatch):
    from cron.jobs import get_job

    home, db = main_home
    actor = _actor(db)
    planned = {}
    rounds = iter(["accept", "wait", "schedule", "acknowledge"])

    def plan(agent, payload, *_ignored, **_kwargs):
        step = next(rounds)
        if step == "accept":
            return _response(args={"action": "project_goal", "op": "open",
                "goal": "Review the supplier's reply", "verification": "Confirm receipt from the supplier"})
        receipt = json.loads(payload["messages"][-1]["content"])
        assert receipt["success"], receipt
        if step == "wait":
            planned["project_id"] = receipt["project_id"]
            return _response(args={"action": "external_wait", **_args(planned["project_id"])})
        if step == "schedule":
            response = _response(args={"action": "create", "project_id": receipt["project_id"],
                "wait_id": receipt["wait_id"], "schedule": "every 15m", "prompt": "Check only the supplier thread"})
            response.choices[0].message.tool_calls[0].function.name = "cronjob_manage"
            return response
        planned["timer"] = receipt["job_id"]
        return _response("I will check for the supplier's reply.")

    verifier = Mock()
    _models(monkeypatch, plan, verifier)
    guard = _deny_decisions(monkeypatch)
    token = admitted_input.set({"admission_id": "fixture-durable-assignment", "payload": {}})
    agent = _agent(db)
    try:
        assert "cronjob_manage" in agent.valid_tool_names
        initial = agent.run_conversation("Review the supplier's reply when it arrives")
        assert initial["completed"], [initial["final_response"], initial["messages"][-4:]]
    finally:
        agent.close()
        admitted_input.reset(token)
    project_id, timer = planned["project_id"], planned["timer"]
    assert _snapshot(actor, project_id)["status"] == "waiting"

    runner, authority = await gateway_owner(home, db, monkeypatch, "goal-timer")
    unrelated = _tool("create", schedule="every 1h", prompt="Other task")
    captured = []

    def model(agent, payload, *_args, **_kwargs):
        assert "Check only the supplier thread" in json.dumps(payload["messages"])
        assert project_id in payload["messages"][-1]["content"]
        return _response("There is no new reply.")

    async def execute(event, *_args):
        agent = _agent(db)
        try:
            result = await asyncio.to_thread(agent.run_conversation, event.text)
            captured.append(result)
            return result["final_response"]
        finally:
            agent.close()

    _models(monkeypatch, model, verifier)
    monkeypatch.setattr(runner, "_handle_message_with_agent", execute)
    before = get_job(timer)["next_run_at"]
    with monkeypatch.context() as clock:
        await asyncio.to_thread(_isolated_tick, clock, timer)
    await _consume(runner, authority, home, _wake(db, "main-cron-"))
    assert captured[-1]["final_response"] == "" and captured[-1]["_notification_reply_muted"]
    assert db.get_messages(HOME_SESSION_ID)[-1]["display_kind"] == "hidden"
    assert _snapshot(actor, project_id)["status"] == "waiting"
    assert get_job(timer)["next_run_at"] > before
    assert get_job(unrelated["job_id"]) is not None
    verifier.assert_not_called()
    guard.assert_not_called()



def test_block_and_resume_keep_the_same_goal_then_cancel_exact_wait(main_home):
    from cron.jobs import get_job
    from hermes_runtime.adapters.goal_turn import finish_segment

    _, db = main_home
    actor = _actor(db)
    project_id = _open(actor, "Wait for the supplier")
    wait = _call(actor, **_args(project_id))
    timer = _timer(actor, {"action": "create", "project_id": project_id, "wait_id": wait["wait_id"],
                           "schedule": "every 15m", "prompt": "Check the supplier thread"})["job_id"]
    finish_segment(actor, "Waiting.")
    actor._agentready_active_turn.goal_segment = GoalSegment("requesting-next-step", False)
    block = _goal(actor, "block", project_id=project_id, blocker="Missing access to the supplier thread",
                  unblock="Restore access to the same thread")
    assert block["project_id"] == project_id and block["status"] == "blocked"
    assert _goal(actor, "resume", project_id=project_id)["project_id"] == project_id
    finish_segment(actor, "Access is restored.")
    assert _snapshot(actor, project_id)["status"] == "waiting"
    assert _goal(actor, "cancel", project_id=project_id)["status"] == "cancelled"
    assert get_job(timer) is None
    with ProjectStore(actor) as store:
        wait = store.read_workspace(project_id)["external_wait"]
        assert (wait["state"], wait["close_reason"]) == ("CLOSED", "cancelled")


def test_recover_ready_goal_reuses_wake_and_retired_inbox_cannot_run(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    project_id = _open(actor, "Complete the saved step")
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    _goal(actor, "checkpoint", project_id=project_id, next_step="Perform the saved check")
    with monkeypatch.context() as interrupted:
        interrupted.setattr("hermes_runtime.standalone.events._admit_scheduled_prompt",
                            Mock(side_effect=OSError("interrupted after saving the next step")))
        goal_turn.finish_segment(actor, "Saved.")
    state = _snapshot(actor, project_id)
    assert state["status"] == "ready" and state["wake_id"]
    inbox = HomeInbox()
    assert inbox.pending() == []

    goal_turn.recover()
    [wake] = inbox.pending()
    goal_turn.recover()
    assert inbox.pending() == [wake]
    assert _snapshot(actor, project_id) == state
    assert _goal(actor, "cancel", project_id=project_id)["status"] == "cancelled"
    turn = SimpleNamespace(inbox=inbox)
    model = Mock(side_effect=AssertionError("A retired Goal must not start a model turn"))
    result = goal_turn.run_segment(actor, turn, model, wake[1], goal_records=[wake], conversation_history=[])
    assert result["completed"] and result["_notification_reply_muted"]
    assert inbox.pending() == []
    model.assert_not_called()



def test_confirmed_native_operation_returns_to_its_goal_without_cron(main_home, monkeypatch):
    from cron.jobs import list_jobs
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    home, db = main_home
    actor = _actor(db)
    project_id = _open(actor, "Retrieve the finished operation")
    operations = ToolOperationStore(home)
    row, accepted = operations.admit(HOME_SESSION_ID, "fixture-uid", "fixture-call", "connector_read",
                                     {"record": "42"}, "fixture-service", "fixture-group")
    assert accepted
    identity = row["operation_id"]
    refused = json.loads(dispatch(actor, {"action": "project_goal", "op": "checkpoint",
        "project_id": project_id, "await_operation_id": identity}))
    assert refused.get("error")
    operations.defer(identity)
    _goal(actor, "checkpoint", project_id=project_id, await_operation_id=identity)
    goal_turn.finish_segment(actor, "The operation is running.")
    assert _snapshot(actor, project_id)["status"] == "awaiting_operation"
    assert list_jobs() == []

    monkeypatch.setattr("hermes_runtime.adapters.turns.forward_to_active_turn", Mock(return_value=True))
    operations.complete(identity, {"value": "verified"})
    inbox = HomeInbox()
    [wake] = inbox.pending()
    verifier = Mock(return_value={project_id: (identity,)})
    monkeypatch.setattr(goal_turn, "_goal_judgments", verifier)
    turn = SimpleNamespace(inbox=inbox)
    actor._agentready_active_turn = turn

    def model(prompt, **_kwargs):
        assert project_id in prompt
        return {"completed": True, "final_response": goal_turn.finish_segment(actor, "The result is verified.")}

    result = goal_turn.run_segment(actor, turn, model, wake[1], goal_records=[wake], conversation_history=[])
    assert result["final_response"] == "The result is verified."
    assert _snapshot(actor, project_id)["final_evidence"] == [identity]
    assert list_jobs() == []
    verifier.assert_called_once()



def test_recovery_leaves_due_slot_to_owning_cron(main_home, monkeypatch):
    from datetime import datetime, timedelta

    from cron.jobs import _jobs_lock, _peek_jobs_unlocked, get_job, save_jobs
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, "Check the exact supplier thread")
    wait = _call(actor, **_args(goal))
    job = _timer(actor, {"action": "create", "project_id": goal, "wait_id": wait["wait_id"],
                         "schedule": "every 15m", "prompt": "Check the thread"})["job_id"]
    goal_turn.finish_segment(actor, "Waiting.")
    due = datetime.fromisoformat(get_job(job)["next_run_at"])
    monkeypatch.setattr(goal_turn, "datetime", SimpleNamespace(now=lambda _tz: due))
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    goal_turn.recover()
    assert _snapshot(actor, goal)["status"] == "waiting"
    assert HomeInbox().pending() == []

    after_deadline = datetime.fromisoformat(wait["deadline"]) + timedelta(seconds=1)
    # Cron advances the recurrence before handing off its last valid occurrence.
    with _jobs_lock(strict=True):
        jobs = _peek_jobs_unlocked()
        next(record for record in jobs if record["id"] == job)["next_run_at"] = after_deadline.isoformat()
        save_jobs(jobs)
    goal_turn.recover()
    assert _snapshot(actor, goal)["status"] == "waiting"
    assert HomeInbox().pending() == []

    monkeypatch.setattr(goal_turn, "datetime", SimpleNamespace(now=lambda _tz: after_deadline))
    goal_turn.recover()
    assert _snapshot(actor, goal)["status"] == "ready"
    assert len(HomeInbox().pending()) == 1


def test_recovery_rotates_a_consumed_wake_without_resaved_transition(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox

    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, "Continue from the last accepted step")
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    goal_turn.finish_segment(actor, "Queued.")
    original = _snapshot(actor, goal)["wake_id"]
    inbox = HomeInbox()
    [(identity, content)] = inbox.pending()
    db.append_message(HOME_SESSION_ID, "user", content, platform_message_id=identity)
    inbox.acknowledge_recorded()
    assert inbox.pending() == []
    assert inbox.event_receipt("goal", original, delivered=True) == identity

    goal_turn.recover()
    next_wake = _snapshot(actor, goal)["wake_id"]
    assert next_wake != original
    [pending] = inbox.pending()
    assert "reconcile any accepted tool work" in pending[1]
    goal_turn.recover()
    assert _snapshot(actor, goal)["wake_id"] == next_wake
    assert inbox.pending() == [pending]


def test_cancel_discards_only_the_goals_queued_work_and_abandoned_claim(main_home, monkeypatch):
    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, "Cancel this task")
    other_goal = _open(actor, "Keep the unrelated task", slot=2)
    with ProjectStore(actor) as store:
        worker = store.create("shared-worker", "Process assignments", "")
        origin = {"goal_project_id": goal, "wake": True}
        abandoned = store.enqueue(worker, "Old step", origin, "first")
        assert store.claim(worker)["message_id"] == abandoned
        pending = store.enqueue(worker, "Queued step", origin, "second")
        other = store.enqueue(worker, "Unrelated step", {"goal_project_id": other_goal}, "other")
        assert _goal(actor, "cancel", project_id=goal)["status"] == "cancelled"
        assert store.message(pending)["state"] == "cancelled"
        assert store.message(other)["state"] == "pending"
        monkeypatch.setattr("tools.delegate_project_store._owner_liveness", lambda: lambda *_: False)
        assert store.claim(worker)["message_id"] == other
        assert store.message(abandoned)["state"] == "cancelled"


def test_bound_wake_cannot_modify_a_second_goal(main_home):
    from tools.delegate_projects import project_action

    _, db = main_home
    actor = _actor(db)
    bound = _open(actor, "First task")
    other = _open(actor, "Second task", slot=2)
    segment = GoalSegment(None, True)
    segment.bind(bound)
    actor._agentready_active_turn.goal_segment = segment
    for operation in ("select", "checkpoint"):
        result = json.loads(dispatch(actor, {"action": "project_goal", "op": operation, "project_id": other}))
        assert "outside the scope" in result["error"]
    assert "outside the scope" in _call(actor, **_args(other))["error"]
    for action in ("archive", "stop", "send"):
        with pytest.raises(ValueError, match="outside the scope"):
            project_action(actor, action, subagent_id=other, message="Do unrelated work", event_id="second")
    with ProjectStore(actor) as store:
        other_key = store.get(other)["project_key"]
        worker = store.create("shared-pending", "Handle multiple goals", "")
        store.enqueue(worker, "Bound work", {"goal_project_id": bound}, "bound")
        store.enqueue(worker, "Unrelated work", {"goal_project_id": other}, "other")
    with pytest.raises(ValueError, match="outside the scope"):
        project_action(actor, "spawn", tasks=[{"project": other_key, "goal": "Do unrelated work"}])
    with pytest.raises(ValueError, match="outside the scope"):
        project_action(actor, "stop", subagent_id=worker["subagent_id"])
    assert [p["subagent_id"] for p in project_action(actor, "list")["projects"]] == [bound]
    assert segment.project_ids == {bound}
    assert _snapshot(actor, other)["status"] == "ready"


def test_human_followup_keeps_its_reply_during_a_quiet_goal_wake(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn, turns
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.events import event_prompt

    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, "Wait for a significant update")
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    goal_turn.finish_segment(actor, "Queued.")
    inbox = HomeInbox()
    [wake] = inbox.pending()
    followup = event_prompt("Which step are you on?", "agentready", "panel:fixture", {})
    receipt, accepted = inbox.enqueue(followup)
    assert accepted
    turn = SimpleNamespace(inbox=inbox, take_records=lambda: [(receipt, followup)])
    actor._agentready_active_turn = turn
    actor._flush_messages_to_session_db = Mock(return_value=True)

    def model(_prompt, **_kwargs):
        assert actor._mute_notification_reply is True
        messages = []
        turns.after_tool_group(actor, messages)
        assert messages[-1]["platform_message_id"] == receipt
        assert not getattr(actor, "_mute_notification_reply", False)
        return {"completed": True, "final_response": goal_turn.finish_segment(actor, "The current step is saved.")}

    result = goal_turn.run_segment(actor, turn, model, wake[1], goal_records=[wake], conversation_history=[])
    assert result["final_response"] == "The current step is saved."
    assert not result.get("_notification_reply_muted")
    assert _snapshot(actor, goal)["status"] == "ready"


def test_scoped_spawn_reuses_the_worker_after_interruption_before_first_input(main_home, monkeypatch):
    from tools.delegate_projects import project_action

    _, db = main_home
    actor = _actor(db)
    goal = _open(actor, "Reuse the reserved worker")
    segment = GoalSegment(None, True)
    segment.bind(goal)
    actor._agentready_active_turn.goal_segment = segment
    monkeypatch.setattr("tools.delegate_projects.recover_projects", Mock(return_value=[]))
    args = {"tasks": [{"goal": "Handle the verified next step"}], "event_id": "goal-step"}
    with monkeypatch.context() as interrupted:
        interrupted.setattr(ProjectStore, "enqueue", Mock(side_effect=OSError("interrupted after create")))
        with pytest.raises(OSError, match="interrupted"):
            project_action(actor, "spawn", **args)
    with ProjectStore(actor) as store:
        [reserved] = [p for p in store.projects() if p["subagent_id"] != goal]
    resumed = project_action(actor, "spawn", **args)["projects"][0]
    assert resumed["subagent_id"] == reserved["subagent_id"]
    assert project_action(actor, "spawn", **args)["projects"][0]["message_id"] == resumed["message_id"]


def test_expired_input_is_removed_from_mixed_segment_and_retired(main_home, monkeypatch):
    from hermes_runtime.adapters import goal_turn
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.events import event_prompt

    _, db = main_home
    actor = _actor(db)
    abandoned = _open(actor, "Cancelled task")
    monkeypatch.setattr("hermes_runtime.adapters.state.main_gateway_origin", dict)
    goal_turn.finish_segment(actor, "Queued.")
    inbox = HomeInbox()
    [obsolete] = inbox.pending()
    _goal(actor, "cancel", project_id=abandoned)

    followup = event_prompt("Check the current status", "agentready", "panel:new-input", {})
    identity, _ = inbox.enqueue(followup)
    records = [obsolete, (identity, followup)]
    turn = SimpleNamespace(inbox=inbox)
    actor._agentready_active_turn = turn

    def model(prompt, **kwargs):
        assert "Check the current status" in prompt and "Continue the saved Project Goal" not in prompt
        assert kwargs["persist_user_platform_id"] == identity
        assert kwargs["persist_user_display_metadata"]["agentready_inbox_ids"] == [identity]
        return {"completed": True, "final_response": "Current status."}

    result = goal_turn.run_segment(actor, turn, model, "old and new inputs", goal_records=records,
                                   goal_steer="A separate clarification", conversation_history=[])
    assert result["final_response"] == "Current status."
    assert inbox.pending() == [(identity, followup)]
