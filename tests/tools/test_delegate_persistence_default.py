"""Default delegates keep native history; only explicit ephemeral calls use the transient runner."""

import json
import sqlite3

import pytest

from tests.tools.test_delegate_projects import _parent


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from tools import delegate_projects

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "compression:\n  enabled: false\ndelegation:\n  worktree_isolation: false\n  max_spawn_depth: 2\n"
    )
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": False})
    seen, parents = [], []

    def model(self, payload, *args, **kwargs):
        seen.append((self.session_id, payload["messages"]))
        return ChatCompletion(id="persistence-default", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": f"Analysis {len(seen)}"},
                      "finish_reason": "stop"}],
            usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)

    def parent():
        value = _parent(tmp_path)
        parents.append(value)
        return value

    yield tmp_path, seen, parent
    for agent, db in parents:
        agent.close()
        db.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_default_child_reopens_and_recovers_accepted_input(runtime, legacy):
    from tools.delegate_project_store import ProjectStore
    from tools.delegate_projects import recover_projects
    from tools.registry import registry

    home, seen, open_parent = runtime
    parent, db = open_parent()
    task = {"goal": "Analyse the supplier offer", "context": "Delivery is Friday"}
    arguments = task if legacy else {"tasks": [task]}
    first = json.loads(parent._dispatch_delegate_task(arguments))
    assert not first.get("error"), first
    child = first["projects"][0]
    sid, identity = child["session_id"], child["subagent_id"]
    assert sid != parent.session_id
    with ProjectStore(parent) as store:
        project = store.get(identity)
        message_id = store.enqueue(project, "New mail: delivery is Monday", {"wake": False}, "mail-42")
        assert store.claim(project)["message_id"] == message_id
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("UPDATE delegated_project_messages SET owner_pid=-1,owner_started_at=NULL WHERE message_id=?", (message_id,))
    parent.close()
    db.close()

    parent, db = open_parent()
    recover_projects(parent)
    recover_projects(parent)
    assert len(seen) == 2 and [item[0] for item in seen] == [sid, sid]
    assert all(text in json.dumps(seen[-1][1]) for text in ("Friday", "Monday", "Analysis 1"))
    saved = json.loads(registry.dispatch("delegate_task", {"action": "read", "subagent_id": identity}, parent_agent=parent))
    assert saved["project"]["session_id"] == sid
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 1
        assert conn.execute("SELECT state FROM delegated_project_messages WHERE message_id=?", (message_id,)).fetchone()[0] == "completed"
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE platform_message_id=? AND role='user'", (message_id,)).fetchone()[0] == 1


def test_explicit_ephemeral_reaches_both_model_dispatch_paths(runtime):
    from tools.async_delegation import get_durable_delegation
    from tools.registry import registry
    from tests.waiting import wait_until

    home, seen, open_parent = runtime
    parent, _ = open_parent()
    args = {"tasks": [{"goal": "Independently verify this calculation"}], "ephemeral": True}
    for dispatch in (parent._dispatch_delegate_task,
                     lambda value: registry.dispatch("delegate_task", value, parent_agent=parent)):
        result = json.loads(dispatch(args))
        assert not result.get("error"), result
        if result.get("status") == "dispatched":
            identity = result["delegation_id"]
            wait_until(lambda: (get_durable_delegation(identity) or {}).get("state") == "completed",
                       timeout=15, what="explicit ephemeral completion")
            result = get_durable_delegation(identity)["result"]
        assert result["results"][0]["status"] == "completed"
    assert len(seen) == 2 and len({sid for sid, _ in seen}) == 2
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 0


def test_identical_default_tasks_keep_independent_saved_identities(runtime):
    from tools.registry import registry

    _, seen, open_parent = runtime
    parent, _ = open_parent()
    result = json.loads(registry.dispatch("delegate_task", {"tasks": [
        {"goal": "Independently inspect the supplier terms"},
        {"goal": "Independently inspect the supplier terms"},
    ]}, parent_agent=parent))
    assert not result.get("error"), result
    assert len(result["projects"]) == 2
    assert len({p["subagent_id"] for p in result["projects"]}) == 2
    assert len(seen) == 2


@pytest.mark.parametrize("options", [
    {"ephemeral": "false"}, {"ephemeral": None}, {"ephemeral": 1},
    {"ephemeral": True, "project": "existing-project"}, {"group": "join"},
])
def test_invalid_mode_cannot_silently_spawn_a_transient_child(runtime, options):
    from tools.registry import registry

    home, seen, open_parent = runtime
    parent, _ = open_parent()
    task = {"goal": "Inspect the incoming supplier requirements"}
    args = {"tasks": [task]}
    for key, value in options.items():
        (args if key == "ephemeral" else task)[key] = value
    result = json.loads(registry.dispatch("delegate_task", args, parent_agent=parent))
    assert result.get("error"), result
    assert seen == []
    with sqlite3.connect(home / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 0


def test_nested_default_child_returns_its_result_inside_the_owner_turn(runtime, monkeypatch):
    from tools import delegate_projects
    from tools.registry import registry

    _, seen, open_parent = runtime
    parent, _ = open_parent()
    parent._delegate_depth = 1
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": True, "session_key": "root-ui"})
    result = json.loads(registry.dispatch("delegate_task", {"tasks": [
        {"goal": "Verify a part of the orchestrator's task"},
    ]}, parent_agent=parent))
    assert not result.get("error"), result
    assert result["projects"][0]["status"] == "completed"
    assert result["projects"][0]["result"]["summary"] == "Analysis 1"
    assert len(seen) == 1
