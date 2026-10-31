"""Opt-in real API/RBAC proof in a DISPOSABLE cluster (never point at production).

HERMES_TEST_KUBECONFIG selects the disposable admin context. The fixture creates
one isolated namespace and gives the backend only its sandbox-scoped account.
Preload python:3.11-slim in the cluster; no real Hermes credentials are used.
Optionally preload the client image built with docker/Dockerfile.kubernetes
and select it through HERMES_TEST_IMAGE for the in-cluster read-only-root test.
"""

import json
import os
import shlex
import socket
import subprocess
import uuid

import pytest

from tools.environments.kubernetes import KubernetesEnvironment
from tools.environments import streams

_CONFIG = os.getenv("HERMES_TEST_KUBECONFIG", "")
_CLIENT_IMAGE = os.getenv("HERMES_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(not _CONFIG, reason="HERMES_TEST_KUBECONFIG not set")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    namespace = "hermes-test-" + uuid.uuid4().hex[:10]

    def admin(*args, data=None):
        return subprocess.run(["kubectl", "--kubeconfig", _CONFIG, *args], input=data,
                              capture_output=True, text=True, timeout=120, check=True).stdout

    admin("create", "namespace", namespace)
    try:
        objects = [
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "hermes"}},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "sandbox"},
             "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"], "resourceNames": ["sandbox-0"]},
                       {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["get", "create"],
                        "resourceNames": ["sandbox-0"]}]},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "sandbox"},
             "subjects": [{"kind": "ServiceAccount", "name": "hermes", "namespace": namespace}],
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "sandbox"}},
            {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "sandbox-0"},
             "spec": {"automountServiceAccountToken": False, "containers": [
                 {"name": "sandbox", "image": "python:3.11-slim", "imagePullPolicy": "IfNotPresent",
                  "command": ["sleep", "infinity"]}]}},
        ]
        admin("--namespace", namespace, "apply", "-f", "-", data=json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}))
        admin("--namespace", namespace, "wait", "--for=condition=Ready", "pod/sandbox-0", "--timeout=90s")
        config = json.loads(admin("config", "view", "--minify", "--flatten", "--raw", "-o", "json"))
        # Only the cluster CA/server are retained, never the administrator's user credentials.
        token = admin("--namespace", namespace, "create", "token", "hermes", "--duration=10m").strip()
        scoped = {"apiVersion": "v1", "kind": "Config", "clusters": config["clusters"],
                  "users": [{"name": "hermes", "user": {"token": token}}],
                  "contexts": [{"name": "test", "context": {"cluster": config["clusters"][0]["name"], "user": "hermes"}}],
                  "current-context": "test"}
        path = tmp_path_factory.mktemp("kubernetes") / "config"
        path.write_text(json.dumps(scoped))
        path.chmod(0o600)
        yield namespace, path
    finally:
        admin("delete", "namespace", namespace, "--wait=false")


@pytest.fixture
def sandbox(cluster, monkeypatch):
    namespace, path = cluster
    monkeypatch.setenv("KUBECONFIG", str(path))
    env = KubernetesEnvironment(namespace, "sandbox-0", "sandbox", cwd="/tmp", timeout=20)
    yield env
    env.cleanup()


def test_real_terminal_and_binary_stream(sandbox, tmp_path):
    env = sandbox
    assert env.execute("mkdir -p work; cd work; export RETAINED=present")["returncode"] == 0
    result = env.execute('printf "%s|%s|" "$RETAINED" "$PWD"; cat; exit 7', stdin_data="код\nlast line")
    assert result["returncode"] == 7 and "present|/tmp/work|код\nlast line" in result["output"]
    data = bytes(range(256)) * 1024
    proc = streams.open_stream(env, ["python3", "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"])
    received, _ = proc.communicate(data, timeout=15)
    assert proc.returncode == 0 and received == data
    env.execute("printf artifact > /tmp/artifact")
    env.fetch_file("/tmp/artifact", tmp_path / "received", max_bytes=1024)
    assert (tmp_path / "received").read_text() == "artifact"
    result = streams.run_in(env, ["id", "-un"], user="nobody")
    assert result.returncode == 0 and result.stdout.strip() == b"nobody"


def test_timeout_kills_only_command_group_and_leaves_pod_running(sandbox):
    env = sandbox
    result = env.execute("sleep 60 & echo $! >/tmp/timed-pid; wait", timeout=2)
    assert result["returncode"] == 124
    check = env.execute("python3 -c " + shlex.quote(
        "from pathlib import Path; p=Path('/proc')/Path('/tmp/timed-pid').read_text().strip()/'stat'; "
        "print('gone' if not p.exists() else p.read_text().split(') ',1)[1].split()[0])"))
    assert check["output"].strip() in {"gone", "Z"}
    assert env.execute("printf still-running")["output"].strip() == "still-running"


def test_browser_loopback_forward_uses_api_stream(sandbox):
    server = streams.open_stream(sandbox, ["python3", "-u", "-c",
        "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1); "
        "print(s.getsockname()[1],flush=True); c,_=s.accept(); "
        "c.sendall(c.recv(4096)); c.close(); s.close()"])
    port = int(server.stdout.readline())
    forward = streams.PortForward(sandbox, "127.0.0.1", port, user=None)
    try:
        with socket.create_connection(("127.0.0.1", forward.local_port), timeout=10) as connection:
            connection.sendall(b"cdp-loopback-through-api")
            assert connection.recv(4096) == b"cdp-loopback-through-api"
        assert server.wait(timeout=15) == 0
    finally:
        forward.close()
        if server.poll() is None:
            server.kill()


def test_scoped_account_cannot_control_other_workloads(sandbox):
    env = sandbox
    for args in (["get", "pods"], ["get", "pod", "unrelated"], ["get", "secrets"],
                 ["delete", "pod", "sandbox-0"], ["exec", "unrelated", "--", "true"]):
        result = subprocess.run([env._kubectl_exe, "--namespace", env.namespace, *args],
                                capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode != 0 and "Forbidden" in result.stderr
    assert streams.run_in(env, ["test", "-e", "/var/run/secrets/kubernetes.io/serviceaccount/token"]).returncode != 0
    assert env.execute("true")["returncode"] == 0


def test_tun_allocation_without_privileged_container(cluster):
    namespace, _ = cluster
    client = ["kubectl", "--kubeconfig", _CONFIG, "--namespace", namespace]
    nodes = json.loads(subprocess.check_output([*client, "get", "nodes", "-o", "json"], text=True))
    if not any(int(n["status"].get("allocatable", {}).get("devic.es/tun", "0")) for n in nodes["items"]):
        pytest.skip("Optional TUN device plugin is not installed")
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "tun-check"}, "spec": {
        "automountServiceAccountToken": False, "containers": [{"name": "check", "image": "python:3.11-slim",
            "command": ["sleep", "infinity"], "securityContext": {"privileged": False,
                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"], "add": ["NET_ADMIN"]}},
            "resources": {"limits": {"devic.es/tun": "1"}}}]}}
    subprocess.run([*client, "apply", "-f", "-"], input=json.dumps(pod), text=True, capture_output=True, check=True)
    subprocess.run([*client, "wait", "--for=condition=Ready", "pod/tun-check", "--timeout=60s"],
                   capture_output=True, check=True, timeout=65)
    code = ("import os,fcntl,struct; fd=os.open('/dev/net/tun',os.O_RDWR); "
            "fcntl.ioctl(fd,0x400454ca,struct.pack('16sH',b'hermescheck',0x1001)); "
            "print('tun-created'); os.close(fd)")
    result = subprocess.run([*client, "exec", "tun-check", "--", "python3", "-c", code],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0 and result.stdout.strip() == "tun-created"


@pytest.mark.skipif(not _CLIENT_IMAGE, reason="Optional kubectl client image HERMES_TEST_IMAGE not set")
def test_in_cluster_serviceaccount_on_readonly_root(cluster):
    namespace, _ = cluster
    client = ["kubectl", "--kubeconfig", _CONFIG, "--namespace", namespace]
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "client-check"}, "spec": {
        "serviceAccountName": "hermes", "containers": [{"name": "client", "image": _CLIENT_IMAGE,
            "imagePullPolicy": "IfNotPresent", "command": ["/usr/local/bin/agentsee-init", "--", "sleep", "infinity"],
            "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "cache", "mountPath": "/root/.kube"}]}],
        "volumes": [{"name": "cache", "emptyDir": {}}]}}
    subprocess.run([*client, "apply", "-f", "-"], input=json.dumps(pod), text=True, capture_output=True, check=True)
    subprocess.run([*client, "wait", "--for=condition=Ready", "pod/client-check", "--timeout=60s"],
                   capture_output=True, check=True, timeout=65)
    result = subprocess.run([*client, "exec", "client-check", "--", "kubectl", "--namespace", namespace,
                             "exec", "sandbox-0", "--container", "sandbox", "--", "printf", "in-cluster-ok"],
                            capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0 and result.stdout == "in-cluster-ok"
    denied = subprocess.run([*client, "exec", "client-check", "--", "kubectl", "--namespace", namespace,
                             "get", "secrets"], capture_output=True, text=True, check=False, timeout=20)
    assert denied.returncode != 0 and "Forbidden" in denied.stderr
