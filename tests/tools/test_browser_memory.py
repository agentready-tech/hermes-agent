import json
from types import SimpleNamespace

from tools.bot_desktop import browser_memory as memory


def test_policy_install_preserves_other_files_and_is_idempotent(tmp_path):
    root = tmp_path / memory.ROOTS[0]
    root.mkdir(parents=True)
    other = root / "admin.json"
    other.write_text('{"Existing":true}')
    memory.install(tmp_path)
    policy = root / "agentsee-memory.json"
    before = policy.stat().st_mtime_ns
    assert json.loads(policy.read_text()) == memory.POLICY
    memory.install(tmp_path)
    assert policy.stat().st_mtime_ns == before
    assert other.read_text() == '{"Existing":true}'
    assert len(list(tmp_path.rglob("agentsee-memory.json"))) == 3


def test_sandbox_configure_reapplies_policy_with_exec_default_and_no_browser_restart(monkeypatch, tmp_path):
    from tools.environments import streams
    calls = []
    def run(env, argv, **kw):
        calls.append((argv, kw))
        ns = {}
        exec(argv[2].rsplit("install()", 1)[0], ns)
        ns["install"](tmp_path)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(streams, "run_in", run)
    assert memory.configure(object())
    assert calls[0][0][:2] == ["python3", "-c"]
    assert "user" not in calls[0][1]


def test_read_only_sandbox_keeps_desktop_available(monkeypatch):
    from tools.environments import streams
    def refused(*args, **kwargs):
        raise RuntimeError("exec unavailable")
    monkeypatch.setattr(streams, "run_in", refused)
    assert memory.configure(object()) is False
