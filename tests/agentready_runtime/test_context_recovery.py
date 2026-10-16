"""Managed overflow recovers in-place; Gateway never rotates its home on failure."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.waiting import wait_until


@pytest.fixture
def recovery_home(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ar-") as directory:
        monkeypatch.setenv("HERMES_HOME", directory)
        monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", Path(directory) / "state.db")
        yield Path(directory)


@pytest.mark.parametrize("failure", ["summary", "recompression", "timeout", "large_tail", "manual", "archive_error", "cancel"])
def test_failed_compression_recovers_and_continues_with_all_history(recovery_home, monkeypatch, failure):
    from agentready_runtime.adapters import hermes
    from agentready_runtime.adapters.state import ensure_home_session
    from agentready_runtime.sessions import HOME_SESSION_ID
    from agent.context_compressor import ContextCompressor
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    from run_agent import AIAgent

    home = recovery_home
    assert Path(get_hermes_home()) == home
    monkeypatch.setattr(hermes, "_home", home.resolve())
    ensure_home_session()
    db = SessionDB()
    messages = []
    for index in range(40):
        messages.extend([
            {"role": "user", "content": f"Request {index}: search-needle-{index} " + "business history " * 300},
            {"role": "assistant", "content": f"Already completed {index}. " + "past result " * 300},
        ])
    messages.append({"role": "user", "content": "Continue invoice INV-42; do not send it twice."})
    if failure == "large_tail":
        messages.append({"role": "assistant", "content": "INV-42 was already sent: " + "large delivery report " * 20000})
    for message in messages:
        db.append_message(HOME_SESSION_ID, message["role"], message["content"])
    before = db.get_messages(HOME_SESSION_ID, include_inactive=True)
    before_ids = {row["id"] for row in before}

    def failed_summary(**kwargs):
        from openai.types.chat import ChatCompletion
        if failure == "cancel":
            from agent.auxiliary_client import AuxiliaryExplicitCancellation
            agent._hard_interrupt_requested.set()
            raise AuxiliaryExplicitCancellation()
        if failure == "timeout":
            time.sleep(0.5)  # health: allow HX013 -- deliberately finish the provider after its 0.3s compression deadline
            return ChatCompletion(id="late", object="chat.completion", created=1, model="gpt-4.1", choices=[{
                "index": 0, "message": {"role": "assistant", "content":
                    "## Historical Task Snapshot\nUser asked: keep working.\n\n## Active State\nLATE_CANDIDATE"},
                "finish_reason": "stop"}])
        if failure == "recompression" and not getattr(failed_summary, "called", False):
            failed_summary.called = True
            return ChatCompletion(id="test", object="chat.completion", created=1, model="gpt-4.1", choices=[{
                "index": 0, "message": {"role": "assistant", "content":
                    "## Historical Task Snapshot\nUser asked: keep working.\n\n## Active State\nInvoice INV-42 is pending."
                    + "\n\n## Completed Actions\n" + "Repeated detail. " * 2000}, "finish_reason": "stop"}])
        raise RuntimeError("summary service unavailable")

    monkeypatch.setattr("agent.context_compressor.call_llm", failed_summary)
    compression_futures = []
    if failure == "timeout":
        monkeypatch.setattr("agent.conversation_compression.resolve_context_compression_timeouts", lambda: (0.15, 0.3))
        from agent.conversation_compression import _get_compress_timeout_executor
        executor = _get_compress_timeout_executor()
        submit = executor.submit

        def record_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            compression_futures.append(future)
            return future

        monkeypatch.setattr(executor, "submit", record_submit)
    if failure == "archive_error":
        def unavailable_archive(*args, **kwargs):
            raise OSError("archive storage unavailable")
        monkeypatch.setattr("agentready_runtime.adapters.checkpoint_files.FileCheckpointArchive.save", unavailable_archive)
    with patch("agent.process_bootstrap.OpenAI"):
        agent = AIAgent(api_key="test-key-1234567890", base_url="http://127.0.0.1:9/v1",
                        provider="custom", model="gpt-4.1", quiet_mode=True, session_db=db,
                        session_id=HOME_SESSION_ID, skip_context_files=True, skip_memory=True,
                        enabled_toolsets=["file"])
    agent.compression_in_place = True
    agent.context_compressor = ContextCompressor(
        model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1", api_key="test-only",
        config_context_length=64000, protect_first_n=1, protect_last_n=2,
        tail_mode="legacy", abort_on_summary_failure=True, quiet_mode=True)
    agent.context_compressor.bind_session_state(db, HOME_SESSION_ID)
    # Model-reported pressure admits automatic preflight (rough estimates alone
    # deliberately defer to real usage in upstream Hermes).
    agent.context_compressor.last_prompt_tokens = 100000
    agent.context_compressor.last_real_prompt_tokens = 100000
    message = SimpleNamespace(content="Invoice checked; no duplicate delivery.", tool_calls=None)
    reply = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                            model="gpt-4.1", usage=SimpleNamespace(
                                prompt_tokens=2000, completion_tokens=10, total_tokens=2010))
    agent.client, agent.tool_delay, agent.save_trajectories = MagicMock(), 0, False
    agent.client.chat.completions.create.return_value = reply
    try:
        if failure == "manual":
            agent._compress_context(db.get_messages_as_conversation(HOME_SESSION_ID), "System", force=True)
            result = {"completed": True}
        else:
            result = agent.run_conversation("Continue checking INV-42.", task_id=HOME_SESSION_ID)
        after = db.get_messages(HOME_SESSION_ID, include_inactive=True)
        assert {row["id"]: row["content"] for row in before} == {
            row["id"]: row["content"] for row in after if row["id"] in before_ids}
        assert agent.session_id == HOME_SESSION_ID
        assert db.get_session(HOME_SESSION_ID)["ended_at"] is None
        if failure in ("archive_error", "cancel"):
            assert result.get("completed") is not True
            assert not getattr(agent, "_last_compaction_in_place", False)
            agent.client.chat.completions.create.assert_not_called()
            return
        assert result.get("completed") is True and not result.get("failed"), result
        compacted = db.get_messages_as_conversation(HOME_SESSION_ID)
        assert len(compacted) < len(messages), "failed summary must recover instead of returning the full context"
        assert agent._last_compaction_in_place is True
        assert before_ids <= {row["id"] for row in after}
        archives = list((home / "workspace/.agentready/checkpoints").rglob("*.md"))
        assert any("search-needle-0" in archive.read_text() and "search-needle-39" in archive.read_text()
                   for archive in archives), "full input, not a guessed summary, must survive recovery"
        assert "INV-42" in json.dumps(compacted)
        displayed = db.get_resume_conversations(HOME_SESSION_ID)[1]
        for row in before:
            assert sum(str(message.get("content", "")).strip() == row["content"].strip()
                       for message in displayed) == 1, (row["role"], row["content"][:90])

        from agent.model_metadata import estimate_request_tokens_rough
        for call in agent.client.chat.completions.create.call_args_list:
            assert estimate_request_tokens_rough(call.kwargs["messages"], tools=call.kwargs.get("tools")) < 64000
        result = agent.run_conversation("Confirm the status of INV-42.", task_id=HOME_SESSION_ID)
        assert result.get("completed") is True and not result.get("failed"), result
        assert agent.session_id == HOME_SESSION_ID
        assert agent.client.chat.completions.create.called
        if failure == "timeout":
            wait_until(lambda: all(future.done() for future in compression_futures), timeout=10,
                       what="retired compression workers to finish")
            assert "LATE_CANDIDATE" not in json.dumps(db.get_messages_as_conversation(HOME_SESSION_ID))
    finally:
        db.close()


@pytest.mark.parametrize("managed", [True, False])
def test_gateway_preserves_managed_home_on_compression_exhaustion(monkeypatch, managed):
    from agentready_runtime.adapters import hermes
    from gateway.run_turn import GatewayTurnMixin
    from hermes_constants import get_hermes_home

    monkeypatch.setattr(hermes, "_home", Path(get_hermes_home()).resolve() if managed else None)
    entry = SimpleNamespace(session_id="agentready_home")
    host = SimpleNamespace(async_session_store=SimpleNamespace(reset_session=AsyncMock(return_value=None)),
                           _evict_cached_agent=MagicMock(), _clear_conversation_scope=MagicMock())
    response, returned = asyncio.run(GatewayTurnMixin._hmwa_compression_exhaustion_reset(
        host, {"compression_exhausted": True}, "Compression failed.", entry,
        "agent:main:agentready:home", SimpleNamespace()))
    if managed:
        host.async_session_store.reset_session.assert_not_awaited()
        host._evict_cached_agent.assert_not_called()
        assert returned is entry and "auto-reset" not in response
    else:
        host.async_session_store.reset_session.assert_awaited_once()
        assert "auto-reset" in response
