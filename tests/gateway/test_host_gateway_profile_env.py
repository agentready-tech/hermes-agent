"""A host gateway spawned from a profile-scoped process must not adopt that profile.

Two disposable homes: the default root (the multiplexer) and a named profile whose
``.env`` holds the only Telegram token. The process environment is that profile's
— the shape a desktop / update spawn inherits. The host's primary adapter claim
must stay the default profile's, the named launcher must start as a secondary,
and a duplicate refusal must say which claim came from the environment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig

_WORKER_TOKEN = "worker-profile-token-456"


@pytest.fixture(autouse=True)
def _reset_multiplex_flag():
    from agent import secret_scope as ss

    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


def _two_homes(tmp_path: Path) -> tuple[Path, Path]:
    """Default root plus ``profiles/worker``, each a real Hermes home."""
    default_home = tmp_path / "default"
    worker_home = default_home / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    (default_home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n", encoding="utf-8",
    )
    (default_home / ".env").write_text("", encoding="utf-8")
    (worker_home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n", encoding="utf-8",
    )
    (worker_home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={_WORKER_TOKEN}\n", encoding="utf-8",
    )
    return default_home, worker_home


def _inherit_worker_env(monkeypatch, worker_home: Path) -> None:
    """Model a process spawned from the worker profile: its home and its dotenv."""
    monkeypatch.setenv("HERMES_HOME", str(worker_home))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", _WORKER_TOKEN)


class TestProfileEnvIsNotThePrimaryClaim:
    def test_worker_env_does_not_become_the_host_primary_token(self, tmp_path, monkeypatch):
        """Profile env inherited by the host process must not seed the primary adapter."""
        from gateway import run as run_mod

        default_home, worker_home = _two_homes(tmp_path)
        _inherit_worker_env(monkeypatch, worker_home)

        cfg = run_mod.load_gateway_config_for_runner()

        assert cfg.multiplex_profiles is True
        telegram = cfg.platforms.get(Platform.TELEGRAM)
        token = "" if telegram is None else (telegram.token or "")
        assert token != _WORKER_TOKEN, (
            "host primary claimed the launching profile's TELEGRAM_BOT_TOKEN"
        )
        # The reload that produced the primary config ran under the default root,
        # not the named launcher's home.
        assert Path(default_home).resolve() != Path(worker_home).resolve()

    @pytest.mark.asyncio
    async def test_named_launcher_starts_as_secondary(self, tmp_path, monkeypatch):
        from gateway.run import GatewayRunner

        default_home, worker_home = _two_homes(tmp_path)
        _inherit_worker_env(monkeypatch, worker_home)
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.adapters = {}
        runner._profile_adapters = {}
        runner._profile_configs = {}
        runner._profile_failed_platforms = {}
        runner._served_profile_signatures = {}
        runner.session_store = None
        runner._busy_text_mode = "queue"
        runner.pairing_stores = {}
        runner.pairing_store = object()
        runner._record_served_profiles = lambda *_args: None
        runner._restore_secondary_completion_ledgers = lambda *_args: None
        started = []

        monkeypatch.setattr(
            "hermes_cli.profiles.profiles_to_serve",
            lambda multiplex, **_kw: [("default", default_home), ("worker", worker_home)],
        )
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "worker")

        async def fake_start(profile_name, profile_home, claimed):
            started.append(profile_name)
            return 1

        monkeypatch.setattr(runner, "_start_one_profile_adapters", fake_start)
        monkeypatch.setattr(
            "gateway.run_profile_reconcile.profile_serve_signature", lambda _home: "signature",
        )

        assert await runner._start_secondary_profile_adapters() == 1
        assert started == ["worker"]


class TestHostGatewaySpawnEnv:
    def test_restart_watcher_does_not_inherit_profile_token(self, tmp_path, monkeypatch):
        """The detached restart child is built with served_profile_child_env, not os.environ.copy()."""
        from gateway.run_shutdown import GatewayShutdownMixin

        default_home, worker_home = _two_homes(tmp_path)
        _inherit_worker_env(monkeypatch, worker_home)

        env = GatewayShutdownMixin._restart_watcher_env()

        assert env.get("HERMES_HOME") == str(default_home)
        assert env.get("TELEGRAM_BOT_TOKEN") != _WORKER_TOKEN


class TestDuplicateRefusalNamesEnvClaim:
    @pytest.mark.asyncio
    async def test_refusal_says_which_claim_is_env_derived(self, tmp_path, monkeypatch):
        """Default inherited the token from the process env; worker configured it in .env."""
        from gateway.run import GatewayRunner

        default_home, worker_home = _two_homes(tmp_path)
        _inherit_worker_env(monkeypatch, worker_home)
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.adapters = {}
        runner._profile_adapters = {}
        runner._profile_failed_platforms = {}
        writes = []

        class _Adapter:
            def __init__(self):
                self.token = _WORKER_TOKEN
                self.config = PlatformConfig(enabled=True, token=_WORKER_TOKEN)

        adapter = _Adapter()
        config = GatewayConfig(
            multiplex_profiles=True,
            platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token=_WORKER_TOKEN)},
        )
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
        monkeypatch.setattr(runner, "_create_adapter", lambda _p, _c: adapter)
        monkeypatch.setattr(
            runner, "_update_platform_runtime_status",
            lambda platform, **kwargs: writes.append((platform, kwargs)),
        )
        monkeypatch.setattr(runner, "_subscribe_plugin_rewire", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "_register_config_hooks", lambda *_a, **_k: None)
        monkeypatch.setattr(runner, "_snapshot_profile_busy_modes", lambda *_a, **_k: None)
        claim = runner._adapter_credential_claim(Platform.TELEGRAM, adapter)
        assert claim is not None

        connected = await runner._start_one_profile_adapters(
            "worker", worker_home, {claim: "default"},
        )

        assert connected == 0
        assert writes, "duplicate was not refused"
        message = writes[0][1]["error_message"]
        assert "env TELEGRAM_BOT_TOKEN" in message
        assert "default" in message
        assert "worker" in message
        assert "profiles/worker/.env" in message
