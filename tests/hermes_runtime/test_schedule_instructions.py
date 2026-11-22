"""Mandatory main scheduling instructions reach native requests without rewriting saved prompts."""

import json
import sqlite3
from contextlib import nullcontext

import pytest

from tests.tools.test_delegate_mail_live import _response


def _agent(db, sid, *, load_soul_identity=False):
    from run_agent import AIAgent
    return AIAgent(model="gpt-4.1", provider="custom", base_url="http://127.0.0.1:9/v1", api_key="test-only",
        api_mode="chat_completions", session_id=sid, session_db=db, enabled_toolsets=["terminal"],
        skip_context_files=True, load_soul_identity=load_soul_identity, skip_memory=True, quiet_mode=True,
        ephemeral_system_prompt="Existing host instructions")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from run_agent import AIAgent
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(hermes, "_home", None)
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n")
    activate_home(tmp_path)
    seen = []

    def model(self, payload, *args, **kwargs):
        seen.append(payload)
        return _response("Schedule policy understood")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    with SessionDB(db_path=tmp_path / "state.db") as db:
        yield tmp_path, db, seen


@pytest.mark.parametrize("kind", ["main", "child", "isolated", "ordinary-profile"])
def test_scheduling_policy_is_mandatory_only_for_the_main_owner(runtime, monkeypatch, kind):
    from agent.delegation_context import delegated_child_context
    from hermes_runtime.adapters import hermes
    from hermes_runtime.sessions import HOME_SESSION_ID

    _, db, seen = runtime
    sid = "separate-session" if kind == "isolated" else HOME_SESSION_ID
    if kind == "ordinary-profile":
        monkeypatch.setattr(hermes, "_home", None)
    scope = delegated_child_context(sid) if kind == "child" else nullcontext()
    with scope:
        agent = _agent(db, sid)
    try:
        agent.run_conversation("Check how the next reminder should run")
        text = seen[0]["messages"][0]["content"]
        from hermes_runtime.standalone.instructions import MAIN_SCHEDULE_INSTRUCTIONS
        assert (MAIN_SCHEDULE_INSTRUCTIONS in text) == (kind == "main")
        assert "Existing host instructions" in text
        for rule in (
            "stable project_id and wait_id",
            "Retry the same arguments for an unresolved receipt",
            "future next_run_at",
            "cron does not check the source",
            "The wake is not evidence of a reply",
        ):
            assert (rule in text) == (kind == "main")
        if kind == "main":
            assert text.count(MAIN_SCHEDULE_INSTRUCTIONS) == 1
            assert not {"skill_view", "skills_list"} & agent.valid_tool_names
    finally:
        agent.close()


def test_resume_and_compression_keep_history_and_one_stable_instruction_overlay(runtime):
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.instructions import MAIN_SCHEDULE_INSTRUCTIONS

    home, db, seen = runtime
    legacy = "Saved main instructions from before the scheduling policy."
    identity = "Fixture identity from the current SOUL.md."
    (home / "SOUL.md").write_text(identity + "\n", encoding="utf-8")
    db.update_system_prompt(HOME_SESSION_ID, legacy)
    db.append_message(HOME_SESSION_ID, "user", "Original task")
    db.append_message(HOME_SESSION_ID, "assistant", "Original finding")
    with sqlite3.connect(home / "state.db") as conn:
        before = conn.execute("SELECT id,role,content,api_content,platform_message_id FROM messages ORDER BY id").fetchall()
    for turn in range(2):
        agent = _agent(db, HOME_SESSION_ID, load_soul_identity=True)
        try:
            agent.run_conversation(f"Continue the saved work {turn}", conversation_history=db.get_messages_as_conversation(HOME_SESSION_ID))
            assert agent.ephemeral_system_prompt.count(MAIN_SCHEDULE_INSTRUCTIONS) == 1
        finally:
            agent.close()
    current_prompt = db.get_session(HOME_SESSION_ID)["system_prompt"]
    assert identity in current_prompt and legacy not in current_prompt
    first, resumed = [request["messages"][0]["content"] for request in seen]
    assert first == resumed and first.startswith(current_prompt)
    with sqlite3.connect(home / "state.db") as conn:
        after = [conn.execute("SELECT id,role,content,api_content,platform_message_id FROM messages WHERE id=?", (row[0],)).fetchone() for row in before]
        assert after == before
    db.reopen_session(HOME_SESSION_ID)
    db.end_session(HOME_SESSION_ID, "compression")
    assert db.get_session(HOME_SESSION_ID)["end_reason"] == "compression"
    db.create_session("main-compressed", source="cli", parent_session_id=HOME_SESSION_ID, system_prompt=current_prompt)
    db.append_message("main-compressed", "user", "Compressed context")
    db.append_message("main-compressed", "assistant", "Preserved finding")
    agent = _agent(db, "main-compressed", load_soul_identity=True)
    try:
        agent.run_conversation("Continue after compression", conversation_history=db.get_messages_as_conversation("main-compressed"))
    finally:
        agent.close()
    assert seen[-1]["messages"][0]["content"] == first
    assert any(message.get("content") == "Compressed context" for message in seen[-1]["messages"])


def test_personality_changes_keep_the_main_scheduling_policy(runtime, monkeypatch):
    import threading
    from tui_gateway import server
    from hermes_runtime.sessions import HOME_SESSION_ID
    from hermes_runtime.standalone.instructions import MAIN_SCHEDULE_INSTRUCTIONS

    _, db, seen = runtime
    agent = _agent(db, HOME_SESSION_ID)
    session = {"agent": agent, "history": [], "history_lock": threading.RLock(), "history_version": 0}
    monkeypatch.setitem(server._sessions, "schedule-policy", session)
    try:
        for personality in ("Respond briefly.", ""):
            reset, _ = server._apply_personality_to_session("schedule-policy", session, personality)
            assert not reset
            agent.run_conversation("Check reminders", conversation_history=session["history"])
            assert seen[-1]["messages"][0]["content"].count(MAIN_SCHEDULE_INSTRUCTIONS) == 1
    finally:
        agent.close()


def test_main_browser_and_workspace_policy_with_an_existing_prompt(runtime):
    from hermes_runtime.sessions import HOME_SESSION_ID

    home, db, seen = runtime
    agent = _agent(db, HOME_SESSION_ID)
    browser_heading = "## Browser working tabs"
    workspace_heading = "## Google Workspace и почта"
    environment = "env_" + "a" * 32
    try:
        agent.run_conversation("First main turn")
        saved = db.get_session(HOME_SESSION_ID)["system_prompt"]
        assert browser_heading not in saved
        assert workspace_heading not in saved
        first = seen[0]["messages"][0]["content"]
        assert first.count(browser_heading) == 1
        assert workspace_heading not in first

        for native, workspace_id, expected in (
            (True, environment, 1),
            (True, "env_" + "b" * 32, 0),
            (False, environment, 0),
        ):
            (home / "config.yaml").write_text(json.dumps({"autonomous": {
                "workspace": {"native": native, "environment_id": workspace_id},
                "panel": {"environment_id": environment},
            }}), encoding="utf-8")
            agent.run_conversation("Continue the same main conversation")
            system = seen[-1]["messages"][0]["content"]
            assert system.startswith(saved)
            assert system.count(browser_heading) == 1
            assert system.count(workspace_heading) == expected
            assert db.get_session(HOME_SESSION_ID)["system_prompt"] == saved
    finally:
        agent.close()
