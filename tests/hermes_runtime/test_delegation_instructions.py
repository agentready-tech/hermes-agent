"""The first real API request carries mandatory main policy without changing saved context."""

import sqlite3
import threading
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from tests.tools.test_delegate_mail_live import _response

HEADER = "## Mandatory native delegation for the main conversation"
GOOGLE_ROUTE = "skill_view(name='agentsee-google-browser-login')"


def _agent(db, sid, **kwargs):
    from run_agent import AIAgent

    options = dict(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1",
        api_key="test-only", api_mode="chat_completions", session_id=sid, session_db=db,
        enabled_toolsets=["terminal", "delegation"], skip_context_files=True, load_soul_identity=False,
        skip_memory=True, quiet_mode=True, ephemeral_system_prompt="Existing host instructions")
    return AIAgent(**{**options, **kwargs})


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_runtime.adapters import hermes
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hermes, "_home", None)
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append(payload)
        return _response("Ready to coordinate the requested work")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        yield tmp_path, db, seen


@pytest.mark.parametrize("managed", [False, True], ids=["ordinary-owner", "managed"])
def test_first_request_installs_common_policy_without_managed_adapter_or_skill(runtime, managed):
    from hermes_runtime.standalone.config import activate_home
    from hermes_runtime.sessions import HOME_SESSION_ID

    home, db, seen = runtime
    if managed:
        activate_home(home)
    sid = HOME_SESSION_ID if managed else "ordinary-main"
    db.create_session(sid, source="cli")
    agent = _agent(db, sid)
    try:
        assert agent.ephemeral_system_prompt.count(HEADER) == 1
        assert agent.ephemeral_system_prompt.count(GOOGLE_ROUTE) == 1
        agent.run_conversation("Analyse the supplier and delivery tasks independently")
        system = seen[0]["messages"][0]["content"]
        assert system.count(HEADER) == 1
        assert system.count(GOOGLE_ROUTE) == 1
        assert "Google account browser sign-in, the main coordinator MUST first read" in system
        assert "authorized protected vault autofill as the skill describes" in system
        assert "from the first user request" in system
        assert "delegate the new task and retain the original work" in system
        assert "long-running work or waiting in a native persistent asynchronous child" in system
        assert "run them in parallel within available capacity" in system
        assert "Dispatch is not completion" in system
        assert "finite caller retains its native join" in system
        assert "shared browser/desktop leases or human control" in system
        assert "delegate_task" in agent.valid_tool_names
        assert not {"skill_view", "skills_list"} & agent.valid_tool_names
        assert "Existing host instructions" in system
        from hermes_runtime.standalone.instructions import install_main_schedule_instructions
        suffix = agent.ephemeral_system_prompt
        install_main_schedule_instructions(agent)
        assert agent.ephemeral_system_prompt == suffix
    finally:
        agent.close()


def test_google_login_first_request_exposes_skill_view_without_prior_catalog_or_skill_read(runtime, monkeypatch):
    _, db, seen = runtime
    legacy = "Cached main prefix with no skill catalog."
    db.create_session("skills-main", source="cli", system_prompt=legacy)
    db.append_message("skills-main", "user", "Original task")
    db.append_message("skills-main", "assistant", "Preserved result")
    catalog = Mock(side_effect=AssertionError("A cached prefix must not reload the skill catalog"))
    view = Mock(side_effect=AssertionError("No skill has been read before the first model request"))
    listing = Mock(side_effect=AssertionError("No skill listing preceded the first model request"))
    monkeypatch.setattr("agent.prompt_builder.build_skills_system_prompt", catalog)
    monkeypatch.setattr("tools.skills_tool.skill_view", view)
    monkeypatch.setattr("tools.skills_tool.skills_list", listing)
    agent = _agent(db, "skills-main", enabled_toolsets=["terminal", "delegation", "skills"])
    try:
        assert not seen
        assert "skill_view" in agent.valid_tool_names
        agent.run_conversation("Sign in to my Google account in the browser",
                               conversation_history=db.get_messages_as_conversation("skills-main"))
        assert seen[0]["messages"][0]["content"].count(GOOGLE_ROUTE) == 1
        assert any(tool.get("function", {}).get("name") == "skill_view" for tool in seen[0]["tools"])
        assert agent._cached_system_prompt == legacy
        assert "agentsee-google-browser-login" not in agent._cached_system_prompt
        catalog.assert_not_called()
        view.assert_not_called()
        listing.assert_not_called()
    finally:
        agent.close()


@pytest.mark.parametrize("role", ["child-context", "child-process", "side", "subagent", "cron", "board"])
def test_assigned_workers_do_not_get_main_routing_policy(runtime, monkeypatch, role):
    from agent.delegation_context import delegated_child_context

    _, db, _ = runtime
    db.create_session("assigned", source="cli")
    if role == "child-process":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    if role == "board":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "isolated-assigned-task")
    options = {"side_agent": True} if role == "side" else {}
    if role in {"subagent", "cron"}:
        options["platform"] = role
    scope = delegated_child_context("assigned") if role == "child-context" else nullcontext()
    with scope:
        agent = _agent(db, "assigned", **options)
    try:
        assert HEADER not in (agent.ephemeral_system_prompt or "")
        assert GOOGLE_ROUTE not in (agent.ephemeral_system_prompt or "")
        assert "Existing host instructions" in agent.ephemeral_system_prompt
    finally:
        agent.close()


def test_resumed_prefix_old_rows_and_compressed_main_lineage_stay_intact(runtime):
    home, db, seen = runtime
    legacy = "Previously saved immutable system bytes."
    db.create_session("ordinary-main", source="cli", system_prompt=legacy)
    db.append_message("ordinary-main", "user", "Original task")
    db.append_message("ordinary-main", "assistant", "Original finding")
    with sqlite3.connect(home / "state.db") as conn:
        before = conn.execute("SELECT id,role,content,api_content,platform_message_id FROM messages ORDER BY id").fetchall()
    for turn in range(2):
        agent = _agent(db, "ordinary-main")
        try:
            agent.run_conversation(f"Continue {turn}", conversation_history=db.get_messages_as_conversation("ordinary-main"))
            assert agent._cached_system_prompt == legacy
            assert "agentsee-google-browser-login" not in agent._cached_system_prompt
            assert seen[-1]["messages"][0]["content"].count(GOOGLE_ROUTE) == 1
            assert agent.ephemeral_system_prompt.count(HEADER) == 1
        finally:
            agent.close()
    assert seen[0]["messages"][0] == seen[1]["messages"][0]
    assert db.get_session("ordinary-main")["system_prompt"] == legacy
    with sqlite3.connect(home / "state.db") as conn:
        assert [conn.execute("SELECT id,role,content,api_content,platform_message_id FROM messages WHERE id=?", (r[0],)).fetchone() for r in before] == before
    db.end_session("ordinary-main", "compression")
    db.create_session("compressed-main", source="cli", parent_session_id="ordinary-main", system_prompt=legacy)
    db.append_message("compressed-main", "user", "Compressed original context")
    db.append_message("compressed-main", "assistant", "Preserved finding")
    agent = _agent(db, "compressed-main", parent_session_id="ordinary-main")
    try:
        agent.run_conversation("Continue the preserved work", conversation_history=db.get_messages_as_conversation("compressed-main"))
        assert seen[-1]["messages"][0]["content"].count(HEADER) == 1
        assert agent._cached_system_prompt == legacy
    finally:
        agent.close()


@pytest.mark.parametrize("managed", [False, True], ids=["ordinary-owner", "managed"])
def test_personality_reinstall_keeps_common_policy_once(runtime, monkeypatch, managed):
    from hermes_runtime.standalone.config import activate_home
    from hermes_runtime.sessions import HOME_SESSION_ID
    from tui_gateway import server

    home, db, seen = runtime
    if managed:
        activate_home(home)
    sid = HOME_SESSION_ID if managed else "ordinary-main"
    db.create_session(sid, source="cli")
    agent = _agent(db, sid)
    session = {"agent": agent, "history": [], "history_lock": threading.RLock(), "history_version": 0}
    monkeypatch.setitem(server._sessions, "delegation-policy", session)
    try:
        for personality in ("Respond briefly.", ""):
            reset, _ = server._apply_personality_to_session("delegation-policy", session, personality)
            assert not reset
            agent.run_conversation("Coordinate the existing tasks", conversation_history=session["history"])
            assert seen[-1]["messages"][0]["content"].count(HEADER) == 1
            assert seen[-1]["messages"][0]["content"].count(GOOGLE_ROUTE) == 1
    finally:
        agent.close()


def test_review_fork_can_inherit_suffix_without_becoming_main(runtime):
    _, db, seen = runtime
    db.create_session("ordinary-main", source="cli")
    parent = _agent(db, "ordinary-main")
    try:
        parent.run_conversation("Give the current status")
        suffix = parent.ephemeral_system_prompt
        fork = _agent(db, "review-fork", side_agent=True, ephemeral_system_prompt=suffix)
        try:
            fork._cached_system_prompt = parent._cached_system_prompt
            fork.run_conversation("Perform the focused assigned review")
            assert fork.ephemeral_system_prompt == suffix
            system = seen[-1]["messages"][0]["content"]
            assert system.count(HEADER) == 1
            assert system.count(GOOGLE_ROUTE) == 1
            assert "govern only the user-facing main coordinator" in system
            assert "inheriting this text does not require recursively handing off" in system
        finally:
            fork.close()
    finally:
        parent.close()
