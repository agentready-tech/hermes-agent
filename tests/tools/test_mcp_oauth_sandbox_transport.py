"""OAuth discovery and device grants use the same sandbox network as MCP."""
from __future__ import annotations

from urllib.parse import parse_qs

import pytest

pytest.importorskip("mcp.client.auth.oauth2", reason="MCP OAuth SDK required")

RESOURCE = "https://resource.example/mcp"
ISSUER = "https://issuer.example"


def _sandbox_http(monkeypatch, *, available=True):
    from tools.environments import network_transport
    from tools.mcp_tool import sdk_httpx

    sdk = sdk_httpx()
    calls, wire, clients = [], [], []

    def respond(request):
        wire.append(request)
        path = request.url.path
        if path == "/mcp":
            return sdk.Response(401, headers={"www-authenticate": "Bearer"})
        if "oauth-protected-resource" in path:
            return sdk.Response(200, json={"resource": RESOURCE, "authorization_servers": [ISSUER]})
        if "oauth-authorization-server" in path:
            return sdk.Response(200, json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "registration_endpoint": ISSUER + "/register",
                "device_authorization_endpoint": ISSUER + "/device",
                "response_types_supported": ["code"],
            })
        if path == "/register":
            return sdk.Response(201, json={"client_id": "fixture-client"})
        if path == "/device":
            return sdk.Response(200, json={
                "device_code": "fixture-device-secret", "user_code": "FIXTURE-CODE",
                "verification_uri": ISSUER + "/verify", "interval": 0.001, "expires_in": 60,
            })
        if path == "/token":
            data = parse_qs(request.content.decode())
            assert data["device_code"] == ["fixture-device-secret"]
            assert data["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
            assert data["resource"] == [RESOURCE]
            return sdk.Response(200, json={
                "access_token": "fixture-access", "refresh_token": "fixture-refresh",
                "token_type": "Bearer", "expires_in": 3600,
            })
        raise AssertionError(f"Unexpected fixture request: {request.method} {path}")

    transport = sdk.MockTransport(respond)

    def own_sandbox(httpx_module, *, async_mode=True, **kwargs):
        calls.append((httpx_module, async_mode))
        assert httpx_module is sdk
        assert async_mode is True
        if not available:
            raise RuntimeError("fixture own sandbox unavailable")
        return transport

    real_client = sdk.AsyncClient
    monkeypatch.setenv("ALL_PROXY", "http://host-proxy.invalid:8080")

    def guarded_client(*args, **kwargs):
        # Fail before constructing a client if this path attempts host networking.
        assert kwargs.get("transport") is transport, "OAuth bypassed its sandbox transport"
        assert kwargs.get("trust_env") is False, "OAuth allowed host proxy mounts"
        clients.append(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(network_transport, "http_transport", own_sandbox)
    monkeypatch.setattr(sdk, "AsyncClient", guarded_client)
    return calls, wire, clients


async def _run_oauth(operation, storage):
    if operation == "device":
        from tools.mcp_oauth_device import login_device
        await login_device("sandbox-fixture", RESOURCE, {"cimd": False})
        return

    from mcp.shared.auth import OAuthClientMetadata
    from tools.mcp_oauth_manager import HermesMCPOAuthProvider
    provider = HermesMCPOAuthProvider(
        server_name="sandbox-fixture", server_url=RESOURCE, storage=storage,
        client_metadata=OAuthClientMetadata(redirect_uris=["http://127.0.0.1:8420/callback"]),
    )
    await provider._prefetch_oauth_metadata()
    assert str(provider.context.oauth_metadata.token_endpoint) == ISSUER + "/token"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prefetch", "device"])
async def test_oauth_discovery_and_device_grant_use_sandbox(operation, tmp_path, monkeypatch, capsys):
    from tools.mcp_oauth import HermesTokenStorage
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("sandbox-fixture")
    calls, wire, clients = _sandbox_http(monkeypatch)

    await _run_oauth(operation, storage)

    assert len(calls) == len(clients) == 1
    assert any("oauth-authorization-server" in request.url.path for request in wire)
    assert str(storage.load_oauth_metadata().token_endpoint) == ISSUER + "/token"
    if operation == "device":
        assert [request.url.path for request in wire[-3:]] == ["/register", "/device", "/token"]
        assert clients[0]["follow_redirects"] is False
        assert (await storage.get_tokens()).access_token == "fixture-access"
        assert (await storage.get_client_info()).client_id == "fixture-client"
        assert "fixture-device-secret" not in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prefetch", "device"])
async def test_unavailable_sandbox_stops_oauth_without_host_fallback(operation, tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthToken
    from tools.mcp_oauth import HermesTokenStorage
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    storage = HermesTokenStorage("sandbox-fixture")
    await storage.set_tokens(OAuthToken(access_token="prior-access", token_type="Bearer"))
    previous = storage.snapshot()
    calls, wire, clients = _sandbox_http(monkeypatch, available=False)

    with pytest.raises(RuntimeError, match="own sandbox unavailable"):
        await _run_oauth(operation, storage)

    assert len(calls) == 1
    assert wire == clients == []
    assert storage.snapshot() == previous
