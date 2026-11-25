"""Panel subscriptions + same-process input, using only fixture identity and stores."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from threading import RLock

import pytest

from hermes_runtime.standalone import agy_runtime
from hermes_runtime.standalone.panel_agy_login import Attempt, GeminiSubscriptions, Readback
from hermes_runtime.standalone.panel_subscriptions import Subscriptions
from providers.agy_policy import AgyError, MODEL, PROVIDER
from providers.agy_process import communicate
from tests.agent.agy_fixture import spawn
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("posix")
VIEWER = "a" * 43
FIXTURE_URL = ("https://accounts.google.com/o/oauth2/v2/auth?redirect_uri=https%3A%2F%2Fcodeassist.google.com%2Fauthcode"
               "&response_type=code&state=fixture-state&code_challenge=inert-challenge&code_challenge_method=S256")


class FixtureAuthority:
    """Synthetic store, subject readback and withdrawal; not Google or a keyring."""
    def __init__(self, home):
        self.home, self.mode, self.mismatch = home, "login", None
        self.lock, self.children, self.pending = RLock(), [], None
        self.binding = home.parent / (home.name + "-fixture-store.json")

    def active(self, home):
        assert home == self.home
        if self.binding.is_file():
            return json.loads(self.binding.read_text())["generation"]
        return None

    def stage(self, home):
        assert home == self.home
        self.pending = Readback(f"{home.name}-{len(self.children)}", f"{home.name}-fixture-store", "policy-fixture", home.name, 0)
        return self.pending

    def spawn(self, home, generation, op, argv):
        assert home == self.home
        if op == "login":
            assert argv == agy_runtime.LOGIN_COMMAND and generation == self.pending.generation
            child = spawn(home, self.mode, FIXTURE_URL, login=True)
        else:
            assert generation in {self.active(home), self.pending.generation if self.pending else None}
            assert argv[-2] == "-p"
            if self.mode == "expired-fixture":
                raise AgyError("auth_expired")
            if op == "inference":
                request = json.loads(argv[-1])
                assert request["version"] == 1
            # The test child returns a fixed answer and does not need the full managed system prompt.
            child = spawn(home, "ok", '{"type":"final","answer":"fixed fixture reply"}')
        self.children.append((op, generation, child))
        return child

    def verify(self, home, stage, cancelled):
        child = self.spawn(home, stage.generation, "probe", agy_runtime.print_command("fixed inert fixture probe"))
        rc, _ = communicate(child, lambda _: None, timeout=3, cancelled=cancelled)
        assert rc == 0
        proof = replace(stage, pid=child.pid)
        return replace(proof, **{self.mismatch: "foreign"}) if self.mismatch else proof

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
    with pytest.raises(AgyError) as expired:
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


@pytest.mark.parametrize("mismatch", ["subject", "namespace", "policy", "generation", "invalid-grant"])
def test_wrong_fixture_readback_or_invalid_grant_cannot_replace_binding(tmp_path, monkeypatch, mismatch):
    a, _, store, _ = setup(tmp_path, monkeypatch)
    assert submit(a, begin(a))["status"] == "approved"
    previous = store.binding.read_bytes()
    if mismatch == "invalid-grant":
        store.mode = mismatch
    else:
        store.mismatch = mismatch
    result = submit(a, begin(a))
    assert result["status"] == ("error_invalid_code" if mismatch == "invalid-grant" else "error")
    assert store.binding.read_bytes() == previous and store.pending is None


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


@pytest.mark.parametrize("url, expected", [(FIXTURE_URL, True),
    (FIXTURE_URL.replace("/v2/auth", "/auth"), False),
    (FIXTURE_URL.replace("%2Fauthcode", "%2Ffixture-other"), False),
    (FIXTURE_URL + "&state=duplicate", False)])
def test_production_login_url_validation_on_synthetic_shape(url, expected):
    from hermes_runtime.standalone.panel_agy_login import login_url
    assert login_url(url) is expected
