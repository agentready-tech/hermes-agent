"""The shared Xfce desktop starts before an RDP viewer and is reused on reconnect."""

import configparser
import subprocess
from types import SimpleNamespace

import pytest


def test_rdp_option_reaches_the_existing_installer(tmp_path, monkeypatch):
    from hermes_runtime.standalone import cli, service

    calls = []
    monkeypatch.setattr(service, "install_service", lambda home, enable, **kwargs: calls.append((home, enable, kwargs)))
    cli.execute(cli.parser().parse_args(["install-service", "--rdp", "--enable"]), tmp_path)
    assert calls == [(tmp_path, True, {"rdp": True})]


def test_rdp_configuration_preserves_other_settings_and_is_idempotent():
    from hermes_runtime.standalone.rdp import configure_ini

    original = "; Keep my settings\n[Sessions]\nPolicy = Separate\nKillDisconnected=true\nMaxSessions=12\n\n[Security]\nAllowRootLogin=false\n"
    changes = {"Policy": "Default", "KillDisconnected": "false", "DisconnectedTimeLimit": "0", "IdleTimeLimit": "0"}
    result = configure_ini(original, "Sessions", changes)
    config = configparser.ConfigParser()
    config.read_string(result)
    assert config["Sessions"]["Policy"] == "Default"
    assert not config["Sessions"].getboolean("KillDisconnected")
    assert config["Sessions"]["MaxSessions"] == "12"
    assert not config["Security"].getboolean("AllowRootLogin")
    assert result.startswith("; Keep my settings\n")
    assert configure_ini(result, "Sessions", changes) == result


def test_rdp_unit_runs_as_desktop_user_and_keeps_the_hermes_home(tmp_path):
    from hermes_runtime.standalone.rdp import render_unit

    user = SimpleNamespace(pw_name="desktop-user", pw_uid=1201, pw_dir=str(tmp_path))
    home = tmp_path / "agent % data"
    unit = configparser.ConfigParser(interpolation=None)
    unit.read_string(render_unit(home, "/opt/hermes env/bin/python", user))
    assert unit["Service"]["User"] == "desktop-user"
    assert "rdp-start" in unit["Service"]["ExecStart"]
    assert str(home).replace("%", "%%") in unit["Service"]["ExecStart"]
    assert unit["Service"]["RemainAfterExit"] == "yes"
    assert "xrdp-sesman.service" in unit["Unit"]["Requires"]


@pytest.mark.platforms("posix")
def test_rdp_owner_waits_for_a_ready_desktop_before_starting_the_agent(tmp_path, monkeypatch):
    from hermes_runtime.standalone import desktop_session, rdp_session

    (tmp_path / "config.yaml").write_text("autonomous:\n  rdp: true\n")
    calls = []
    def not_ready(home):
        raise RuntimeError("desktop unavailable")
    monkeypatch.setattr(rdp_session, "wait_for_desktop", not_ready)
    monkeypatch.setattr(desktop_session.os, "execvpe", lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match="desktop unavailable"):
        desktop_session.run_service(tmp_path)
    assert calls == []
    env = {"DISPLAY": ":10", "XDG_CURRENT_DESKTOP": "XFCE", "XRDP_SESSION": "1"}
    def ready(home):
        desktop_session.capture_session(home, env, notify=False)
        return env
    monkeypatch.setattr(rdp_session, "wait_for_desktop", ready)
    desktop_session.run_service(tmp_path)
    assert calls[-1][2]["DISPLAY"] == ":10"


def test_rdp_reinstall_preserves_configuration_and_remembers_the_mode(tmp_path):
    from hermes_runtime.standalone.rdp import remember
    from hermes_runtime.standalone.config import read_config

    path = tmp_path / "config.yaml"
    path.write_text("# My model\nmodel: custom\nmemory:\n  provider: holographic\nautonomous:\n  gmail:\n    enabled: true\n")
    original = path.read_text()
    remember(tmp_path)
    first = path.read_text()
    remember(tmp_path)
    assert path.read_text() == first and "# My model" in first
    assert path.with_suffix(".yaml.hermes-before-rdp").read_text() == original
    config = read_config(tmp_path)
    assert config["autonomous"]["rdp"] is True
    assert config["autonomous"]["gmail"]["enabled"] is True
    assert config["model"] == "custom" and config["memory"]["provider"] == "holographic"
    assert config["terminal"]["backend"] == "local"
    assert config["browser"]["headed"] and config["browser"]["cloud_provider"] == "local"
    assert config["browser"]["engine"] == "chrome" and config["browser"]["cdp_url"] == ""
    assert config["bot_desktop"]["auto_start"] is False


@pytest.mark.platforms("posix")
def test_old_xrdp_is_rejected_before_any_privileged_change(tmp_path, monkeypatch):
    from hermes_runtime.standalone import rdp

    monkeypatch.setattr(rdp, "command", lambda name: [name])
    monkeypatch.setattr(rdp.subprocess, "run", lambda *args, **kwargs:
        subprocess.CompletedProcess(args, 0, stdout="xrdp 0.9.24\n", stderr=""))
    monkeypatch.setattr(rdp, "root_run", lambda *args: pytest.fail("host modified for unsupported xrdp"))
    with pytest.raises(ValueError, match="xrdp >= 0.10"):
        rdp.preflight(tmp_path)


def test_unattended_desktop_masks_lockers_and_keeps_user_override_backup(tmp_path, monkeypatch):
    from hermes_runtime.standalone import rdp

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    folder = tmp_path / "autostart"
    folder.mkdir()
    original = "[Desktop Entry]\nType=Application\nExec=light-locker\nName=My locker\n"
    path = folder / "light-locker.desktop"
    path.write_text(original)
    user = SimpleNamespace(pw_dir=str(tmp_path))
    rdp.prepare_desktop(user)
    rdp.prepare_desktop(user)
    assert path.with_suffix(".desktop.hermes-before-rdp").read_text() == original
    entry = configparser.ConfigParser()
    entry.read_string(path.read_text())
    assert entry["Desktop Entry"].getboolean("Hidden")


@pytest.mark.platforms("posix")
def test_rdp_start_reuses_the_running_session_without_spawning_another(tmp_path, monkeypatch):
    from hermes_runtime.standalone import rdp_session

    monkeypatch.setattr(rdp_session.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    env = {"DISPLAY": ":10", "XDG_CURRENT_DESKTOP": "XFCE", "XRDP_SESSION": "142"}
    monkeypatch.setattr(rdp_session, "xfce_sessions", lambda: [env])
    monkeypatch.setattr(rdp_session, "wait_for_desktop", lambda home: env)
    monkeypatch.setattr(rdp_session.subprocess, "run", lambda *args, **kwargs: pytest.fail("second desktop launched"))
    assert rdp_session.start(tmp_path)["reused"]


@pytest.mark.platforms("posix")
def test_rdp_start_refuses_to_create_a_second_desktop_beside_a_console(tmp_path, monkeypatch):
    from hermes_runtime.standalone import rdp_session

    monkeypatch.setattr(rdp_session.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(rdp_session, "xfce_sessions", lambda: [{"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "XFCE"}])
    with pytest.raises(ValueError, match="console"):
        rdp_session.start(tmp_path)
