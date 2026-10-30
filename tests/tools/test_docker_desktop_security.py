"""Desktop containers must support Chromium's own sandbox without granting VPN privileges."""

import json
import subprocess
from pathlib import Path

import pytest

from tools.environments import docker


@pytest.fixture
def sandbox(monkeypatch):
    monkeypatch.setattr(docker, "find_docker", lambda: "docker")
    monkeypatch.setattr(docker, "_cgroup_limits_ok", True)
    monkeypatch.setattr(docker.DockerEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(docker.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "new-container\n" if cmd[1] == "run" else "", ""))

    def create(image="nousresearch/hermes-sandbox:desktop", **kwargs):
        return docker.DockerEnvironment(image=image, persist_across_processes=False, **kwargs)

    return create


def security_options(args):
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--security-opt"] + [
        arg.partition("=")[2] for arg in args if arg.startswith("--security-opt=")]


@pytest.mark.parametrize("image", [
    "nousresearch/hermes-sandbox:desktop",
    "docker.io/nousresearch/hermes-sandbox:desktop@sha256:1234",
])
def test_desktop_chromium_gets_a_filtered_namespace_policy_without_vpn_permissions(sandbox, image):
    env = sandbox(image=image)
    args = env._all_run_args
    options = security_options(args)
    profiles = [s.partition("=")[2] for s in options if s.startswith("seccomp=")]
    assert len(profiles) == 1 and profiles[0] != "unconfined"
    policy = json.loads(Path(profiles[0]).read_text())
    assert policy["defaultAction"] == "SCMP_ACT_ERRNO"
    allowed = {name for rule in policy["syscalls"]
               if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
               and not rule.get("includes") for name in rule["names"]}
    assert {"clone", "setns", "unshare", "chroot"} <= allowed
    assert not {"mount", "ptrace", "bpf", "keyctl"} & allowed
    assert "no-new-privileges" in options
    assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
    assert not {"--privileged", "SYS_ADMIN", "NET_ADMIN", "--device"} & set(args)
    assert not any("/dev/net/tun" in arg or "apparmor=unconfined" in arg for arg in args)
    assert env.chromium_sandbox_enabled is True


def test_plain_images_keep_the_default_docker_policy(sandbox):
    env = sandbox(image="python:3.13")
    assert not any(s.startswith("seccomp=") for s in security_options(env._all_run_args))
    assert env.chromium_sandbox_enabled is False


def test_unrelated_security_options_do_not_disable_the_bundled_profile(sandbox):
    env = sandbox(extra_args=["--security-opt", "apparmor=operator-profile", "--env", "seccomp=variable"])
    options = security_options(env._all_run_args)
    assert "apparmor=operator-profile" in options
    assert len([s for s in options if s.startswith("seccomp=")]) == 1


@pytest.mark.parametrize("override", [
    ["--security-opt", "seccomp=/operator/policy.json"],
    ["--security-opt=seccomp=unconfined"],
])
def test_operator_security_policy_and_vpn_args_are_preserved(sandbox, override):
    extra = [*override, "--cap-add=NET_ADMIN", "--device=/dev/net/tun:/dev/net/tun"]
    env = sandbox(extra_args=extra)
    assert len([s for s in security_options(env._all_run_args) if s.startswith("seccomp=")]) == 1
    assert env._all_run_args[-len(extra):] == extra
    assert env.chromium_sandbox_enabled is True


def test_security_changes_cannot_reuse_a_container_with_old_permissions(sandbox):
    ordinary = sandbox()
    vpn = sandbox(extra_args=["--cap-add=NET_ADMIN", "--device=/dev/net/tun:/dev/net/tun"])
    assert ordinary._labels["hermes-environment"] != vpn._labels["hermes-environment"]
    assert ordinary._labels["hermes-environment"] == sandbox()._labels["hermes-environment"]
