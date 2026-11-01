"""Sandbox restart removes runtime scratch while preserving the user's Xfce configuration."""

import shutil
import subprocess

from tools.bot_desktop import sandbox_host


def test_xfce_config_moves_out_of_scratch_and_survives_recreation(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    home = tmp_path / "desktop-home"
    home.mkdir()

    class Sandbox:
        _bd_desktop_home = str(home)

        def get_temp_dir(self):
            return str(scratch)

    env = Sandbox()
    old_config = scratch / "hermes-bot-desktop/default/xdg"
    old_config.mkdir(parents=True)
    (old_config / "user-theme.xml").write_text("original user choice")
    monkeypatch.setattr(sandbox_host, "_user_for", lambda _env: None)
    monkeypatch.setattr(sandbox_host, "stop", lambda *_args: True)
    monkeypatch.setattr(sandbox_host, "missing_binaries", lambda *_args: [])
    monkeypatch.setattr(sandbox_host, "_record", lambda _env, _dir, _profile, live: live)
    invocations = []

    def run_in(_env, argv, **_kwargs):
        invocations.append(argv)
        if argv[:2] == ["bash", "-c"] and argv[2].startswith("set -e;"):
            return subprocess.run(argv, capture_output=True, check=False)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(sandbox_host.streams, "run_in", run_in)
    for expected in ("original user choice", "customized later"):
        responses = iter(({}, {"DISPLAY": ":20"}))
        monkeypatch.setattr(sandbox_host, "_published", lambda *_args: next(responses))
        sandbox_host.start(env, "default", geometry="1280x800")
        persisted = home / ".hermes/bot-desktop/config/default/user-theme.xml"
        assert persisted.read_text() == expected
        assert not (scratch / "hermes-bot-desktop/default/xdg").exists()
        persisted.write_text("customized later")
        shutil.rmtree(scratch)
    assert invocations
