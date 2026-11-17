"""Lifecycle against an enrolled managed workload with an inactive replica."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from threading import Barrier, Event
import time

import pytest

from tools.bot_desktop import lease
from tools.environments.base import EnvironmentConnectionError
from tools.environments.kubernetes_lifecycle import SandboxLifecycle

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    profile = tmp_path / ("env_" + "a" * 32)
    profile.mkdir()
    namespace = "agentsee-a-" + "a" * 32
    lifecycle = SandboxLifecycle(profile, "/usr/bin/kubectl", namespace, 300)
    state = {"replicas": 0, "version": 0, "pod": {}, "scales": []}

    def pod(identity):
        return {
            "metadata": {"namespace": namespace, "name": "sandbox-0", "uid": identity},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "sandbox", "containerID": "containerd://" + identity,
                                              "state": {"running": {"startedAt": "now"}}}]},
        }

    def scale(replicas, version):
        assert version == str(state["version"])
        state["scales"].append(replicas)
        state["version"] += 1
        state["replicas"] = replicas
        state["pod"] = pod("generation-" + str(state["version"])) if replicas else {}

    monkeypatch.setattr(lifecycle, "_scale", lambda: (state["replicas"], str(state["version"])))
    monkeypatch.setattr(lifecycle, "_set_scale", scale)
    monkeypatch.setattr(lifecycle, "_pod", lambda: state["pod"])
    return lifecycle, state, pod


def _age(path, seconds):
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


def test_two_requests_start_one_pod_and_post_idle_request_gets_new_generation(sandbox):
    lifecycle, state, _ = sandbox
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=2)
        with lifecycle.active() as (identity, _):
            return identity["uid"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(lambda _: run(), range(2))) == ["generation-1"] * 2
    assert state["scales"] == [1]
    _age(lifecycle.activity, 301)
    assert lifecycle.stop_if_idle()
    with lifecycle.active() as (identity, _):
        assert identity["uid"] == "generation-3"
    assert state["scales"] == [1, 0, 1]


def test_request_waits_for_old_ready_pod_to_disappear(sandbox, monkeypatch):
    lifecycle, state, pod = sandbox
    old = pod("old")
    pod_reads = iter((old, {}))

    def read_pod():
        current = next(pod_reads, None)
        if current == {}:
            # The StatefulSet controller can update status as the former Pod goes away.
            state["version"] += 1
        return state["pod"] if current is None else current

    monkeypatch.setattr(lifecycle, "_pod", read_pod)
    with pytest.raises(EnvironmentConnectionError, match="stopping"):
        with lifecycle.active(wake=False, expected_uid="old"):
            pytest.fail("should not admit a command to the old pod")
    assert state["scales"] == []
    with lifecycle.active() as (identity, _):
        assert identity["uid"] == "generation-2"
    assert state["scales"] == [1]


def test_status_never_wakes_and_active_exec_or_human_use_defers_stop(sandbox):
    lifecycle, state, _ = sandbox
    with pytest.raises(EnvironmentConnectionError, match="not ready"):
        lifecycle.identity()
    assert not lifecycle.stop_if_idle()
    assert state["scales"] == []
    admitted, finished = Event(), Event()

    def run():
        with lifecycle.active():
            admitted.set()
            assert finished.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=1) as workers:
        command = workers.submit(run)
        try:
            assert admitted.wait(timeout=2)
            _age(lifecycle.activity, 310)
            assert not lifecycle.stop_if_idle()
        finally:
            finished.set()
        command.result(timeout=3)
    assert not lifecycle.stop_if_idle()  # Completion starts the idle clock.
    _age(lifecycle.activity, 310)
    active = lease.acquire("viewer", profile_key=str(lifecycle.home))
    assert not lifecycle.stop_if_idle()
    lease.release("viewer", expected_epoch=active.epoch, profile_key=str(lifecycle.home))
    assert not lifecycle.stop_if_idle()  # Closing the viewer starts the idle clock.
    _age(Path(lifecycle.home) / "bot-desktop" / "lease.json", 310)
    assert lifecycle.stop_if_idle()
    assert state["scales"] == [1, 0]


@pytest.mark.parametrize("spec", ({"spec": None}, {"spec": []}, {}))
def test_scale_requires_a_valid_spec(tmp_path, monkeypatch, spec):
    from types import SimpleNamespace

    namespace = "agentsee-a-" + "a" * 32
    lifecycle = SandboxLifecycle(tmp_path / ("env_" + "a" * 32), "/usr/bin/kubectl", namespace, 300)
    response = json.dumps({"metadata": {"resourceVersion": "1"}, **spec})
    monkeypatch.setattr("tools.environments.kubernetes_lifecycle.run_capture",
                        lambda *args, **kwargs: SimpleNamespace(stdout=response))
    with pytest.raises(EnvironmentConnectionError, match="Cannot read the managed Kubernetes sandbox scale"):
        lifecycle._scale()


def test_cached_terminal_wakes_after_retirement_but_stream_probe_does_not(tmp_path, monkeypatch):
    from tools.environments import streams
    from tools.environments.kubernetes import KubernetesEnvironment

    profile = tmp_path / ("env_" + "a" * 32)
    profile.mkdir()
    namespace = "agentsee-a-" + "a" * 32
    monkeypatch.setenv("HERMES_HOME", str(profile))
    state = tmp_path / "replicas.json"
    state.write_text(json.dumps({"replicas": 0, "version": 0, "scales": []}))
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"path = pathlib.Path({str(state)!r})\n"
        f"namespace = {namespace!r}\n"
        "s = json.loads(path.read_text())\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'get':\n"
        "    assert args[2].endswith('/statefulsets/sandbox/scale')\n"
        "    spec = {'replicas': s['replicas']} if s['replicas'] else {}\n"
        "    print(json.dumps({'spec':spec,'metadata':{'resourceVersion':str(s['version'])}}))\n"
        "elif args[0] == 'replace':\n"
        "    data = json.load(sys.stdin)\n"
        "    assert args[2].endswith('/statefulsets/sandbox/scale')\n"
        "    assert data['metadata']['resourceVersion'] == str(s['version'])\n"
        "    s['replicas'] = data['spec']['replicas']\n"
        "    s['version'] += 1\n"
        "    s['scales'].append(s['replicas'])\n"
        "    path.write_text(json.dumps(s))\n"
        "elif args[2:5] == ['get', 'pod', 'sandbox-0']:\n"
        "    if s['replicas']:\n"
        "        print(json.dumps({'metadata': {'namespace':namespace,'name':'sandbox-0','uid':str(s['version'])},"
        "'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}],"
        "'containerStatuses':[{'name':'sandbox','containerID':str(s['version']),'state':{'running':{'startedAt':'now'}}}]}}))\n"
        "else:\n"
        "    assert s['replicas'] == 1\n"
        "    command = args[args.index('--') + 1:]\n"
        "    os.execvp(command[0], command)\n"
    )
    kubectl.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(KubernetesEnvironment, "get_temp_dir", lambda self: str(tmp_path))

    def make_env(probe_only=False):
        return KubernetesEnvironment(namespace, "sandbox-0", "sandbox", cwd=str(tmp_path),
                                      probe_only=probe_only, idle_seconds=300)

    with pytest.raises(EnvironmentConnectionError, match="not ready"):
        make_env(probe_only=True)
    assert json.loads(state.read_text())["scales"] == []
    env = make_env()
    assert env.execute("printf first")["output"].strip() == "first"
    old_uid = env.kubernetes_identity["uid"]
    _age(env.lifecycle.activity, 301)
    assert env.lifecycle.stop_if_idle()
    assert streams.probe_in(env, ["printf", "forbidden"]).returncode == 125
    assert json.loads(state.read_text())["scales"] == [1, 0]
    assert env.execute("printf next")["output"].strip() == "next"
    assert env.kubernetes_identity["uid"] != old_uid
    assert json.loads(state.read_text())["scales"] == [1, 0, 1]
    _age(env.lifecycle.activity, 301)
    assert env.lifecycle.stop_if_idle()
    result = streams.run_in(env, ["printf", "workspace request"])
    assert result.stdout == b"workspace request"
    assert json.loads(state.read_text())["scales"] == [1, 0, 1, 0, 1]
