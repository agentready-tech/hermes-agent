"""Panel subscription catalogs refresh only the owning connected provider's native cache."""

import base64
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_runtime.standalone.panel_subscriptions import PROVIDERS, Subscriptions, SubscriptionError


def token_for(account):
    claims = {"sub": "fixture-subject", "https://api.openai.com/auth": {
        "chatgpt_account_id": account, "chatgpt_data_residency": "eu"}}
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.fixture-signature"


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    from hermes_cli import auth
    from hermes_constants import get_hermes_home
    from agent.secret_scope import current_secret_scope_home, get_secret
    import httpx

    home = tmp_path / "customer"
    home.mkdir()
    (home / ".env").write_text("CATALOG_SCOPE_FIXTURE=own-profile\n")
    (home / "config.yaml").write_text(json.dumps({
        "model": {"provider": "custom", "default": "managed-model", "key_env": "AGENTSEE_INFERENCE_KEY",
                  "base_url": "https://panel.invalid/app/api/agents/env_fixture/inference/v1"},
        "autonomous": {"panel": {"url": "https://panel.invalid", "environment_id": "env_fixture"}},
    }))
    state = SimpleNamespace(account="account-a", connected=True, available=True, offline=False,
                            slugs=["codex-fixture-future"], calls=[], credentials=[])

    class Auth:
        def authenticated_request(self, path, payload=None, *, method=None):
            if path == "/api/providers/oauth":
                return {"providers": [{"id": "openai-codex", "status": {"logged_in": state.connected}}]}
            # The old inventory path deliberately represents a fresh but outdated row.
            if path == "/api/model/options":
                return {"providers": [{"slug": "openai-codex", "models": ["codex-fixture-old"]}]}
            pytest.fail(f"Unexpected native request: {path}")

    value = Subscriptions(home, Auth())
    monkeypatch.setattr(value, "_claude", lambda: {"available": state.available, "logged_in": state.connected})
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex"))

    def credentials(**options):
        assert options == {"read_only": True}
        assert get_hermes_home() == home
        assert current_secret_scope_home() == str(home)
        assert get_secret("CATALOG_SCOPE_FIXTURE") == "own-profile"
        state.credentials.append(copy.deepcopy(options))
        return {"api_key": token_for(state.account), "base_url": "https://chatgpt.com/backend-api/codex"}

    def get(url, *, headers, timeout):
        assert timeout == 10
        assert urlsplit(url).path == "/backend-api/codex/models"
        version = parse_qs(urlsplit(url).query)["client_version"]
        assert version == ["99.0.0"] or (state.offline and version == ["0.0.0"])
        assert headers["Authorization"] == "Bearer " + token_for(state.account)
        assert headers["ChatGPT-Account-ID"] == state.account
        assert headers["x-openai-internal-codex-residency"] == "eu"
        state.calls.append(url)
        if state.offline:
            raise httpx.ConnectError("synthetic offline")
        return SimpleNamespace(status_code=200, json=lambda: {"models": [
            *[{"slug": slug, "priority": i} for i, slug in enumerate(state.slugs)],
            {"slug": "codex-fixture-hidden", "visibility": "hidden"},
        ]})

    monkeypatch.setattr(auth, "resolve_codex_runtime_credentials", credentials)
    monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda *_args: False)
    monkeypatch.setattr(httpx, "get", get)
    return value, home, state


def ids(result):
    return [row["id"] for row in result["models"]]


def test_connected_catalog_refreshes_fresh_cache_with_unknown_provider_ids(catalog):
    value, home, state = catalog
    assert ids(value.models("openai")) == ["codex-fixture-future"]
    cache_path = home / "provider_models_cache.json"
    cache = json.loads(cache_path.read_text())
    cache["untouched-provider"] = {"fp": "other-account", "at": 1, "models": ["other-model"]}
    cache_path.write_text(json.dumps(cache))
    state.slugs = ["codex-fixture-next-release", "codex-fixture-future"]
    assert ids(value.models("openai")) == state.slugs
    saved = json.loads(cache_path.read_text())
    assert saved["openai-codex"]["models"] == state.slugs
    assert saved["untouched-provider"] == cache["untouched-provider"]
    assert len(state.calls) == 2, "Even a fresh cache must discover additions on this explicit load"
    assert state.credentials, "Discovery must use the existing read-only account resolver"


def test_failed_discovery_serves_last_verified_catalog_without_overwriting_it(catalog):
    value, home, state = catalog
    value.models("openai")
    before = (home / "provider_models_cache.json").read_bytes()
    state.offline = True
    assert ids(value.models("openai")) == ["codex-fixture-future"]
    assert (home / "provider_models_cache.json").read_bytes() == before


def test_account_change_cannot_reuse_another_accounts_verified_catalog(catalog):
    value, _home, state = catalog
    value.models("openai")
    state.account = "account-b"
    state.offline = True
    fallback = ids(value.models("openai"))
    assert fallback
    assert "codex-fixture-future" not in fallback


@pytest.mark.parametrize("provider,available,connected", [("openai", True, False), ("claude", False, True), ("claude", True, False)])
def test_catalog_does_not_probe_unavailable_or_disconnected_subscriptions(catalog, monkeypatch, provider, available, connected):
    from hermes_cli import models
    value, _home, state = catalog
    state.available, state.connected = available, connected
    monkeypatch.setattr(models, "cached_provider_model_ids", lambda *_args, **_kwargs: pytest.fail("Forbidden discovery"))
    with pytest.raises(SubscriptionError):
        value.models(provider)
    assert not state.calls


@pytest.mark.parametrize("provider", ["openai", "claude"])
def test_native_catalog_load_is_targeted_and_restores_caller_profile(catalog, monkeypatch, provider):
    from agent.secret_scope import current_secret_scope_home, get_secret
    from hermes_cli import models
    from hermes_constants import get_hermes_home
    value, home, _state = catalog
    previous_home, previous_secrets = get_hermes_home(), current_secret_scope_home()
    calls = []

    def cached(slug, **options):
        assert get_hermes_home() == home
        assert get_secret("CATALOG_SCOPE_FIXTURE") == "own-profile"
        calls.append((slug, options))
        return ["provider-fixture-future"]

    monkeypatch.setattr(models, "cached_provider_model_ids", cached)
    assert ids(value.models(provider)) == ["provider-fixture-future"]
    assert calls == [(PROVIDERS[provider], {"force_refresh": True})]
    assert (get_hermes_home(), current_secret_scope_home()) == (previous_home, previous_secrets)
