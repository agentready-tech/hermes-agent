"""Matching image/data rollback, including SQLite WAL, Git and checkpoints."""

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_runtime.standalone import snapshot
from hermes_runtime.standalone.deployment import Deployment


@pytest.fixture
def data(tmp_path):
    home = tmp_path / "data"
    home.mkdir()
    (home / "config.yaml").write_text("memory: {provider: holographic}\n")
    (home / ".env").write_text("SECRET=keep\n")
    (home / "workspace" / ".git").mkdir(parents=True)
    (home / "workspace" / ".git" / "config").write_text("git metadata")
    (home / "workspace" / ".agentready" / "checkpoints").mkdir(parents=True)
    (home / "workspace" / ".agentready" / "checkpoints" / "history.json").write_text("full history")
    with sqlite3.connect(home / "memory_store.db") as db:
        db.execute("CREATE TABLE facts(content TEXT)")
        db.execute("INSERT INTO facts VALUES ('remember')")
    (home / "version.txt").write_text("old state")
    return home


class DockerFixture:
    def __init__(self, home):
        self.home, self.containers, self.actions = home, {}, []
        self.fail_start = False

    def image(self, image):
        return "sha256:" + image

    def preflight(self, image):
        self.actions.append(("preflight", image))

    def start(self, value):
        if self.fail_start:
            self.fail_start = False
            raise RuntimeError("create interrupted")
        self.containers[value["name"]] = value
        self.actions.append(("start", value["image"]))
        if value["image"] == "sha256:bad":
            (self.home / "version.txt").write_text("bad migrated state")

    def stop(self, value):
        self.actions.append(("stop", value["image"]))

    def remove(self, value):
        self.containers.pop(value["name"])

    def healthy(self, value):
        return value["image"] != "sha256:bad"

    def call(self, *args):
        if args[0] == "ps":
            return "\n".join(self.containers)
        return ""


def operator(tmp_path, data):
    docker = DockerFixture(data)
    result = Deployment(tmp_path / "deployment", docker)
    result.up(data, "old", 9119)
    return result, docker


@pytest.mark.parametrize("failure", ["health", "create"])
def test_failed_update_restores_complete_matching_state(tmp_path, data, failure):
    deployment, docker = operator(tmp_path, data)
    docker.fail_start = failure == "create"
    with pytest.raises(RuntimeError):
        deployment.upgrade("bad")
    assert deployment.read()["image"] == "sha256:old"
    assert (data / "version.txt").read_text() == "old state"
    assert (data / ".env").read_text() == "SECRET=keep\n"
    assert (data / "workspace" / ".git" / "config").read_text() == "git metadata"
    assert (data / "workspace" / ".agentready" / "checkpoints" / "history.json").read_text() == "full history"
    with sqlite3.connect(data / "memory_store.db") as db:
        assert db.execute("SELECT content FROM facts").fetchone()[0] == "remember"
    assert not (deployment.directory / "transition.json").exists()


def test_success_update_then_explicit_rollback(tmp_path, data):
    deployment, _ = operator(tmp_path, data)
    result = deployment.upgrade("new")
    assert deployment.read()["image"] == "sha256:new"
    (data / "version.txt").write_text("new state")
    deployment.rollback(result["rollback_snapshot"])
    assert deployment.read()["image"] == "sha256:old"
    assert (data / "version.txt").read_text() == "old state"
    assert list(data.parent.glob("data.before-restore-*"))


def test_snapshot_keeps_wal_and_additional_workspace(tmp_path, data):
    external = tmp_path / "project"
    external.mkdir()
    (external / "draft.txt").write_text("uncommitted")
    deployment = {"image": "sha256:old", "mounts": [
        {"source": str(data), "target": "/opt/data"}, {"source": str(external), "target": "/projects/order"}]}
    writer = sqlite3.connect(data / "state.db")
    writer.execute("PRAGMA journal_mode=wal")
    writer.execute("CREATE TABLE delegated_projects(workspace_json TEXT)")
    writer.execute("INSERT INTO delegated_projects VALUES (?)", (json.dumps({"cwd": "/projects/order"}),))
    writer.commit()
    backup = tmp_path / "snapshot"
    try:
        snapshot.create(backup, deployment)
    finally:
        writer.close()
    (external / "draft.txt").write_text("changed")
    snapshot.restore(backup)
    assert (external / "draft.txt").read_text() == "uncommitted"
    with sqlite3.connect(data / "state.db") as db:
        assert db.execute("SELECT COUNT(*) FROM delegated_projects").fetchone()[0] == 1


def test_missing_external_workspace_refuses_and_restarts_old_image(tmp_path, data):
    deployment, docker = operator(tmp_path, data)
    with sqlite3.connect(data / "state.db") as db:
        db.execute("CREATE TABLE delegated_projects(workspace_json TEXT)")
        db.execute("INSERT INTO delegated_projects VALUES (?)", (json.dumps({"cwd": "/unmounted/order"}),))
    with pytest.raises(ValueError, match="Uncovered persistent"):
        deployment.upgrade("new")
    assert ("stop", "sha256:old") in docker.actions
    assert deployment.read()["image"] == "sha256:old"
    assert not (deployment.directory / "transition.json").exists()


def test_damaged_snapshot_cannot_touch_current_data(tmp_path, data):
    deployment, docker = operator(tmp_path, data)
    result = deployment.upgrade("new")
    Path(result["rollback_snapshot"], "state.tar.gz").write_bytes(b"damaged")
    docker.actions.clear()
    with pytest.raises(ValueError, match="damaged"):
        deployment.rollback(result["rollback_snapshot"])
    assert (data / "version.txt").read_text() == "old state"
    assert docker.actions == []


@pytest.mark.parametrize("phase", ["stopping", "removed", "directory-swap"])
def test_recover_interrupted_controller(tmp_path, data, phase):
    deployment, docker = operator(tmp_path, data)
    old = deployment.read()
    deployment.preparing(old)
    if phase != "stopping":
        saved = deployment.backup_stopped(old)
        candidate = {**old, "image": "sha256:bad"}
        deployment.write("transition.json", {"snapshot": str(saved), "candidate": candidate})
        docker.remove(old)
        if phase == "directory-swap":
            data.rename(data.with_name("interrupted-old-directory"))
    recovered = deployment.recover()
    assert recovered["image"] == "sha256:old"
    assert (data / "version.txt").read_text() == "old state"
    assert not (deployment.directory / "transition.json").exists()
