"""The existing cron tool must schedule the managed main agent with a Docker terminal."""

import json
import sqlite3

import pytest


@pytest.fixture
def main_home(monkeypatch):
    from hermes_constants import get_hermes_home
    from hermes_runtime.adapters import hermes
    from hermes_runtime.standalone.config import activate_home
    from hermes_state import SessionDB

    home = get_hermes_home()
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("terminal:\n  backend: docker\ncompression:\n  enabled: false\n")
    monkeypatch.setattr(hermes, "_home", None)
    activate_home(home)
    with SessionDB() as db:
        db.append_message("agentready_home", "user", "Keep working on the existing project")
        db.append_message("agentready_home", "assistant", "The existing project finding")
        yield home, db


def _tool(action, session_id="agentready_home", **arguments):
    import tools.cronjob_tools  # register the real model-facing handler
    from tools.registry import registry

    return json.loads(registry.dispatch("cronjob_manage", {"action": action, **arguments}, session_id=session_id))


def _fire(job, monkeypatch):
    from cron import scheduler

    # Keep the real execute/save/bookkeeping path; a process handoff is exercised on the VM.
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda *_: None)
    assert scheduler.run_one_job(job)


def test_existing_tool_creates_and_manages_main_schedule_with_docker_terminal(main_home, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import CRON_ORIGIN

    _, db = main_home
    before = db.get_messages("agentready_home", include_inactive=True)
    with sqlite3.connect(main_home[0] / "state.db") as conn:
        sessions_before = conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()
    created = _tool("create", prompt="Review the existing project", schedule="every 1h", name="Main follow-up")
    assert created["success"] is True
    identity = created["job_id"]
    assert get_job(identity)["origin"]["platform"] == CRON_ORIGIN
    assert get_job(identity)["origin"]["chat_id"] == "main"
    assert created["job"]["execution_scope"] == "main"
    assert get_job(identity)["deliver"] == "local"
    assert _tool("pause", job_id=identity)["success"] is True
    assert _tool("resume", job_id=identity)["success"] is True
    updated = _tool("update", job_id=identity, prompt="Continue the existing project with new evidence")
    assert updated["job"]["execution_scope"] == "main"
    assert _tool("list")["jobs"][0]["execution_scope"] == "main"

    _fire(get_job(identity), monkeypatch)
    pending = HomeInbox().pending()
    assert len(pending) == 1
    assert "Continue the existing project with new evidence" in pending[0][1]
    assert get_job(identity)["last_status"] == "ok"
    # Admission is not a replacement conversation or a claim that the model completed the task.
    assert db.get_messages("agentready_home", include_inactive=True) == before
    with sqlite3.connect(main_home[0] / "state.db") as conn:
        assert conn.execute("SELECT id FROM sessions ORDER BY id").fetchall() == sessions_before
    assert _tool("remove", job_id=identity)["success"] is True


def test_cli_schedule_acceptance_survives_full_scheduler_bookkeeping(main_home, monkeypatch):
    from cron.executions import get_execution
    from cron.jobs import get_job
    from hermes_runtime.adapters.inbox import HomeInbox
    from hermes_runtime.standalone.events import admit_cron, schedule

    job = schedule("Continue this conversation later", "every 1h", "CLI main follow-up")
    _fire(job, monkeypatch)
    stored = get_job(job["id"])
    assert stored["last_status"] == "ok", stored.get("last_error")
    assert not stored.get("last_error")
    assert get_execution(job["execution_id"])["status"] == "completed"
    assert len(HomeInbox().pending()) == 1
    receipt = admit_cron(job, execution_id=job["execution_id"])
    assert "Accepted into main-session inbox" in receipt[2]
    assert len(HomeInbox().pending()) == 1


@pytest.mark.parametrize("kind", ["main", "compressed", "ordinary", "child"])
def test_managed_tool_always_targets_existing_main_conversation(main_home, kind):
    from agent.delegation_context import delegated_child_context
    from contextlib import nullcontext
    from hermes_runtime.adapters.state import ensure_home_head

    _, db = main_home
    session_id = "agentready_home"
    if kind == "compressed":
        db.end_session(session_id, "compression")
        db.create_session("compressed-main", source="telegram", parent_session_id=session_id)
        ensure_home_head()
        session_id = "compressed-main"
    if kind == "ordinary":
        session_id = "unrelated-chat"
    scope = delegated_child_context(session_id) if kind == "child" else nullcontext()
    with scope:
        result = _tool("create", session_id=session_id, prompt="Scheduled follow-up", schedule="every 2h")
    assert result["success"] is True
    assert result["job"]["execution_scope"] == "main"


def test_main_schedule_rejects_separate_session_options_without_saving(main_home):
    from cron.jobs import list_jobs

    result = _tool("create", prompt="Check later", schedule="every 1h", enabled_toolsets=[])
    assert result["success"] is False
    assert "prompt" in result["error"]
    assert list_jobs() == []


def test_managed_scheduler_refuses_legacy_isolated_job_without_creating_session(main_home):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    _, db = main_home
    before = db.get_messages("agentready_home", include_inactive=True)
    job = create_job(prompt="Legacy separate-session job", schedule="every 1h")
    with pytest.raises(ValueError, match="Separate-session cron runs are disabled"):
        run_job(job)
    assert db.get_messages("agentready_home", include_inactive=True) == before


def test_unmanaged_profiles_keep_native_cron_semantics(main_home, monkeypatch):
    from cron.jobs import get_job
    from hermes_runtime.adapters import hermes

    monkeypatch.setattr(hermes, "_home", None)
    result = _tool("create", prompt="Native profile job", schedule="every 1h")
    assert result["success"] is True
    assert not get_job(result["job_id"]).get("origin")
    assert "execution_scope" not in result["job"]


def test_detached_managed_worker_refuses_isolated_run_before_adapter_activation(main_home, monkeypatch):
    from cron.jobs import create_job
    from cron.scheduler import run_job
    from hermes_runtime.adapters import hermes

    home, _ = main_home
    (home / "config.yaml").write_text("autonomous:\n  enabled: true\nterminal:\n  backend: docker\n")
    monkeypatch.setattr(hermes, "_home", None)
    job = create_job(prompt="Legacy separate-session job", schedule="every 1h")
    with pytest.raises(ValueError, match="Separate-session cron runs are disabled"):
        run_job(job)


def test_model_schema_describes_main_scheduling_under_docker(main_home, monkeypatch):
    import tools.cronjob_tools  # register the existing tool
    from tools.registry import registry

    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    definition = registry.get_definitions({"cronjob_manage"})[0]["function"]
    assert "existing persistent main conversation" in definition["description"]
    assert "Docker" in definition["description"]
    assert "always runs in a fresh session" not in definition["parameters"]["properties"]["deliver"]["description"]
