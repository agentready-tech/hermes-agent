"""Persistent recovery keeps its owning profile across real parallel worker threads."""

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("project_count", [1, 3])
def test_recovery_workers_keep_bound_profile_instead_of_launcher(tmp_path, monkeypatch, project_count):
    from agent.secret_scope import build_profile_secret_scope, get_secret, reset_secret_scope, set_secret_scope
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    from tools import delegate_projects, delegate_tool, terminal_tool
    from tools.delegate_project_store import ProjectStore
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope, terminal_env

    launch, served = tmp_path / "launcher", tmp_path / "served"
    launch.mkdir()
    served.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("TERMINAL_KUBERNETES_NAMESPACE", "launcher-sandbox")
    monkeypatch.setenv("PROFILE_CONTEXT_FIXTURE", "launcher-value")
    (served / ".env").write_text("PROFILE_CONTEXT_FIXTURE=served-value\n")
    (served / "config.yaml").write_text(
        "terminal:\n  env: kubernetes\n  kubernetes_namespace: served-sandbox\n")
    db = SessionDB(db_path=served / "state.db")
    db.create_session("main", source="cli")
    parent = SimpleNamespace(_session_db=db, session_id="main")
    barrier = threading.Barrier(project_count)
    callback = object()
    old_callback = terminal_tool._get_approval_callback()
    home_token = set_hermes_home_override(served)
    secret_token = set_secret_scope(build_profile_secret_scope(served), profile_home=str(served))
    terminal_token = install_profile_terminal_scope(served)
    terminal_tool.set_approval_callback(callback)

    def inspect_worker(_parent, store, project):
        # _recover_project_scoped has already constructed the real ProjectStore,
        # whose guard rejects a worker that silently fell back to the launch home.
        observed = {
            "project": project["project_key"], "profile": get_hermes_home(), "database": store.db_path,
            "namespace": terminal_env("TERMINAL_KUBERNETES_NAMESPACE"),
            "profileValue": get_secret("PROFILE_CONTEXT_FIXTURE"),
            "approvalCallbackPreserved": terminal_tool._get_approval_callback() is callback,
        }
        # All workers must overlap: reusing ONE copied Context across submits
        # raises "Context is already entered" rather than passing by sequential reuse.
        barrier.wait(timeout=5)
        return [observed]

    monkeypatch.setattr(delegate_tool, "is_spawn_paused", lambda: False)
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: project_count)
    monkeypatch.setattr(delegate_projects, "_recover_project", inspect_worker)
    try:
        with ProjectStore(parent) as store:
            for index in range(project_count):
                store.create(f"project-{index}", "Inspect saved project", None)
        observed = delegate_projects.recover_projects(parent)
        assert {row["project"] for row in observed} == {f"project-{index}" for index in range(project_count)}
        assert all(row["profile"] == served and row["database"] == served / "state.db" for row in observed)
        assert all(row["namespace"] == "served-sandbox" and row["profileValue"] == "served-value" for row in observed)
        assert all(row["approvalCallbackPreserved"] for row in observed), "Recovery dropped the parent prompt callback"
        assert get_hermes_home() == served
    finally:
        terminal_tool.set_approval_callback(old_callback)
        reset_terminal_scope(terminal_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
        db.close()
