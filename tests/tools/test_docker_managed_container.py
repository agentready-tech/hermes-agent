"""An explicitly provisioned sandbox is never replaced by native lifecycle code."""

import subprocess

import pytest

from tools.environments import docker


@pytest.fixture
def managed(monkeypatch):
    calls = []
    monkeypatch.setattr(docker, "_ensure_docker_available", lambda: None)
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1] not in {"run", "create", "rm", "stop"}
        return subprocess.CompletedProcess(argv, 0, "existing-id\trunning\tdesktop-image\n", "")
    monkeypatch.setattr(docker, "run_capture", run)
    return calls


def test_fixed_container_bypasses_creation_fingerprint_and_cleanup(managed):
    env = docker.DockerEnvironment("desktop-image", container_name="existing-sandbox")
    assert env._container_id == "existing-id"
    env.cleanup(force_remove=True)
    assert env._container_id is None
    assert len(managed) == 1 and managed[0][1] == "inspect"


def test_missing_fixed_container_refuses_replacement(managed, monkeypatch):
    monkeypatch.setattr(docker, "run_capture", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "missing"))
    with pytest.raises(RuntimeError, match="refusing to create a replacement"):
        docker.DockerEnvironment("desktop-image", container_name="missing")


def test_fixed_container_image_mismatch_is_not_silently_recreated(managed):
    with pytest.raises(RuntimeError, match="image differs"):
        docker.DockerEnvironment("another-image", container_name="existing-sandbox")


def test_stopped_fixed_container_is_started_without_recreation(managed, monkeypatch):
    def run(argv, **kwargs):
        managed.append(argv)
        assert argv[1] in {"inspect", "start"}
        return subprocess.CompletedProcess(argv, 0, "existing-id\texited\tdesktop-image\n" if argv[1] == "inspect" else "existing-id", "")
    monkeypatch.setattr(docker, "run_capture", run)
    env = docker.DockerEnvironment("desktop-image", container_name="existing-sandbox")
    assert env._container_id == "existing-id"
    assert [argv[1] for argv in managed] == ["inspect", "start"]


def test_recovery_reattaches_only_the_fixed_container(managed):
    env = docker.DockerEnvironment("desktop-image", container_name="existing-sandbox")
    assert env._recreate_container()
    assert env._container_id == "existing-id"
    assert all(argv[1] == "inspect" for argv in managed)


def test_fixed_container_is_resolved_from_its_profile_not_ambient_env(tmp_path, monkeypatch):
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import _get_env_config

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: docker\n  docker_container: chosen-sandbox\n")
    monkeypatch.setenv("TERMINAL_DOCKER_CONTAINER", "another-agents-sandbox")
    with install_and_reset_profile_terminal_scope(home):
        assert _get_env_config()["docker_container"] == "chosen-sandbox"
