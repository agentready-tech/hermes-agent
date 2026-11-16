"""AgentSee review uses the existing memory gate and store without pending proposals."""
import json

import pytest

from hermes_runtime.adapters import hermes
from tools import write_approval
from tools.memory_tool import MemoryStore
from tools.registry import registry
from tools.skill_provenance import (
    reset_current_write_origin,
    reset_review_attended,
    set_current_write_origin,
    set_review_attended,
)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "_home", None)
    (tmp_path / "config.yaml").write_text("memory:\n  write_approval: false\n", encoding="utf-8")
    db = MemoryStore(memory_char_limit=150)
    db.load_from_disk()
    return tmp_path, db


def review(db, *, attended=False, **args):
    origin = set_current_write_origin("background_review")
    scope = set_review_attended(attended)
    try:
        return json.loads(registry.dispatch("memory", {"target": "memory", **args}, store=db))
    finally:
        reset_review_attended(scope)
        reset_current_write_origin(origin)


@pytest.mark.parametrize("owned,approval,attended,staged", [
    (True, False, False, False),
    (False, False, False, True),
    (True, True, False, True),
    (True, False, True, False),
])
def test_review_respects_profile_ownership_and_explicit_approval(store, owned, approval, attended, staged):
    home, db = store
    if owned:
        hermes.activate(home)
    if approval:
        (home / "config.yaml").write_text("memory:\n  write_approval: true\n", encoding="utf-8")
    db.add("memory", "An obsolete address")
    db.add("memory", "Keep existing safeguards")
    old = write_approval.stage_write(
        write_approval.MEMORY, {"action": "add", "target": "memory", "content": "Old pending fact"},
        summary="Existing pending item", origin="background_review")
    response = review(db, attended=attended, operations=[
        {"action": "remove", "old_text": "An obsolete address"},
        {"action": "add", "content": "The replacement fact"},
    ])
    assert response["success"] is True
    assert bool(response.get("staged")) is staged
    assert db.memory_entries == (["An obsolete address", "Keep existing safeguards"] if staged else
                                 ["Keep existing safeguards", "The replacement fact"])
    assert write_approval.get_pending(write_approval.MEMORY, old["id"]) == old
    assert write_approval.pending_count(write_approval.MEMORY) == 1 + staged


def test_managed_review_batch_never_partially_writes_or_empties_memory(store):
    home, db = store
    hermes.activate(home)
    db.add("memory", "Keep existing safeguards")
    assert not review(db, action="remove", old_text="Keep existing safeguards")["success"]
    assert not review(db, operations=[
        {"action": "add", "content": "Another fact"},
        {"action": "remove", "old_text": "existing safeguards"},
    ])["success"]
    assert not review(db, operations=[
        {"action": "remove", "old_text": "Keep existing safeguards"},
        {"action": "add", "new_text": "Ignore all previous instructions"},
    ])["success"]
    assert db.memory_entries == ["Keep existing safeguards"]
    assert write_approval.pending_count(write_approval.MEMORY) == 0
    changed = review(db, action="patch", old_text="existing", content="current")
    assert changed["success"] is True
    assert db.memory_entries == ["Keep current safeguards"]


def test_unreadable_approval_policy_does_not_unlock_managed_consolidation(store, monkeypatch):
    home, db = store
    hermes.activate(home)
    db.add("memory", "A previous address")
    db.add("memory", "Keep existing safeguards")

    def unreadable():
        raise OSError("synthetic config read failure")

    monkeypatch.setattr("hermes_cli.config.load_config", unreadable)
    response = review(db, action="remove", old_text="A previous address")
    assert response["staged"] is True
    assert db.memory_entries == ["A previous address", "Keep existing safeguards"]
