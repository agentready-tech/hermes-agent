"""Run the product Antigravity adapter against deterministic POSIX children only."""
from importlib import import_module
from types import SimpleNamespace

import pytest

from hermes_runtime.standalone import agy_runtime
from providers.agy_policy import AgyError, BINARY, MODEL, PROVIDER, SETTINGS
from providers.agy_process import communicate
from tests.agent.agy_fixture import spawn

pytestmark = pytest.mark.platforms("posix")

AGY = import_module("plugins.model-providers.gemini-antigravity")
SCHEMA = {"type": "object", "properties": {"text": {"type": "string", "maxLength": 64}},
          "required": ["text"], "additionalProperties": False}
TOOLS = [{"type": "function", "function": {"name": "fixture_echo", "parameters": SCHEMA}}]
MESSAGES = [{"role": "user", "content": "echo ☃"}]


class Authority:
    def __init__(self, home, steps):
        self.home, self.steps = home, iter(steps)
        self.processes, self.withdrawals = [], []

    def active(self, home):
        return "fixture-generation" if home == self.home and not self.withdrawals else None

    def spawn(self, home, generation, operation, argv):
        assert self.active(home) == generation == "fixture-generation" and operation == "inference"
        assert argv[:-1] == (BINARY, "--model", MODEL, "--output-format", "stream-json",
                            "--disable-slash-commands", "--settings", SETTINGS, "--log-file", "/dev/null",
                            "--print-timeout", "120s", "-p")
        mode, response = next(self.steps)
        child = spawn(home, mode, response, argv[-1])
        self.processes.append(child)
        return child

    def withdraw(self, home, generation, reason):
        self.withdrawals.append((home, generation, reason))


def client(tmp_path, monkeypatch, *steps):
    authority = Authority(tmp_path, steps)
    monkeypatch.setattr(agy_runtime, "admit", lambda *_: authority)
    return authority, agy_runtime.profile_for(tmp_path).create_client(cancelled=lambda: False)


def test_one_complete_chunk_only_after_process_eof(tmp_path, monkeypatch):
    authority, provider = client(tmp_path, monkeypatch, ("ok", '{"type":"final","answer":"Ответ ☃ "}'))
    chunks = list(provider.create(model=MODEL, messages=MESSAGES, stream=True))
    assert len(chunks) == 1 and chunks[0].choices[0].delta.content == "Ответ ☃ "
    proc, = authority.processes
    assert proc.returncode == 0 and proc.stdout.closed and proc.stderr.closed


@pytest.mark.parametrize(("mode", "code"), [("repeat", "protocol"), ("partial", "protocol"), ("large", "protocol"),
    ("native", "containment"), ("error", "transport"), ("nonzero", "transport"), ("stderr", "transport"),
    ("out-of-order", "protocol"), ("outer-dupe", "protocol"), ("identity", "protocol"), ("no-final", "protocol"),
    ("soft-deny", "containment")])
def test_rejects_ambiguous_or_unsafe_subprocess(tmp_path, monkeypatch, mode, code):
    authority, provider = client(tmp_path, monkeypatch, (mode, '{"type":"final","answer":"unpublished"}'))
    with pytest.raises(AgyError) as failure:
        provider.create(model=MODEL, messages=MESSAGES)
    assert failure.value.code == code and "unpublished" not in str(failure.value)
    assert len(authority.processes) == 1 and authority.processes[0].returncode is not None
    if code == "containment":
        assert authority.withdrawals == [(tmp_path, "fixture-generation", code)]
        with pytest.raises(AgyError, match="Connect"):
            provider.create(model=MODEL, messages=MESSAGES)


@pytest.mark.parametrize("response", ['{"type":"final","answer":"x","answer":"y"}',
    '{"type":"final","answer":"x"} trailing', '{"type":"final","answer":"\\ud800"}',
    '{"type":"tool_call","id":"step","name":"unknown","arguments":{"text":"x"}}',
    '{"type":"tool_call","id":"step","name":"fixture_echo","arguments":{"text":5}}',
    '{"type":"tool_call","id":"step","name":"fixture_echo","arguments":{"text":1e999}}',
    '{"type":"tool_call","id":"step","name":"fixture_echo","arguments":{"text":"x","extra":true}}',
    '{"type":"tool_call","id":"call_1","name":"fixture_echo","arguments":{"text":"x"}}'])
def test_response_shape_and_same_catalog_schema(tmp_path, monkeypatch, response):
    _, provider = client(tmp_path, monkeypatch, ("ok", response))
    with pytest.raises(AgyError) as failure:
        provider.create(model=MODEL, messages=MESSAGES, tools=TOOLS)
    assert failure.value.code == "protocol"


def test_reject_input_without_spawn_and_do_not_offer_remote_ref(tmp_path, monkeypatch):
    authority, provider = client(tmp_path, monkeypatch)
    for kwargs in ({"response_format": {}}, {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
                   {"messages": [{"role": "user", "content": "x" * (96 * 1024)}]}):
        with pytest.raises(AgyError):
            provider.create(**({"model": MODEL, "messages": MESSAGES} | kwargs))
    assert authority.processes == []
    prompt, validators = AGY.prepare(MESSAGES, [{"type": "function", "function": {"name": "fixture_echo",
                                       "parameters": {"$ref": "https://fixture.invalid/schema"}}}])
    assert not validators and "fixture_echo" not in prompt


def test_supervisor_bounded_end_and_descriptor_cleanup(tmp_path):
    proc = spawn(tmp_path, "tree")
    with pytest.raises(AgyError) as timeout:
        communicate(proc, lambda _: None, timeout=0.3, cancelled=lambda: False)
    assert timeout.value.code == "transport"
    assert proc.returncode is not None and proc.stdout.closed and proc.stderr.closed
    proc = spawn(tmp_path, "tree")
    with pytest.raises(AgyError) as cancel:
        communicate(proc, lambda _: None, timeout=125, cancelled=lambda: True)
    assert cancel.value.code == "cancelled"
    assert proc.returncode is not None and proc.stdout.closed and proc.stderr.closed


def test_production_not_discovered_and_exits_before_other_routes(tmp_path, monkeypatch):
    from agent.agent_runtime_helpers import create_openai_client
    from agent.auxiliary_client import resolve_provider_client
    from hermes_cli import runtime_provider as router
    from providers import get_provider_profile, list_providers
    from tui_gateway.server import _resolve_runtime_with_fallback

    def forbidden(*_, **__):
        pytest.fail("the reserved route entered the HTTP/fallback ladder")

    monkeypatch.setattr(router, "_ladder_rungs", forbidden)
    assert get_provider_profile(PROVIDER) is None
    assert PROVIDER not in {p.name for p in list_providers()}
    cfg = {"fallback_providers": [{"provider": "custom", "model": "fixture"}]}
    for operation in (lambda: router.resolve_runtime_with_fallback(cfg, requested=PROVIDER, explicit_base_url="https://fixture.invalid", explicit_api_key="fixture"),
                      lambda: _resolve_runtime_with_fallback({"requested": PROVIDER}),
                      lambda: create_openai_client(SimpleNamespace(provider=PROVIDER), {}, reason="fixture", shared=False)):
        with pytest.raises(AgyError) as failure:
            operation()
        assert failure.value.code == "unavailable"
    assert resolve_provider_client(PROVIDER, explicit_base_url="https://fixture.invalid") == (None, None)


def test_fixed_launch_environment_is_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "ambient-fixture")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "ambient-fixture")
    child = agy_runtime.child_environment(tmp_path)
    assert set(child) == {"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR", "LANG"}
    assert all(str(tmp_path) in value for key, value in child.items() if key != "LANG")
    pointer = tmp_path / "alias"
    pointer.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AgyError, match="unavailable"):
        agy_runtime.child_environment(pointer)
