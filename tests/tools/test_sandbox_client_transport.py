"""Sandbox clients retain their selected transport without forwarding host secrets."""

import json
import os
import socket
import subprocess
import sys

import pytest


@pytest.mark.parametrize("consumer", ["browser", "computer_use"])
@pytest.mark.platforms("posix")
def test_sandbox_tool_subprocess_reaches_selected_transport(monkeypatch, tmp_path, consumer):
    from tools import browser_tool_session
    from tools.bot_desktop import runtime, sandbox_host
    from tools.computer_use import cua_backend
    from tools.environments.docker import DockerEnvironment

    client = tmp_path / "docker"
    client.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'docker_host': os.getenv('DOCKER_HOST'), "
        "'ssh_agent': os.getenv('SSH_AUTH_SOCK'), "
        "'provider_secret': os.getenv('OPENAI_API_KEY'), "
        "'panel_secret': os.getenv('HERMES_PANEL_CREDENTIAL'), "
        "'remote_command': sys.argv[-1]}))\n"
    )
    client.chmod(0o700)

    class Sandbox(DockerEnvironment):
        def __init__(self):
            self._docker_exe = str(client)
            self._container_id = "this-clients-sandbox"
            self._bd_browser_dirs_ready = True
            self.chromium_sandbox_enabled = False

        def get_temp_dir(self):
            return "/tmp"  # no-tmp: sandbox-side path

    sandbox = Sandbox()
    monkeypatch.setenv("DOCKER_HOST", "unix:///run/client-only/docker.sock")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/client-only/ssh-agent.sock")
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "sandbox-only.kubeconfig"))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret-must-stay-out")
    monkeypatch.setenv("HERMES_PANEL_CREDENTIAL", "panel-secret-must-stay-out")
    monkeypatch.setattr(runtime, "_sandbox_env", lambda **_: sandbox)
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":20"})
    monkeypatch.setattr(sandbox_host, "_user_for", lambda _: "pn")
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda _: "/home/pn/browser-profile")

    if consumer == "browser":
        monkeypatch.setattr(browser_tool_session, "_browser_in_sandbox", lambda: True)
        command, environment = browser_tool_session._sandbox_wrap(
            ["agent-browser", "--json", "snapshot"], {"PATH": os.environ["PATH"]}, str(tmp_path)
        )
    else:
        monkeypatch.setattr(runtime, "tool_placement", lambda: "terminal")
        (executable, arguments), environment = cua_backend.sandbox_mcp_invocation()
        command = [executable, *arguments]

    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=True)
    observed = json.loads(result.stdout)
    assert observed["docker_host"] == os.environ["DOCKER_HOST"]
    assert observed["ssh_agent"] == os.environ["SSH_AUTH_SOCK"]
    assert environment["KUBECONFIG"] == os.environ["KUBECONFIG"]
    assert environment["KUBERNETES_SERVICE_HOST"] == "10.43.0.1"
    assert environment["KUBERNETES_SERVICE_PORT"] == "443"
    assert observed["provider_secret"] is None
    assert observed["panel_secret"] is None
    assert "DOCKER_HOST" not in observed["remote_command"]
    assert "SSH_AUTH_SOCK" not in observed["remote_command"]
    assert "KUBECONFIG" not in observed["remote_command"]


@pytest.mark.platforms("posix")
def test_browser_attaches_using_the_sandbox_profile_and_loopback(monkeypatch, tmp_path):
    from tools import browser_tool_session
    from tools.bot_desktop import browser, runtime, sandbox_host
    from tools.environments import streams

    profile = tmp_path / "sandbox-profile"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to(f"sandbox-{os.getpid()}")
    monkeypatch.setattr(browser_tool_session, "_browser_in_sandbox", lambda: True)
    monkeypatch.setattr(runtime, "_sandbox_env", lambda **_: object())
    monkeypatch.setattr(runtime, "published_env", lambda: {"DISPLAY": ":20"})
    monkeypatch.setattr(browser, "profile_dir", lambda: tmp_path / "gateway-profile")
    monkeypatch.setattr(sandbox_host, "browser_profile_dir", lambda _: str(profile))
    monkeypatch.setattr(sandbox_host, "_user_for", lambda _: "pn")
    monkeypatch.setattr(streams, "run_in", lambda env, argv, **kwargs: subprocess.run(
        [sys.executable, *argv[1:]], capture_output=True, timeout=5, check=False
    ))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/test\n")
        session = {"session_name": "another-browser-client", "features": {"local": True}}
        assert browser_tool_session._bot_desktop_attach_port(session) == port

    assert browser_tool_session._bot_desktop_attach_port(session) is None
