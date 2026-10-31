"""Terminal and stream contracts against a real subprocess standing in for kubectl's API boundary."""

import json
import os
import sys

import pytest

from tools.environments.base import EnvironmentConnectionError
from tools.environments.kubernetes import KubernetesEnvironment, marker_alive, validate_target
from tools.terminal_tool_backends import _create_environment


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    workspace, temporary, home, binaries = (tmp_path / p for p in ("workspace", "tmp", "home", "bin"))
    for p in (workspace, temporary, home, binaries):
        p.mkdir()
    metadata = tmp_path / "pod.json"
    value = {"metadata": {"namespace": "client-one", "name": "sandbox-0", "uid": "pod-a"},
             "status": {"phase": "Running", "containerStatuses": [
                 {"name": "sandbox", "containerID": "containerd://one", "state": {"running": {"startedAt": "now"}}}]}}
    metadata.write_text(json.dumps(value))
    audit = tmp_path / "api-calls.jsonl"
    client = binaries / "kubectl"
    client.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(audit)!r}, 'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n"
        f"value=json.load(open({str(metadata)!r}))\n"
        "if value.get('denied'): print('credential-must-not-be-returned',file=sys.stderr);sys.exit(1)\n"
        "if sys.argv[3]=='get': print(json.dumps(value));sys.exit(0)\n"
        "assert sys.argv[1:3]==['--namespace','client-one']\n"
        "assert sys.argv[3]=='exec'\n"
        "assert sys.argv[5:8]==['sandbox-0','--container','sandbox']\n"
        "command=sys.argv[sys.argv.index('--')+1:]\n"
        f"os.environ['HOME']={str(home)!r}\n"
        "os.execvp(command[0],command)\n"
    )
    client.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(KubernetesEnvironment, "get_temp_dir", lambda self: str(temporary))
    environment = _create_environment("kubernetes", "", str(workspace), 10, container_config={
        "kubernetes_namespace": "client-one", "kubernetes_pod": "sandbox-0", "kubernetes_container": "sandbox"})
    yield environment, metadata, audit, workspace
    environment.cleanup()


@pytest.mark.platforms("posix")
def test_terminal_state_stdin_and_exit_code(sandbox):
    env, _, _, _ = sandbox
    first = env.execute("mkdir -p child; cd child; export SAVED_VALUE=retained")
    assert first["returncode"] == 0
    result = env.execute("printf '%s|' \"$SAVED_VALUE\"; pwd; cat; exit 7", stdin_data="секрет\nlast line")
    assert result["returncode"] == 7
    assert "retained|" in result["output"]
    assert "/child" in result["output"]
    assert "секрет\nlast line" in result["output"]


@pytest.mark.platforms("posix")
def test_forwarded_values_and_command_stay_out_of_api_arguments(sandbox, monkeypatch):
    env, _, audit, _ = sandbox
    monkeypatch.setattr("tools.environments.kubernetes.resolve_passthrough_env",
                        lambda: ({"TEST_SECRET": "private-forwarded-value"}, set()))
    result = env.execute("printf 'command-reached-sandbox:%s' \"$TEST_SECRET\"")
    assert "command-reached-sandbox:private-forwarded-value" in result["output"]
    calls = audit.read_text()
    assert "private-forwarded-value" not in calls
    assert "command-reached-sandbox" not in calls


@pytest.mark.platforms("posix")
def test_binary_stream_file_fetch_and_cleanup_preserve_pod(sandbox, tmp_path):
    from tools.environments import streams

    env, _, audit, workspace = sandbox
    payload = bytes(range(256)) * 17
    process = streams.open_stream(env, ["python3", "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"])
    stdout, _ = process.communicate(payload, timeout=5)
    assert process.returncode == 0 and stdout == payload
    (workspace / "binary").write_bytes(payload)
    target = tmp_path / "received"
    env.fetch_file(str(workspace / "binary"), target, max_bytes=len(payload))
    assert target.read_bytes() == payload
    env.cleanup()
    assert all(call[2] in {"get", "exec"} for call in map(json.loads, audit.read_text().splitlines()))


@pytest.mark.platforms("posix")
def test_replacement_resets_shell_and_invalidates_screen_identity(sandbox):
    env, metadata, _, _ = sandbox
    original = dict(env.kubernetes_identity)
    assert marker_alive(original)
    env.execute("export BEFORE_REPLACEMENT=yes")
    value = json.loads(metadata.read_text())
    value["metadata"]["uid"] = "pod-b"
    value["status"]["containerStatuses"][0]["containerID"] = "containerd://two"
    metadata.write_text(json.dumps(value))
    assert not marker_alive(original)
    result = env.execute("printf 'after=%s' \"${BEFORE_REPLACEMENT:-absent}\"")
    assert result.get("environment_recreated") is True
    assert "after=absent" in result["output"]


@pytest.mark.platforms("posix")
def test_api_failure_does_not_leak_credentials_or_fall_back(sandbox):
    env, metadata, _, _ = sandbox
    metadata.write_text(json.dumps({"denied": True}))
    with pytest.raises(EnvironmentConnectionError) as error:
        env.execute("touch should-not-run")
    assert "credential-must-not-be-returned" not in str(error.value)


@pytest.mark.parametrize("namespace,pod,container", [
    ("", "sandbox-0", "sandbox"), ("client", "--all", "sandbox"), ("client", "sandbox-0", "../hermes"),
    ("client", "a..b", "sandbox"),
])
def test_target_must_be_explicit_and_cannot_inject_flags(namespace, pod, container):
    with pytest.raises(ValueError):
        validate_target(namespace, pod, container)
