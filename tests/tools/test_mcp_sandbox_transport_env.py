"""Every stdio MCP child retains its owning profile and safe sandbox transport metadata."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools.mcp_tool_config import _build_safe_env
from tools.mcp_tool_transport import _stdio_launch


def test_mcp_env_keeps_active_profile_and_transport_without_secrets_or_global_mutation(tmp_path):
    ambient = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "launcher"),
        "HERMES_HOME": str(tmp_path / "wrong-profile"), "KUBERNETES_SERVICE_HOST": "192.0.2.27",
        "KUBERNETES_SERVICE_PORT": "6443", "KUBERNETES_SERVICE_PORT_HTTPS": "6443",
        "KUBECONFIG": "/private/another-profile", "OPENAI_API_KEY": "synthetic-secret",
        "GITHUB_TOKEN": "synthetic-github-secret", "TERMINAL_KUBERNETES_NAMESPACE": "wrong-namespace"}
    own_home = tmp_path / "served-profile"
    token = set_hermes_home_override(own_home)
    try:
        with patch.dict(os.environ, ambient, clear=True):
            before = dict(os.environ)
            result = _build_safe_env(None)
            assert result["HERMES_HOME"] == str(own_home)
            for key in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBERNETES_SERVICE_PORT_HTTPS"):
                assert result[key] == ambient[key]
            for key in ("KUBECONFIG", "OPENAI_API_KEY", "GITHUB_TOKEN", "TERMINAL_KUBERNETES_NAMESPACE"):
                assert key not in result
            configured = _build_safe_env({"HERMES_HOME": str(tmp_path / "explicit-profile"),
                "KUBERNETES_SERVICE_HOST": "192.0.2.28", "KUBERNETES_SERVICE_PORT": "7443",
                "KUBERNETES_SERVICE_PORT_HTTPS": "7443"})
            assert configured["HERMES_HOME"] == str(tmp_path / "explicit-profile")
            assert configured["KUBERNETES_SERVICE_HOST"] == "192.0.2.28"
            assert configured["KUBERNETES_SERVICE_PORT"] == configured["KUBERNETES_SERVICE_PORT_HTTPS"] == "7443"
            assert dict(os.environ) == before
    finally:
        reset_hermes_home_override(token)


def test_actual_stdio_children_resolve_each_own_sandbox_from_profile_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-launch-profile"))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.27")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "6443")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "6443")
    monkeypatch.setenv("TERMINAL_KUBERNETES_NAMESPACE", "wrong-launch-namespace")
    monkeypatch.setenv("KUBECONFIG", "/private/another-profile")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    code = '''import json, os
from pathlib import Path
from hermes_constants import get_hermes_home
from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
from tools.terminal_tool import _get_env_config
token = install_profile_terminal_scope(get_hermes_home())
try:
    config = _get_env_config()
    print(json.dumps({"namespace": config["kubernetes_namespace"], "pod": config["kubernetes_pod"],
        "host": os.environ.get("KUBERNETES_SERVICE_HOST"), "port": os.environ.get("KUBERNETES_SERVICE_PORT"),
        "httpsPort": os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS"),
        "secretExcluded": "OPENAI_API_KEY" not in os.environ, "kubeconfigExcluded": "KUBECONFIG" not in os.environ}))
finally:
    reset_terminal_scope(token)
'''
    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps({"terminal": {"backend": "kubernetes",
            "kubernetes_namespace": "namespace-" + name, "kubernetes_pod": "sandbox-0", "kubernetes_container": "sandbox"}}))
        token = set_hermes_home_override(home)
        try:
            command, env, cwd = _stdio_launch({"command": sys.executable, "cwd": str(Path(__file__).resolve().parents[2])})
            result = subprocess.run([command, "-c", code], env=env, cwd=cwd, capture_output=True, text=True,
                timeout=15, check=True)
        finally:
            reset_hermes_home_override(token)
        assert json.loads(result.stdout) == {"namespace": "namespace-" + name, "pod": "sandbox-0",
            "host": "192.0.2.27", "port": "6443", "httpsPort": "6443", "secretExcluded": True, "kubeconfigExcluded": True}
