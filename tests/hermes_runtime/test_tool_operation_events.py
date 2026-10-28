"""A new inbox event releases the model without rerunning a slow tool."""

import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.waiting import wait_until


def test_pending_event_does_not_override_stop_or_unmanaged_execution(monkeypatch):
    from agent import tool_operation_lifecycle

    wakeup = threading.Event()
    wakeup.set()
    agent = SimpleNamespace(_agentready_active_turn=SimpleNamespace(wakeup=wakeup), _interrupt_requested=False)
    monkeypatch.setattr(tool_operation_lifecycle, "enabled", lambda: False)
    assert not tool_operation_lifecycle.incoming_event(agent)
    monkeypatch.setattr(tool_operation_lifecycle, "enabled", lambda: True)
    assert tool_operation_lifecycle.incoming_event(agent)
    agent._interrupt_requested = True
    assert not tool_operation_lifecycle.incoming_event(agent)


@pytest.fixture
def operation_home(monkeypatch):
    from hermes_runtime.adapters import hermes
    from hermes_runtime.adapters.state import ensure_home_session

    with tempfile.TemporaryDirectory(prefix="hop-") as directory:
        home = Path(directory)
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
        monkeypatch.setattr(hermes, "_home", home.resolve())
        ensure_home_session()
        yield home
        from hermes_logging import _reset_queued_handlers
        _reset_queued_handlers()


def test_incoming_event_releases_wait_and_completion_is_delivered_once(operation_home, monkeypatch):
    from agent import tool_executor
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.turns import ActiveHomeTurn, forward_to_active_turn
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from hermes_state import SessionDB

    started, release, returned = threading.Event(), threading.Event(), threading.Event()
    calls, results = [], []
    turn = ActiveHomeTurn()
    agent = SimpleNamespace(session_id="agentready_home", _agentready_active_turn=turn,
        _managed_tool_call_uids={"tc_slow": "uid_slow"}, _managed_tool_group="group-1",
        _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
        _interrupt_requested=False, _touch_activity=lambda _: None)

    def middleware(_agent, **kwargs):
        calls.append(kwargs["tool_call_id"])
        started.set()
        assert release.wait(10)
        return tool_executor._ManagedToolResult(result='{"answer":42}', args={},
            middleware_trace=[], blocked=False, dispatched=True)

    monkeypatch.setattr(tool_executor, "_run_agent_tool_execution_middleware", middleware)
    monkeypatch.setattr(tool_executor, "_resolve_sequential_tool_timeout", lambda: None)

    def run_tool():
        results.append(tool_executor._run_sequential_tool_execution_middleware(agent,
            function_name="mcp_example_slow", function_args={}, effective_task_id=agent.session_id,
            tool_call_id="tc_slow", execute=lambda _: None))
        returned.set()

    from agent.memory_provider import spawn_context_thread
    runner = spawn_context_thread(run_tool, name="test-incoming-tool-event")
    try:
        runner.start()
        assert started.wait(5)
        assert forward_to_active_turn("Answer this Telegram message now")
        assert returned.wait(3), "the model must resume before the tool completes"
        receipt = json.loads(results[0].result)
        assert receipt["status"] == "running"
        assert turn.take() == ["Answer this Telegram message now"]
        release.set()
        inbox = HomeInbox()
        assert wait_until(lambda: len(inbox.pending()) == 2, timeout=5)
        completion = next((identity, text) for identity, text in inbox.pending() if receipt["operation_id"] in text)
        data = json.loads(completion[1].split("[UNTRUSTED EVENT DATA]\n", 1)[1].split("\n[/UNTRUSTED", 1)[0])
        assert json.loads(data["result"])["answer"] == 42
        assert calls == ["tc_slow"]
        # A crash after the canonical append, before inbox acknowledgement,
        # must not duplicate this completion on startup recovery.
        db = SessionDB()
        try:
            db.append_message(agent.session_id, "user", completion[1], platform_message_id=completion[0])
        finally:
            db.close()
        store = ToolOperationStore(operation_home)
        store.recover()
        store.recover()
        assert not any(receipt["operation_id"] in text for _, text in inbox.pending())
        assert calls == ["tc_slow"]
    finally:
        release.set()
        runner.join(5)
        assert wait_until(lambda: not agent._tool_worker_threads, timeout=5)
        turn.finish(error="test finished")


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("entry", ["redirect", "gateway_text", "gateway_voice", "gateway_photo"])
def test_real_main_turn_answers_new_event_before_tool_finishes(operation_home, monkeypatch, parallel, entry):
    from openai.types.chat import ChatCompletion
    from unittest.mock import patch
    from agent.memory_provider import spawn_context_thread
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_state import SessionDB
    from run_agent import AIAgent

    started, release, answered = threading.Event(), threading.Event(), threading.Event()
    executed, requests, outcome, errors = [], [], [], []
    db = SessionDB()
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="test-only", base_url="http://127.0.0.1:9/v1", provider="custom",
            model="gpt-4.1", quiet_mode=True, session_db=db, session_id="agentready_home",
            skip_context_files=True, skip_memory=True, enabled_toolsets=["web", "terminal"])
    agent.compression_enabled, agent.save_trajectories, agent.tool_delay = False, False, 0

    def model(payload, *args, **kwargs):
        requests.append(payload["messages"])
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "slow-read", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"slow"}'}},
                {"id": "dependent-write", "type": "function", "function": {
                    "name": "web_extract" if parallel else "terminal",
                    "arguments": '{"urls":["https://example.test"]}' if parallel else '{"command":"printf result > answer.txt"}'}}]}
            reason = "tool_calls"
        else:
            assert "Answer Telegram now" in json.dumps(payload["messages"])
            assert not release.is_set()
            answered.set()
            message, reason = {"role": "assistant", "content": "Telegram acknowledged; search still running."}, "stop"
        return ChatCompletion(id="reply", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": message, "finish_reason": reason}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})

    def handler(name, args, *a, **kw):
        executed.append(name)
        assert name in {"web_search", "web_extract"}, "dependent command must not overlap its unfinished prerequisite"
        started.set()
        assert release.wait(15)
        return '{"result":"Search finished"}'

    monkeypatch.setattr(agent, "_interruptible_api_call", model)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", model)
    monkeypatch.setattr("model_tools.handle_function_call", handler)

    def run():
        try:
            outcome.append(agent.run_conversation("Search and then write its result", task_id=agent.session_id))
        except BaseException as error:
            errors.append(error)

    owner = spawn_context_thread(run, name="test-main-tool-event")
    try:
        owner.start()
        assert started.wait(8), errors
        if parallel:
            assert wait_until(lambda: len(executed) == 2, timeout=5)
        monkeypatch.setattr("agent.interrupt_control._request_yield",
            lambda *_: pytest.fail("managed calls must use operation receipts"))
        if entry == "redirect":
            # Direct redirects must also avoid the native Terminal yield shortcut.
            assert agent.redirect("Answer Telegram now")
        else:
            import asyncio
            from unittest.mock import AsyncMock, Mock
            from gateway.config import GatewayConfig, Platform
            from gateway.platforms.event import MessageEvent, MessageType
            from gateway.run import GatewayRunner
            from gateway.session import SessionSource
            from hermes_runtime.sessions import HOME_SESSION_KEY

            host = GatewayRunner(config=GatewayConfig())
            host._busy_input_mode = "interrupt"
            host._session_state(HOME_SESSION_KEY).turn.agent = agent
            host._is_user_authorized_for_source = lambda _: True
            host._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="Answer Telegram now")
            host._consume_pending_native_image_paths = lambda _: []
            source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm")
            event = MessageEvent(text="Answer Telegram now", source=source,
                message_type=MessageType(entry.removeprefix("gateway_")), message_id="event-42")
            with patch.object(agent, "interrupt", Mock()) as interrupt, patch.object(agent, "redirect", Mock()) as redirect:
                assert asyncio.run(host._hm_busy_preempt(event, source, HOME_SESSION_KEY))
                interrupt.assert_not_called()
                redirect.assert_not_called()
        assert answered.wait(5), errors
        owner.join(5)
        assert not owner.is_alive()
        assert not errors
        assert outcome[0]["completed"]
        assert sorted(executed) == (["web_extract", "web_search"] if parallel else ["web_search"])
        rows = db.get_messages_as_conversation(agent.session_id)
        results = [r for r in rows if r["role"] == "tool"]
        assert len(results) == 2
        assert '"status": "running"' in results[0]["content"]
        assert ('"status": "running"' if parallel else '"status": "not_started"') in results[1]["content"]
        assert {r["tool_call_id"] for r in results} == {"slow-read", "dependent-write"}
        release.set()
        assert wait_until(lambda: any("Search finished" in text for _, text in HomeInbox().pending()), timeout=5)
        assert wait_until(lambda: not agent._tool_worker_threads, timeout=5)
        assert agent.session_id == "agentready_home"
    finally:
        release.set()
        owner.join(5)
        db.close()


def test_code_script_keeps_running_while_main_answers_a_new_event(operation_home, monkeypatch):
    from openai.types.chat import ChatCompletion
    from unittest.mock import patch
    import model_tools
    from agent.memory_provider import spawn_context_thread
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_state import SessionDB
    from run_agent import AIAgent
    from tools.code_kernel import shutdown_all_kernels

    started, release, answered = threading.Event(), threading.Event(), threading.Event()
    calls, requests, results, errors = [], [], [], []
    db = SessionDB()
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="test-only", base_url="http://127.0.0.1:9/v1", provider="custom",
            model="gpt-4.1", quiet_mode=True, session_db=db, session_id="agentready_home",
            skip_context_files=True, skip_memory=True, enabled_toolsets=["file", "terminal", "code_execution"])
    agent.compression_enabled, agent.save_trajectories, agent.tool_delay = False, False, 0
    original = model_tools.handle_function_call
    def handler(name, args, *a, **kw):
        if name == "read_file":
            calls.append(name)
            started.set()
            assert release.wait(20)
            return '{"answer":"code script finished"}'
        return original(name, args, *a, **kw)
    monkeypatch.setattr(model_tools, "handle_function_call", handler)

    def model(payload, *args, **kwargs):
        requests.append(payload["messages"])
        if len(requests) == 1:
            code = 'from hermes_tools import call_tool\nprint(call_tool("read_file", {"path":"demo"}, step="slow"))'
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "code-script", "type": "function",
                "function": {"name": "execute_code", "arguments": json.dumps({"code": code})}}]}
            reason = "tool_calls"
        else:
            assert "Answer Telegram now" in json.dumps(payload["messages"])
            assert not release.is_set()
            answered.set()
            message, reason = {"role": "assistant", "content": "Telegram acknowledged; script still running."}, "stop"
        return ChatCompletion(id="reply", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": message, "finish_reason": reason}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
    monkeypatch.setattr(agent, "_interruptible_api_call", model)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", model)
    def run():
        try:
            results.append(agent.run_conversation("Run the demo script", task_id=agent.session_id))
        except BaseException as exc:
            errors.append(exc)
    owner = spawn_context_thread(run, name="test-code-mode-event")
    try:
        owner.start()
        assert started.wait(10), errors
        assert agent.redirect("Answer Telegram now")
        assert answered.wait(5), errors
        owner.join(5)
        assert not owner.is_alive()
        assert not errors
        assert results[0]["completed"]
        release.set()
        assert wait_until(lambda: any("code script finished" in text for _, text in HomeInbox().pending()), timeout=8)
        rows = db.get_messages_as_conversation(agent.session_id)
        tool_rows = [r for r in rows if r["role"] == "tool"]
        assert len(tool_rows) == 1  # Internal steps never become extra model tool results.
        assert tool_rows[0]["tool_call_id"] == "code-script"
        assert '"status": "running"' in tool_rows[0]["content"]
        assert calls == ["read_file"]
        completions = [text for _, text in HomeInbox().pending() if "code script finished" in text]
        assert len(completions) == 1
        assert "agentready_home" in completions[0]
        assert "code_run_id" in completions[0]
    finally:
        release.set()
        owner.join(5)
        assert wait_until(lambda: not agent._tool_worker_threads, timeout=5)
        shutdown_all_kernels()
        db.close()


def test_crashed_script_recovery_offers_saved_run_without_child_inbox_events(operation_home):
    import os
    import subprocess
    import sys
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from hermes_state import SessionDB
    worker = '''
import os
from hermes_constants import get_hermes_home
from hermes_runtime.adapters.tool_operations import ToolOperationStore
from tools.code_execution_runs import CodeRun
store = ToolOperationStore(get_hermes_home())
row, _ = store.admit("agentready_home", "code-owner-uid", "code-owner-call", "execute_code", {}, "tool:execute_code", "g")
run = CodeRun("agentready_home", "print('saved')", "code-owner-uid")
run.call("read_file", {"path":"demo"}, lambda *_: '{"read":true}', step="read")
os._exit(0)
'''
    db = SessionDB()
    try:
        before = db.get_messages_as_conversation("agentready_home")
        subprocess.run([sys.executable, "-c", worker], check=True, timeout=30, env=dict(os.environ),
            cwd=Path(__file__).resolve().parents[2])
        store = ToolOperationStore(operation_home)
        store.recover()
        pending = HomeInbox().pending()
        assert len(pending) == 1
        assert "resume_run_id" in pending[0][1]
        assert "agentready_home" in pending[0][1]
        assert "read_file" not in pending[0][1]
        assert db.get_messages_as_conversation("agentready_home") == before
    finally:
        db.close()


@pytest.mark.parametrize("phase", ["running", "completed"])
def test_process_loss_preserves_outcome_without_reexecuting(operation_home, phase):
    import os
    import subprocess
    import sys
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    script = """
import os, sys
from pathlib import Path
from hermes_runtime.adapters.tool_operations import ToolOperationStore
home = Path(sys.argv[1])
store = ToolOperationStore(home)
row, execute = store.admit('agentready_home', 'call-uid', 'call-1', 'mcp_write',
    {'order': 42}, 'mcp:orders', 'group-crash')
assert execute
(home / 'executions').write_text('one external call')
if sys.argv[2] == 'completed':
    store.complete(row['operation_id'], {'remote_job_id': 'job-42', 'accepted': True})
os._exit(23)
"""
    result = subprocess.run([sys.executable, "-c", script, str(operation_home), phase],
        env={**os.environ, "HERMES_HOME": str(operation_home)}, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 23, result.stderr
    if phase == "running":
        # A repaired protocol placeholder is not proof that the external
        # operation finished; its durable unknown outcome must still arrive.
        from hermes_state import SessionDB
        with SessionDB() as db:
            db.append_messages_batch("agentready_home", [
                {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function",
                    "function": {"name": "mcp_write", "arguments": "{}"}}], "_tool_call_uids": {"call-1": "call-uid"}},
                {"role": "tool", "tool_call_id": "call-1", "content": "Result unavailable after interruption",
                    "_tool_call_uid": "call-uid"}])
    store = ToolOperationStore(operation_home)
    store.recover()
    store.recover()
    pending = HomeInbox().pending()
    assert len(pending) == 1
    expected = "outcome_unknown" if phase == "running" else "completed"
    assert expected in pending[0][1]
    if phase == "completed":
        assert "job-42" in pending[0][1]
    row, execute = store.admit('agentready_home', 'call-uid', 'call-1', 'mcp_write',
        {'order': 42}, 'mcp:orders', 'another-group')
    assert not execute
    assert row["state"] == expected
    assert (operation_home / "executions").read_text() == "one external call"


def test_no_event_keeps_normal_result_and_resource_is_released(operation_home):
    from agent.tool_operation_lifecycle import ToolOperation
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from hermes_state import SessionDB

    store = ToolOperationStore(operation_home)
    first, execute = store.admit("agentready_home", "uid-a", "call-a", "browser_exec", {}, "desktop", "group-a")
    assert execute
    blocked, execute = store.admit("agentready_home", "uid-b", "call-b", "browser_exec", {}, "desktop", "group-b")
    assert not execute and blocked["status"] == "not_started"
    # An independent network request can run while the desktop is occupied.
    independent, execute = store.admit("agentready_home", "uid-c", "call-c", "web_search", {}, "web", "group-c")
    assert execute
    store.complete(independent["operation_id"], "network answer")
    store.complete(first["operation_id"], "browser answer")
    store.recover()  # the owner is alive and about to commit the normal result
    assert HomeInbox().pending() == []
    db = SessionDB()
    try:
        db.append_messages_batch("agentready_home", [
            {"role": "assistant", "tool_calls": [{"id": "call-a", "type": "function",
                "function": {"name": "browser_exec", "arguments": "{}"}}], "_tool_call_uids": {"call-a": "uid-a"}},
            {"role": "tool", "tool_call_id": "call-a", "content": "browser answer", "_tool_call_uid": "uid-a"}])
    finally:
        db.close()
    store.recover()
    assert store.get(first["operation_id"])["delivered_at"] is not None
    assert HomeInbox().pending() == []
    _, execute = store.admit("agentready_home", "uid-d", "call-d", "browser_exec", {}, "desktop", "group-d")
    assert execute
    # Replaying the exact durable call returns its saved result, not another execution.
    replayed, execute = store.admit("agentready_home", "uid-a", "call-a", "browser_exec", {}, "desktop", "group-new")
    operation = ToolOperation(SimpleNamespace(), store, replayed, execute)
    assert operation.replay() == "browser answer"


def test_completion_keeps_its_origin_home_after_profile_scope_changes(operation_home):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    original = ToolOperationStore(operation_home)
    row, execute = original.admit("agentready_home", "shared-uid", "call-1", "web_search", {}, "web", "group-1")
    assert execute
    original.defer(row["operation_id"])
    other_home = operation_home / "other-profile"
    token = set_hermes_home_override(other_home)
    try:
        original.complete(row["operation_id"], "Result for the original profile")
        other = ToolOperationStore(other_home)
        _, execute = other.admit("agentready_home", "shared-uid", "call-1", "web_search", {}, "web", "group-1")
        assert execute, "a call identity from another profile must not suppress this dispatch"
        assert HomeInbox(other_home).pending() == []
    finally:
        reset_hermes_home_override(token)
    assert len(HomeInbox(operation_home).pending()) == 1


def test_stop_cancels_queued_call_without_claiming_running_call_was_cancelled(operation_home):
    from concurrent.futures import Future
    from agent.tool_concurrent_batch import ConcurrentBatch
    from agent.tool_executor import _ParsedCall

    agent = SimpleNamespace(session_id="agentready_home", _interrupt_requested=True,
        _agentready_active_turn=SimpleNamespace(wakeup=threading.Event()),
        _managed_tool_call_uids={"call-0": "uid-0", "call-1": "uid-1"}, _managed_tool_group="stop-group")
    calls = [_ParsedCall(SimpleNamespace(id=f"call-{i}"), name, {}, [], None, None)
        for i, name in enumerate(["web_search", "web_extract"])]
    batch = ConcurrentBatch(agent, [], agent.session_id, calls, None)
    running, queued = Future(), Future()
    assert running.set_running_or_notify_cancel()
    assert batch.await_completion([running, queued], {running: 0, queued: 1}, None)
    assert queued.cancelled()
    assert running.running()
    assert batch.defer_status == "outcome_unknown"
    cancelled = batch.operations[1].store.get(batch.operations[1].row["operation_id"])
    assert json.loads(json.loads(cancelled["result_json"]))["status"] == "cancelled"
    outstanding = batch.operations[0].store.get(batch.operations[0].row["operation_id"])
    assert outstanding["state"] == "running"


def test_late_completion_wakes_dispatching_chat_after_restart(operation_home):
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.startup import _wake_inbox
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from tui_gateway.server import _session_owns_notification_event

    tokens = set_session_vars(platform="telegram", chat_id="42", chat_type="dm", user_id="42",
        thread_id="7", session_key="agent:main:agentready:home", session_id="agentready_home")
    try:
        store = ToolOperationStore(operation_home)
        row, execute = store.admit("agentready_home", "origin-uid", "origin-call", "terminal", {}, "workspace", "origin-group")
    finally:
        clear_session_vars(tokens)
    assert execute
    # A different surface now owns the ambient context. Reopening the store
    # still delivers to the dispatching Telegram topic and the same home.
    restarted = ToolOperationStore(operation_home)
    restarted.defer(row["operation_id"])
    restarted.complete(row["operation_id"], "finished")
    # Completion publication itself offers the routed wake immediately, without
    # waiting for the Gateway's orphan sweep or a subsequent inbound message.
    assert not _wake_inbox(HomeInbox()), "one durable wake per pending batch"
    with HomeInbox().transaction() as conn:
        event = json.loads(conn.execute("SELECT event_json FROM async_delegations").fetchone()[0])
    assert event["parent_session_id"] == "agentready_home"
    assert event["session_key"] == "agent:main:agentready:home"
    assert event["chat_id"] == "42" and event["thread_id"] == "7"
    assert not _session_owns_notification_event("web", {"session_key": "agentready_home"}, event)
    # Prove native automation can resolve that address in the real managed
    # routing index. A standard per-Telegram key is not registered here.
    from gateway.config import GatewayConfig, Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionStore, SessionSource
    from gateway.session_automation import _owner
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", chat_type="dm", user_id="42", thread_id="7")
    routes = SessionStore(operation_home / "sessions", GatewayConfig())
    entry = routes.get_or_create_session(source)
    wake = MessageEvent(text="Saved result is ready", message_type=MessageType.TEXT, source=source, internal=True,
        metadata={"gateway_session_key": event["session_key"], "gateway_session_id": event["parent_session_id"]})
    assert _owner(SimpleNamespace(session_store=routes), wake, SimpleNamespace()).session_id == entry.session_id


def test_recovery_does_not_wake_while_home_writer_owns_inbox(operation_home):
    import fcntl
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.adapters.startup import _wake_inbox
    from hermes_runtime.adapters.turns import lock_path

    inbox = HomeInbox()
    inbox.enqueue("A message accepted while the main turn is still running")
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    with lock_path().open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        assert not _wake_inbox(inbox)
    with inbox.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0] == 0
    assert _wake_inbox(inbox)
