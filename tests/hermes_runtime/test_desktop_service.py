"""Xfce login reconnects the same autonomous owner without persisting stale boot credentials."""

import configparser
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_service_and_xfce_autostart_use_the_same_home_and_interpreter(tmp_path):
    from hermes_runtime.standalone.service import render_service, render_autostart, service_name

    home = tmp_path / "main data % literal"
    interpreter = "/opt/hermes env/bin/python"
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(render_service(home, interpreter))
    assert "service-run" in unit["Service"]["ExecStart"]
    assert str(home).replace("%", "%%") in unit["Service"]["ExecStart"]
    assert unit["Service"]["KillMode"] == "mixed"
    assert unit["Service"]["Restart"] == "always"
    entry = configparser.ConfigParser(interpolation=None)
    entry.read_string(render_autostart(home, interpreter))
    command = shlex.split(entry["Desktop Entry"]["Exec"].replace("%%", "%"))
    assert command == [interpreter, "-m", "hermes_runtime.standalone.cli", "--home", str(home), "desktop-session"]
    assert entry["Desktop Entry"]["OnlyShowIn"] == "XFCE;"
    assert service_name(home) == service_name(home.resolve())


@pytest.mark.platforms("posix")
def test_gui_reconnect_replaces_boot_snapshot_but_keeps_native_history(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session
    from hermes_state import SessionDB

    home = tmp_path / "home"
    home.mkdir()
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session("main", source="cli", system_prompt="Saved system prefix")
        db.append_message("main", "user", "Keep the ongoing task")
        before = db.get_messages_as_conversation("main")
        environment = {"DISPLAY": ":7", "XAUTHORITY": str(tmp_path / "cookie-one"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus", "XDG_CURRENT_DESKTOP": "XFCE",
            "XDG_SESSION_TYPE": "x11", "OPENAI_API_KEY": "must-not-be-copied"}
        monkeypatch.setattr(desktop_session.psutil, "boot_time", lambda: 100)
        assert desktop_session.capture_session(home, environment, notify=False)["captured"]
        first = desktop_session.session_environment(home, {"DISPLAY": ":wrong", "PATH": "/usr/bin"})
        assert first["DISPLAY"] == ":7" and first["PATH"] == "/usr/bin"
        assert "must-not-be-copied" not in desktop_session.snapshot_path(home).read_text()
        monkeypatch.setattr(desktop_session.psutil, "boot_time", lambda: 200)
        assert "DISPLAY" not in desktop_session.session_environment(home, {"PATH": "/usr/bin"})
        changed = {**environment, "DISPLAY": ":9", "XAUTHORITY": str(tmp_path / "cookie-two")}
        assert desktop_session.capture_session(home, changed, notify=False)["captured"]
        restored = desktop_session.session_environment(home, {"WAYLAND_DISPLAY": "wayland-old"})
        assert restored["DISPLAY"] == ":9" and restored["XAUTHORITY"].endswith("cookie-two")
        assert "WAYLAND_DISPLAY" not in restored
        assert db.get_session("main")["system_prompt"] == "Saved system prefix"
        assert db.get_messages_as_conversation("main") == before
        mode = desktop_session.snapshot_path(home).stat().st_mode & 0o777
        assert mode == 0o600


@pytest.mark.platforms("posix")
def test_capture_notifies_only_enabled_service_and_executes_existing_run(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session
    from hermes_runtime.standalone.service import service_name

    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="enabled\n")
    monkeypatch.setattr(desktop_session.subprocess, "run", run)
    env = {"DISPLAY": ":12", "XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11"}
    assert desktop_session.capture_session(tmp_path, env)["captured"]
    assert calls[-1][-2:] == ["restart", service_name(tmp_path)]
    calls.clear()
    desktop_session.capture_session(tmp_path, env)
    assert calls[-1][-2:] == ["start", service_name(tmp_path)]
    monkeypatch.setattr(desktop_session.os, "execvpe", lambda executable, argv, env: calls.append((executable, argv, env)))
    desktop_session.run_service(tmp_path)
    assert calls[-1][1][-3:] == ["--home", str(tmp_path), "run"]
    assert calls[-1][2]["DISPLAY"] == ":12"
    monkeypatch.setattr(desktop_session.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="disabled\n"))
    assert not desktop_session.capture_session(tmp_path, {**env, "DISPLAY": ":13"})["service_notified"]


@pytest.mark.platforms("posix")
def test_ssh_display_and_foreign_or_corrupt_snapshot_are_not_adopted(tmp_path):
    from hermes_runtime.standalone import desktop_session

    assert not desktop_session.capture_session(tmp_path,
        {"DISPLAY": "localhost:10.0", "XDG_CURRENT_DESKTOP": "XFCE"}, notify=False)["captured"]
    assert not desktop_session.snapshot_path(tmp_path).exists()
    path = desktop_session.snapshot_path(tmp_path)
    path.parent.mkdir()
    path.write_text("not json")
    path.chmod(0o600)
    assert desktop_session.session_environment(tmp_path, {}) == {}
    path.write_text(json.dumps({"uid": os.getuid() + 1, "boot_time": desktop_session.psutil.boot_time(),
                                "environment": {"DISPLAY": ":99"}}))
    assert desktop_session.session_environment(tmp_path, {}) == {}


@pytest.mark.platforms("posix")
def test_ssh_install_discovers_only_its_own_unambiguous_xfce_session(monkeypatch):
    from hermes_runtime.standalone import desktop_session

    def session(uid, display):
        return SimpleNamespace(info={"name": "xfce4-session", "uids": SimpleNamespace(real=uid)},
            environ=lambda: {"DISPLAY": display, "DESKTOP_SESSION": "xubuntu"})
    own = session(os.getuid(), ":2")
    monkeypatch.setattr(desktop_session.psutil, "process_iter", lambda *args: [session(os.getuid() + 1, ":0"), own])
    assert desktop_session.discover_session({"SSH_CONNECTION": "test"})["DISPLAY"] == ":2"
    monkeypatch.setattr(desktop_session.psutil, "process_iter", lambda *args: [own, session(os.getuid(), ":3")])
    with pytest.raises(ValueError, match="Several Xfce"):
        desktop_session.discover_session({})


@pytest.mark.platforms("posix")
def test_service_boot_rediscovers_live_desktop_and_drops_stale_manager_display(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session

    monkeypatch.setenv("DISPLAY", "localhost:10.0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-stale")
    calls = []
    monkeypatch.setattr(desktop_session.os, "execvpe", lambda executable, argv, env: calls.append(env))
    monkeypatch.setattr(desktop_session.psutil, "process_iter", lambda *args: [])
    desktop_session.run_service(tmp_path)
    assert not (desktop_session.GUI_KEYS & calls[-1].keys())
    live = SimpleNamespace(info={"name": "xfce4-session", "uids": SimpleNamespace(real=os.getuid())},
        environ=lambda: {"DISPLAY": ":3", "XAUTHORITY": "/run/user/test/Xauthority", "DESKTOP_SESSION": "xubuntu"})
    monkeypatch.setattr(desktop_session.psutil, "process_iter", lambda *args: [live])
    desktop_session.run_service(tmp_path)
    assert calls[-1]["DISPLAY"] == ":3"
    assert calls[-1]["XAUTHORITY"] == "/run/user/test/Xauthority"
    assert "WAYLAND_DISPLAY" not in calls[-1]


@pytest.mark.platforms("posix")
def test_hook_retries_binding_if_previous_service_notification_failed(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session

    env = {"DISPLAY": ":5", "XDG_CURRENT_DESKTOP": "XFCE"}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if "is-enabled" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="enabled\n")
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(desktop_session.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        desktop_session.capture_session(tmp_path, env)
    assert desktop_session.capture_session(tmp_path, env)["service_notified"]
    assert calls[-1][-2] == "restart"


@pytest.mark.platforms("linux")
def test_linux_installer_registers_one_owner_and_xfce_login_hook(tmp_path, monkeypatch):
    from hermes_runtime.standalone import service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("DISPLAY", ":77")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
        lambda argv, **kwargs: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
    result = service.install_service(tmp_path / "agent-home", True)
    unit = Path(result["unit"])
    hook = Path(result["autostart"])
    assert unit.exists() and hook.exists()
    assert result["desktop_session"]["display"] == ":77"
    assert calls[0] == ["loginctl", "enable-linger"]
    assert calls[-1] == ["systemctl", "--user", "restart", unit.name]
    assert sum("restart" in call for call in calls) == 1
