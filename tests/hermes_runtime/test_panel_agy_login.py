"""Panel subscriptions + same-process input, using only fixture identity and stores."""

import asyncio
import json
from pathlib import Path
from threading import Event, RLock, Thread

import pytest

from hermes_runtime.standalone import agy_runtime
from hermes_runtime.standalone.agy_runtime import StagedStore
from hermes_runtime.standalone.panel_agy_login import Attempt, GeminiSubscriptions
from hermes_runtime.standalone.panel_subscriptions import Subscriptions
from providers.agy_policy import AgyError, MODEL, PROVIDER
from tests.agent.agy_fixture import spawn, spawn_acp
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("posix")
VIEWER = "a" * 43
FIXTURE_URL = ("https://accounts.google.com/o/oauth2/v2/auth?redirect_uri=https%3A%2F%2Fcodeassist.google.com%2Fauthcode"
               "&response_type=code&state=fixture-state&code_challenge=inert-challenge&code_challenge_method=S256")
ANTIGRAVITY_FIXTURE_URL = (
    "https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code&state=fixture-state&code_challenge=inert-challenge&code_challenge_method=S256"
)


class FixtureAuthority:
    """Synthetic per-generation backing and withdrawal; never Google or a keyring."""
    def __init__(self, home):
        self.home, self.mode, self.probe_mode = home, "login", "ok"
        self.mismatch = False
        self.lock, self.children, self.pending = RLock(), [], None
        self.binding = home.parent / (home.name + "-fixture-store.json")

    def active(self, home):
        assert home == self.home
        if self.binding.is_file():
            return json.loads(self.binding.read_text())["generation"]
        return None

    def stage(self, home):
        assert home == self.home
        generation = f"{home.name}-{len(self.children)}"
        self.pending = StagedStore(generation, home, "a")
        self.staged_root(self.pending).mkdir()
        return self.pending

    def staged_root(self, stage):
        return self.home.parent / f"{self.home.name}-fixture-store-{stage.generation}"

    def models(self, home, generation):
        assert home == self.home and generation == self.active(home)
        return ((MODEL, MODEL),)

    def spawn_staged(self, home, stage, op, request):
        assert home == self.home and op in {"login", "probe"}
        if stage != self.pending or (op == "probe" and self.mismatch):
            raise AgyError("unavailable")  # Refuse before starting even the inert child.
        root = self.staged_root(stage)
        if op == "login":
            assert request == agy_runtime.LOGIN_COMMAND
            mode = "store-login" if self.mode == "login" else self.mode
            child = spawn(root, mode, FIXTURE_URL, login=True)
        else:
            assert request is None
            probe = '{"type":"probe"}'
            response = '{"type":"final","answer":"AGE11_READY"}'
            mode = self.probe_mode
            if mode == "wrong-answer":
                response = '{"type":"final","answer":"unrelated fixture"}'
                mode = "ok"
            child = spawn_acp(root, [(mode, response)], probe, require_store=True)
        self.children.append((op, stage.generation, child))
        return child

    def spawn(self, home, generation, op, request):
        assert home == self.home and op == "inference" and generation == self.active(home)
        assert json.loads(request)["model"]
        if self.mode == "expired-fixture":
            raise AgyError("auth_expired")
        child = spawn_acp(home, [("ok", '{"type":"final","answer":"fixed fixture reply"}')], request)
        self.children.append((op, generation, child))
        return child

    def promote(self, home, stage):
        assert home == self.home
        with self.lock:
            if self.pending != stage:
                return False
            staged = self.binding.with_suffix(".tmp")
            staged.write_text(json.dumps({"generation": stage.generation}))
            staged.chmod(0o600)
            staged.replace(self.binding)
            self.pending = None
            return True

    def discard(self, home, stage):
        assert home == self.home
        if self.pending == stage:
            self.pending = None

    def withdraw(self, home, generation, _reason):
        with self.lock:
            if self.active(home) == generation:
                self.binding.unlink()


class Auth:
    gateway_owned = True

    def authenticated_request(self, *_args, **_kwargs):
        return {"providers": []}


def setup(tmp_path, monkeypatch):
    services = []
    fixtures = {}
    for key in ("A", "B"):
        home = tmp_path / key
        home.mkdir()
        route = {"provider": "custom", "default": "managed-fixture", "key_env": "AGENTSEE_INFERENCE_KEY",
                 "base_url": f"https://panel.fixture.invalid/app/api/agents/{key}/inference/v1"}
        (home / "config.yaml").write_text(json.dumps({"model": route,
            "autonomous": {"panel": {"url": "https://panel.fixture.invalid", "environment_id": key}}}))
        fixtures[home] = FixtureAuthority(home)
        def attempt(*args):
            return Attempt(*args, timeout=3)
        service = Subscriptions(home, Auth(), gemini=GeminiSubscriptions(home, attempt_factory=attempt))
        monkeypatch.setattr(service, "_claude", dict)
        services.append(service)
    def admit(home, _op):
        authority = fixtures.get(home)
        if authority is None or Path(home).resolve() != home:
            raise AgyError("unavailable")
        return authority
    monkeypatch.setattr(agy_runtime, "admit", admit)
    return (*services, *fixtures.values())


def rpc(value, method, viewer=VIEWER, **params):
    return asyncio.run(value.handle(f"subscriptions.{method}", {"provider": "gemini", **params}, viewer=viewer))


def begin(value):
    sid = rpc(value, "login.start")["sessionId"]
    assert wait_until(lambda: rpc(value, "login.poll", sessionId=sid).get("url") == FIXTURE_URL, timeout=2)
    return sid


def submit(value, sid):
    assert rpc(value, "login.code", sessionId=sid, code="fixture-only") == {"submitted": True}
    assert value.gemini.attempt.done.wait(3)
    return rpc(value, "login.poll", sessionId=sid)


def test_two_fixture_owners_login_same_pipe_then_probe_expire_and_disconnect(tmp_path, monkeypatch, caplog):
    a, b, store_a, store_b = setup(tmp_path, monkeypatch)
    sid = begin(a)
    with pytest.raises(AgyError):
        rpc(a, "login.code", "b" * 43, sessionId=sid, code="fixture-only")
    with pytest.raises(AgyError):
        rpc(b, "login.poll", sessionId=sid)
    with pytest.raises(AgyError):
        rpc(a, "login.code", sessionId=sid, code="fixture-only\n")
    assert not store_a.active(a.home) and not store_b.active(b.home)
    result = submit(a, sid)
    assert result["status"] == "approved" and "url" not in result
    with pytest.raises(AgyError):
        rpc(a, "login.code", sessionId=sid, code="fixture-only")
    [(login, gen_a, login_child), (probe, gen_probe, probe_child)] = store_a.children
    assert (login, probe, gen_a == gen_probe) == ("login", "probe", True)
    assert login_child.pid != probe_child.pid and login_child.stdin.closed and login_child.returncode == 0
    assert store_a.active(a.home) == gen_a and store_b.active(b.home) is None
    assert not b.gemini.card()["connected"] and a.gemini.card()["connected"]
    assert rpc(a, "models")["models"][0]["id"] == MODEL
    selected = rpc(a, "select", model=MODEL, managedFallback=False)
    assert selected["deferred"] and a._config()["model"] == {"provider": PROVIDER, "default": MODEL}
    assert a._config()["fallback_providers"] == []
    assert submit(b, begin(b))["status"] == "approved"
    old_b = store_b.binding.read_bytes()
    store_a.mode = "expired-fixture"
    client = agy_runtime.profile_for(a.home).create_client(cancelled=lambda: False)
    from agent.agy_turn import acp_turn_scope

    with acp_turn_scope(), pytest.raises(AgyError) as expired:
        client.create(model=MODEL, messages=[{"role": "user", "content": "fixed fixture"}])
    assert expired.value.code == "auth_expired" and not a.gemini.card()["connected"]
    assert store_b.binding.read_bytes() == old_b
    with pytest.raises(ValueError):
        rpc(a, "disconnect")
    asyncio.run(a.handle("subscriptions.select", {"provider": "managed"}))
    with pytest.raises(ValueError):
        rpc(a, "disconnect")  # Persisting a different route is not native application.
    assert store_b.binding.read_bytes() == old_b
    assert "fixture-only" not in caplog.text and FIXTURE_URL not in caplog.text


def test_panel_lists_and_saves_only_models_from_the_connected_acp_profile(tmp_path, monkeypatch):
    from hermes_runtime.standalone.agy_selection import read_selection
    from hermes_runtime.workspace.transport import profile_scope

    a, b, store_a, store_b = setup(tmp_path, monkeypatch)
    for store in (store_a, store_b):
        store.binding.write_text(json.dumps({"generation": f"{store.home.name}-fixture"}))

    def model_catalog(home, generation):
        assert (home, generation) == (a.home, "A-fixture")
        return (("gemini-4-neon", "Gemini 4 Neon"), (MODEL, MODEL))

    monkeypatch.setattr(store_a, "models", model_catalog, raising=False)
    monkeypatch.setattr(store_b, "models", lambda *_: ((MODEL, MODEL),), raising=False)
    assert rpc(a, "models")["models"] == [{"id": "gemini-4-neon", "label": "Gemini 4 Neon"},
                                         {"id": MODEL, "label": MODEL}]
    before = b._config()
    with pytest.raises(AgyError) as unknown:
        rpc(b, "select", model="gemini-4-neon", managedFallback=False)
    assert unknown.value.code == "unsupported_model" and b._config() == before
    assert rpc(a, "select", model="gemini-4-neon", managedFallback=False)["deferred"]
    ticket = read_selection(a.home, a._config())
    assert (ticket.model, ticket.provider, ticket.generation) == ("gemini-4-neon", PROVIDER, "A-fixture")
    with profile_scope(a.home):
        assert agy_runtime.runtime("gemini-4-neon")["model"] == ticket.model


@pytest.mark.parametrize("failure", [
    "foreign-store", "error", "partial", "nonzero", "containment", "wrong-answer", "invalid-grant",
])
def test_failed_staged_probe_or_invalid_grant_cannot_replace_binding(tmp_path, monkeypatch, caplog, failure):
    a, _, store, _ = setup(tmp_path, monkeypatch)
    assert submit(a, begin(a))["status"] == "approved"
    previous = store.binding.read_bytes()
    if failure == "invalid-grant":
        store.mode = failure
    elif failure == "foreign-store":
        store.mismatch = True
    else:
        store.probe_mode = failure
    result = submit(a, begin(a))
    assert result["status"] == ("error_invalid_code" if failure == "invalid-grant" else "error")
    assert store.binding.read_bytes() == previous and store.pending is None
    expected = ["login"] if failure in {"foreign-store", "invalid-grant"} else ["login", "probe"]
    assert [op for op, _, _ in store.children[2:]] == expected
    assert all(child.returncode is not None for _, _, child in store.children)
    phase = "exchange" if failure == "invalid-grant" else "probe"
    assert f"Gemini sign-in failed (phase={phase}," in caplog.text
    assert "fixture-only" not in caplog.text and FIXTURE_URL not in caplog.text
    assert "fixture diagnostic" not in caplog.text and "fixture permission denied" not in caplog.text


def test_cancel_retire_and_production_denial(tmp_path, monkeypatch):
    assert GeminiSubscriptions(tmp_path).card() is None
    with pytest.raises(AgyError, match="unavailable"):
        GeminiSubscriptions(tmp_path).handle("subscriptions.login.start", {}, VIEWER)
    a, _, store, _ = setup(tmp_path, monkeypatch)
    sid = begin(a)
    assert rpc(a, "login.cancel", sessionId=sid) == {"status": "cancelled"}
    with pytest.raises(AgyError):
        rpc(a, "login.poll", sessionId=sid)
    sid = begin(a)
    a.gemini.retire()
    with pytest.raises(AgyError):
        rpc(a, "login.code", sessionId=sid, code="fixture-only")
    assert all(child.returncode is not None and child.stdin.closed for _, _, child in store.children)
    symlink = tmp_path / "restored"
    symlink.symlink_to(a.home, target_is_directory=True)
    assert GeminiSubscriptions(symlink).card() is None


@pytest.mark.parametrize("url, expected", [
    (FIXTURE_URL, True),
    (ANTIGRAVITY_FIXTURE_URL, True),
    (FIXTURE_URL.replace("/v2/auth", "/auth"), False),
    (ANTIGRAVITY_FIXTURE_URL.replace("/o/oauth2/auth", "/o/oauth2/v2/auth"), False),
    (FIXTURE_URL.replace("%2Fauthcode", "%2Ffixture-other"), False),
    (ANTIGRAVITY_FIXTURE_URL.replace("accounts.google.com", "example.invalid"), False),
    (ANTIGRAVITY_FIXTURE_URL.replace("method=S256", "method=plain"), False),
    (FIXTURE_URL + "&state=duplicate", False),
])
def test_production_login_url_validation_on_synthetic_shape(url, expected):
    from hermes_runtime.standalone.panel_agy_login import login_url
    assert login_url(url) is expected


def test_fixed_staged_launch_cannot_bypass_production_admission(tmp_path):
    # A supplied stage and process authority do not replace the real hosted gate.
    store = FixtureAuthority(tmp_path)
    stage = store.stage(tmp_path)
    for operation in ("catalog", "login", "inference", "disconnect"):
        with pytest.raises(AgyError, match="unavailable"):
            agy_runtime.admit(tmp_path, operation)
    with pytest.raises(AgyError, match="unavailable"):
        agy_runtime.start_login(store, tmp_path, stage)
    with pytest.raises(AgyError, match="unavailable"):
        agy_runtime.verify_staged(store, tmp_path, stage, 1, lambda: False)
    assert not store.children and store.active(tmp_path) is None


def test_cancel_after_staged_probe_exit_prevents_promotion(tmp_path, monkeypatch):
    a, b, store, store_b = setup(tmp_path, monkeypatch)
    sid = begin(a)
    attempt = a.gemini.attempt
    real_verify = agy_runtime.verify_staged
    probe_drained = Event()

    def hold_probe(*args, **kwargs):
        real_verify(*args, **kwargs)
        probe_drained.set()
        assert attempt.cancelled.wait(2)

    monkeypatch.setattr(agy_runtime, "verify_staged", hold_probe)
    rpc(a, "login.code", sessionId=sid, code="fixture-only")
    assert probe_drained.wait(3)
    cancel = Thread(target=a.gemini.retire)
    cancel.start()
    cancel.join(5)
    assert not cancel.is_alive() and attempt.done.is_set() and attempt.status == "cancelled"
    assert store.active(a.home) is None and store.pending is None and store_b.active(b.home) is None
    assert not store_b.children and a.gemini.attempt is None
