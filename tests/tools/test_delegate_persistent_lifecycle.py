"""Persistent defaults keep native completion hooks and bounded parallel work."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.tools.test_delegate_persistence_default import runtime as _runtime
from tests.waiting import wait_until

runtime = _runtime


@pytest.mark.parametrize("background", [False, True])
def test_default_completion_notifies_memory_and_hooks_once(runtime, monkeypatch, background):
    from hermes_cli import plugins
    from tools import delegate_projects
    from tools.delegate_project_store import ProjectStore
    from tools.registry import registry

    _, _, open_parent = runtime
    parent, _ = open_parent()
    memory = SimpleNamespace(prefetch_all=Mock(return_value=""), on_delegation=Mock(), on_session_end=Mock())
    parent._memory_manager = memory
    hooks = Mock(return_value=[])
    monkeypatch.setattr(plugins, "invoke_hook", hooks)
    monkeypatch.setattr(delegate_projects, "_origin", lambda: {"wake": background})
    first = json.loads(registry.dispatch("delegate_task", {"goal": "Analyse the incoming contract"}, parent_agent=parent))
    assert not first.get("error"), first
    child = first["projects"][0]

    def receipt():
        with ProjectStore(parent) as store:
            row = store.message(child["message_id"])
            return json.loads(row["result_json"]) if row["result_json"] else None

    wait_until(receipt, timeout=15, what="persistent completion receipt")
    result = receipt()
    assert result["status"] == "completed"
    assert not any(key.startswith("_child_") for key in result)
    memory.on_delegation.assert_called_once_with(task="Analyse the incoming contract", result="Analysis 1",
                                                child_session_id=child["session_id"])
    assert [call.args[0] for call in hooks.call_args_list].count("subagent_stop") == 1
    delegate_projects.recover_projects(parent)
    memory.on_delegation.assert_called_once()


def test_default_batch_starts_independent_children_together(runtime, monkeypatch):
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from tools.registry import registry

    _, _, open_parent = runtime
    parent, _ = open_parent()
    barrier = threading.Barrier(2)
    sessions = set()
    lock = threading.Lock()

    def model(self, payload, *args, **kwargs):
        with lock:
            sessions.add(self.session_id)
        barrier.wait(timeout=10)
        return ChatCompletion(id="parallel", object="chat.completion", created=1, model="gpt-4.1",
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Independent finding"},
                      "finish_reason": "stop"}])

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    result = json.loads(registry.dispatch("delegate_task", {"tasks": [
        {"goal": "Inspect contract terms"}, {"goal": "Inspect delivery terms"},
    ]}, parent_agent=parent))
    assert not result.get("error"), result
    assert [item["status"] for item in result["results"]] == ["completed", "completed"]
    assert len(sessions) == 2
    assert [item["message_id"] for item in result["results"]] == [item["message_id"] for item in result["projects"]]
