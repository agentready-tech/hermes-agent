"""Model-facing persistent spawns must honor async capability without a history declaration."""
from contextvars import copy_context
import json
import threading

import pytest

from tests.tools.test_delegate_projects import _parent
from tests.tools.test_delegate_mail_live import _response


@pytest.mark.parametrize("finite", [False, True])
def test_persistent_model_spawn_uses_the_turns_async_policy(tmp_path, monkeypatch, finite):
    from gateway.session_context import set_session_vars, clear_session_vars
    from gateway.session_finite import finite_turn_scope
    from run_agent import AIAgent
    import tools.cronjob_tools
    import tools.delegate_tool
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\n")
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    def model(self, payload, *args, **kwargs):
        entered.set()
        assert release.wait(timeout=15)
        return _response("Finished the saved child task")

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    parent, db = _parent(tmp_path)
    tokens = set_session_vars(platform="telegram", session_key="root-route", session_id="main",
                              async_delivery=True, session_history_delivery=None)
    try:
        with finite_turn_scope(finite):
            context = copy_context()
        def invoke():
            try:
                results.append(json.loads(context.run(registry.dispatch, "delegate_task",
                    {"tasks": [{"goal": "Review the existing saved evidence"}]}, parent_agent=parent)))
            except BaseException as error:
                errors.append(error)
            finally:
                returned.set()
        worker = threading.Thread(target=invoke)
        worker.start()
        try:
            assert entered.wait(timeout=5), errors
            if finite:
                release.set()
            assert returned.wait(timeout=5), "A persistent main spawn waited for the child instead of returning its handle"
            assert not errors, errors
            statuses = [x.get("status") for x in results[0].get("results", [])]
            assert ("dispatched" in statuses) is not finite
        finally:
            release.set()
            worker.join(timeout=15)
            assert not worker.is_alive()
    finally:
        clear_session_vars(tokens)
        from tests.waiting import wait_until
        from tools.delegate_project_store import ProjectStore
        with ProjectStore(parent) as store:
            wait_until(lambda: all(p.get("status") != "running" for p in store.projects()),
                       timeout=15, what="saved child completion")
        parent.close()
        db.close()
