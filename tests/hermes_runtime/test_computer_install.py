"""Native sandbox configuration preserves durable state and uses Hermes' desktop runtime."""

import json
import sqlite3

import pytest

from hermes_runtime.standalone import computer, config, panel


def test_docker_configuration_preserves_history_profile_and_operator_settings(tmp_path, monkeypatch):
    import subprocess
    from tools.bot_desktop.sandbox_host import SANDBOX_IMAGE_HINT

    home = tmp_path / "hermes"
    config.initialize(home)
    profile = home / "bot-desktop/browser-profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_bytes(b"existing authenticated profile")
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE history (message TEXT)")
        db.execute("INSERT INTO history VALUES ('same eternal conversation')")
    before = (home / "state.db").read_bytes()
    settings = config.read_config(home)
    settings["terminal"] = {"container_cpu": 2, "docker_volumes": ["/projects:/workspace/projects"]}
    settings["autonomous"]["panel"] = {"url": "https://panel.example", "environment_id": "existing"}
    (home / "config.yaml").write_text(json.dumps(settings))
    original = (home / "config.yaml").read_bytes()

    def no_host_install(*args, **kwargs):
        pytest.fail("Computer setup must not install packages or launch a host browser")
    monkeypatch.setattr(subprocess, "run", no_host_install)
    result = computer.install(home)
    computer.install(home)

    assert (home / "state.db").read_bytes() == before
    assert (home / "config.yaml.before-computer").read_bytes() == original
    assert (profile / "Cookies").read_bytes() == b"existing authenticated profile"
    settings = config.read_config(home)
    assert settings["terminal"] == {
        "backend": "docker", "docker_image": SANDBOX_IMAGE_HINT, "container_persistent": True,
        "docker_persist_across_processes": True, "container_cpu": 2,
        "docker_volumes": ["/projects:/workspace/projects"],
    }
    assert settings["bot_desktop"]["placement"] == "auto"
    assert settings["bot_desktop"]["idle_stop_minutes"] == 0
    assert settings["browser"]["headed"] is True
    assert settings["autonomous"]["panel"]["environment_id"] == "existing"
    assert result["backend"] == "docker"


def test_legacy_host_configuration_migrates_without_touching_browser_data(tmp_path):
    home = tmp_path / "hermes"
    config.initialize(home)
    profile = tmp_path / "old Chrome profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"keep existing logins")
    settings = config.read_config(home)
    settings["autonomous"]["computer"] = {
        "enabled": True, "browser_executable": "/usr/bin/google-chrome", "browser_profile": str(profile),
    }
    settings["bot_desktop"] = {"placement": "gateway"}
    (home / "config.yaml").write_text(json.dumps(settings))
    original = (home / "config.yaml").read_bytes()

    computer.configure(home)

    settings = config.read_config(home)
    assert settings["autonomous"]["computer"] == {"enabled": True}
    assert settings["bot_desktop"]["placement"] == "auto"
    assert (profile / "Cookies").read_bytes() == b"keep existing logins"
    assert (home / "config.yaml.before-computer").read_bytes() == original


def test_host_browser_overrides_are_not_inherited_by_sandbox_worker(tmp_path, monkeypatch):
    from tools.environments import local
    home = tmp_path / "hermes"
    config.initialize(home)
    computer.configure(home)
    monkeypatch.setattr(local, "served_profile_child_env", lambda **_: {
        "DISPLAY": ":old", "XAUTHORITY": "/old/cookie", "XDG_RUNTIME_DIR": "/run/user/1000",
        "AGENT_BROWSER_PROFILE": "/old/host-profile", "AGENT_BROWSER_EXECUTABLE_PATH": "/usr/bin/google-chrome",
        "HERMES_PANEL_CREDENTIAL": "keep-secret",
    })
    environment = config.child_environment(home)
    assert "DISPLAY" not in environment and "XAUTHORITY" not in environment
    assert "AGENT_BROWSER_PROFILE" not in environment
    assert "AGENT_BROWSER_EXECUTABLE_PATH" not in environment
    assert environment["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert environment["HERMES_PANEL_CREDENTIAL"] == "keep-secret"


def test_computer_cli_configures_native_sandbox_before_installing_service(tmp_path, monkeypatch):
    from hermes_runtime.standalone import cli, service
    calls = []

    def install_service(home, enable, *, rdp):
        calls.append((home, enable, rdp, config.read_config(home)["terminal"]["backend"]))
        return {"enabled": enable}

    monkeypatch.setattr(service, "install_service", install_service)
    result = cli.execute(cli.parser().parse_args(["install-service", "--computer", "--enable"]), tmp_path)
    assert calls == [(tmp_path, True, False, "docker")]
    assert result["computer"]["backend"] == "docker"


def test_desktop_start_uses_native_sandbox_runtime(monkeypatch):
    from tools.bot_desktop import placement, runtime
    calls = []
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(placement.TERMINAL, "docker"))
    monkeypatch.setattr(runtime, "start", lambda: calls.append("native sandbox"))
    computer.start_desktop()
    assert calls == ["native sandbox"]


@pytest.mark.parametrize("backend,where", [("local", "gateway"), ("docker", "gateway"), ("ssh", "terminal")])
def test_computer_mode_never_falls_back_to_host_or_another_backend(backend, where, monkeypatch):
    from tools.bot_desktop import placement, runtime
    monkeypatch.setattr(placement, "resolve", lambda: placement.Placement(where, backend))
    monkeypatch.setattr(runtime, "start", lambda: pytest.fail("An unintended desktop was started"))
    with pytest.raises(ValueError, match="Docker sandbox"):
        computer.start_desktop()


def test_connector_only_accepts_screen_methods_and_never_a_foreign_profile():
    with pytest.raises(ValueError, match="Only Bot Screen"):
        panel.screen_request(json.dumps({"id": 1, "method": "terminal.execute"}))
    request = json.loads(panel.screen_request(json.dumps({"id": 1, "method": "display.lease.release",
        "params": {"viewer_id": "minted-by-native", "force": True, "profile_key": "foreign"}})))
    assert request["params"] == {"viewer_id": "minted-by-native"}
    with pytest.raises(ValueError, match="params must be an object"):
        panel.screen_request(json.dumps({"id": 2, "method": "display.observe", "params": ["foreign"]}))
    assert panel.socket_url("https://panel.example", "env_a", "agent") == "wss://panel.example/app/api/environments/env_a/computer/agent"
    for url in ("http://panel.example", "https://user:secret@panel.example", "https://panel.example/app"):
        with pytest.raises(ValueError):
            panel.panel_url(url)


def test_enrollment_retry_reuses_locally_persisted_credential(tmp_path, monkeypatch):
    import io
    from dotenv import dotenv_values
    home = tmp_path / "hermes"
    config.initialize(home)
    calls = []
    def open_request(request, **kwargs):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"environmentId": "env_a"}).encode())
    monkeypatch.setattr(panel, "urlopen", open_request)
    panel.enroll(home, "https://panel.example", "env_a", "one-time")
    panel.enroll(home, "https://panel.example", "env_a", "one-time")
    assert calls[0]["credential"] == calls[1]["credential"] == dotenv_values(home / ".env")["HERMES_PANEL_CREDENTIAL"]
    assert (home / ".env").stat().st_mode & 0o777 == 0o600
    assert "one-time" not in (home / "config.yaml").read_text()


def test_machine_secret_is_scoped_to_service_home_and_scrubbed_from_tools(tmp_path, monkeypatch):
    from tools.environments.local import hermes_subprocess_env
    home = tmp_path / "other-profile"
    config.initialize(home)
    with (home / ".env").open("a") as output:
        output.write("HERMES_PANEL_CREDENTIAL=target-profile-credential\n")
    monkeypatch.setenv("HERMES_PANEL_CREDENTIAL", "wrong-launch-profile")
    assert config.child_environment(home)["HERMES_PANEL_CREDENTIAL"] == "target-profile-credential"
    for inherit in (False, True):
        environment = hermes_subprocess_env(base_env={"HERMES_PANEL_CREDENTIAL": "secret"}, inherit_credentials=inherit)
        assert "HERMES_PANEL_CREDENTIAL" not in environment


def test_old_xfce_login_hook_cannot_rebind_or_restart_native_computer(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session
    home = tmp_path / "native-profile"
    config.initialize(home)
    computer.configure(home)
    def notify(*args):
        pytest.fail("An unrelated Xfce login restarted the native computer owner")
    monkeypatch.setattr(desktop_session, "_notify_service", notify)
    result = desktop_session.capture_session(home, {"DISPLAY": ":old-rdp", "XDG_CURRENT_DESKTOP": "XFCE"})
    assert result["captured"] is False
    assert not desktop_session.snapshot_path(home).exists()


@pytest.mark.parametrize("close_code,cancel", [(1000, False), (1012, False), (1012, True)])
def test_rfb_bridge_preserves_clean_close_and_keeps_network_loss_distinct(close_code, cancel):
    import asyncio
    from websockets.asyncio.server import serve

    async def scenario():
        closed = asyncio.get_running_loop().create_future()
        remote_ready, finish = asyncio.Event(), asyncio.Event()

        async def native_handler(socket):
            assert socket.request.path == "/api/display/ws?display_ticket=native-ticket"
            await socket.send(b"RFB synthetic frame")
            await socket.wait_closed()
            closed.set_result(socket.close_code)

        async def panel_handler(socket):
            assert socket.request.headers["Authorization"] == "Bearer panel-secret"
            assert await socket.recv() == b"RFB synthetic frame"
            remote_ready.set()
            await finish.wait()
            await socket.close(code=close_code)

        async with serve(native_handler, "127.0.0.1", 0) as native, serve(panel_handler, "127.0.0.1", 0) as remote:
            config = {"url": f"http://127.0.0.1:{remote.sockets[0].getsockname()[1]}", "environment_id": "env_a"}
            task = asyncio.create_task(panel.bridge({"kind": "rfb", "id": "connection-a", "ticket": "native-ticket"},
                config, native.sockets[0].getsockname()[1], "panel-secret", "not-forwarded"))
            await asyncio.wait_for(remote_ready.wait(), 5)
            if cancel:
                task.cancel()
            else:
                finish.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            assert await asyncio.wait_for(closed, 5) == close_code
            finish.set()

    asyncio.run(scenario())
