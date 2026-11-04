"""Agent-owned setup uses the real config/secret lifecycle, not sandbox filesystem access."""

import copy
import hashlib
import io
import json
from pathlib import Path

import pytest


def api_definition():
    return {"base_url": "https://api.example.test/v1", "auth": {"type": "bearer"}, "operations": [{
        "name": "lookup", "description": "Look up an item using path.item_id and optional query.",
        "method": "GET", "path": "items/{item_id}", "input_schema": {
            "type": "object", "properties": {"path": {"type": "object", "properties": {
                "item_id": {"type": "string"}}, "required": ["item_id"], "additionalProperties": False},
                "query": {"type": "object"}}, "required": ["path"], "additionalProperties": False}}]}


@pytest.fixture
def setup_runtime(tmp_path, monkeypatch):
    from tools.connectors import custom
    from hermes_cli.config import save_config
    from tools import mcp_tool_discovery
    from tools.connectors import mcp
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    home = tmp_path / "agent";home.mkdir()
    token = set_hermes_home_override(home)
    scope = set_secret_scope({}, profile_home=str(home))
    monkeypatch.setattr(custom, "available", lambda: True)
    save_config({"mcp_servers": {}, "model": {"provider": "keep-this-provider"}})
    registered = {}
    monkeypatch.setattr(mcp_tool_discovery, "register_mcp_servers", lambda servers: registered.update(servers))
    monkeypatch.setattr(mcp, "_registered_tool_names", lambda name: [f"mcp_{name}_lookup"])
    yield home, registered
    reset_secret_scope(scope);reset_hermes_home_override(token)


def call_install(**kwargs):
    from tools.connectors.tool import manage_connections
    return json.loads(manage_connections({"action": "install", "connectors": [{"name": "store", "mcp": True}], **kwargs}))


def test_custom_mcp_installs_without_catalog_or_sandbox_config(setup_runtime, monkeypatch):
    from hermes_cli import mcp_config
    from hermes_cli.config import load_config
    seen = []
    monkeypatch.setattr(mcp_config, "_probe_single_server", lambda name, cfg, **kw: seen.append((name, cfg)))
    result = call_install(server={"url": "https://mcp.example.test/mcp"})
    assert result["targets"][0]["state"] == "connected"
    assert result["targets"][0]["tools"] == ["mcp_store_lookup"]
    assert seen[0][0] == "store"
    assert load_config()["mcp_servers"]["store"]["url"] == "https://mcp.example.test/mcp"
    assert load_config()["model"]["provider"] == "keep-this-provider"
    assert "already exists" in call_install(server={"url": "https://other.example.test/mcp"})["error"]
    assert load_config()["mcp_servers"]["store"]["url"] == "https://mcp.example.test/mcp"


def test_api_secret_form_persists_only_ref_and_adapter_is_callable(setup_runtime):
    from tools.skills_tool import bind_turn_secret_capture
    from hermes_cli.config import save_env_value_secure, load_config
    from tools.connectors.api_mcp import connection, serve
    home, _registered = setup_runtime
    secret = "synthetic-api-key-not-for-history"
    prompts = []
    def capture(name, prompt, metadata):
        prompts.append((name, prompt));save_env_value_secure(name, secret)
        return {"success": True, "stored_as": name, "validated": False, "skipped": False}
    with bind_turn_secret_capture(capture):
        result = call_install(api=api_definition())
    assert result["targets"][0]["state"] == "connected"
    assert result["authenticationVerified"] is False
    assert len(prompts) == 1 and "store" in prompts[0][1]
    assert secret not in json.dumps(result) and secret not in (home / "config.yaml").read_text()
    assert secret in (home / ".env").read_text()
    spec, headers = connection("store")
    assert headers["Authorization"] == "Bearer " + secret
    assert spec["operations"][0]["name"] == "lookup"
    # Actual stdio protocol path loads the persisted connection independently of the model call.
    output = io.StringIO()
    serve("store", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"), output)
    assert json.loads(output.getvalue())["result"]["tools"][0]["name"] == "lookup"
    assert secret not in output.getvalue()
    assert load_config()["model"]["provider"] == "keep-this-provider"
    # A real native MCP client launches the persisted first-party stdio adapter.
    from hermes_cli.mcp_config import _probe_single_server
    from hermes_cli.config import read_raw_config_readonly
    tools = _probe_single_server("store", read_raw_config_readonly()["mcp_servers"]["store"], connect_timeout=10)
    assert tools[0][0] == "lookup"
    from tools.skill_manager_tool import skill_manage
    skill = json.loads(skill_manage("create", "store-lookup", content="---\nname: store-lookup\ndescription: Look up store items through the connected API.\n---\nUse the store connection lookup tool. Pass item identifiers in path.item_id. Never include credentials.\n"))
    assert skill["success"] and not skill.get("staged")
    saved_skill = next((home / "skills").rglob("SKILL.md")).read_text()
    assert "store connection" in saved_skill and secret not in saved_skill


def test_cancelled_secret_does_not_install_or_accept_plaintext_fallback(setup_runtime):
    from hermes_cli.config import load_config
    from tools.skills_tool import bind_turn_secret_capture
    with bind_turn_secret_capture(lambda *_: {"success": True, "skipped": True}):
        result = call_install(api=api_definition())
    assert result["secret_required"] is True
    assert load_config()["mcp_servers"] == {}
    bad = api_definition();bad["auth"]["value"] = "NEVER-ACCEPT-RAW-KEY"
    result = call_install(api=bad)
    assert "protected form" in result["error"] and "NEVER-ACCEPT-RAW-KEY" not in json.dumps(result)


def test_stored_key_does_not_fall_back_to_another_profile_or_process(setup_runtime, monkeypatch):
    from tools.connectors.custom import credential
    from tools.skills_tool import bind_turn_secret_capture
    variable = "HERMES_INTEGRATION_" + hashlib.sha256(b"store").hexdigest()[:12].upper() + "_API_KEY"
    monkeypatch.setenv(variable, "other-profile-key")
    with bind_turn_secret_capture(lambda *_: {"success": True, "skipped": True}):
        headers, missing = credential("store", api_definition())
    assert headers == {} and missing == variable


def test_credentials_require_tls_before_requesting_a_key(setup_runtime):
    from tools.connectors.custom import credential
    from tools.skills_tool import bind_turn_secret_capture
    spec = api_definition();spec["base_url"] = "http://public.example.test"
    with bind_turn_secret_capture(lambda *_: pytest.fail("must not ask for a key for plaintext HTTP")):
        with pytest.raises(ValueError, match="HTTPS"):
            credential("store", spec)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://169.254.169.254/", "http://localhost/", "https://u:p@example.com/mcp", "https://example.com/mcp?key=abc"])
def test_custom_endpoints_do_not_accept_host_metadata_or_embedded_credentials(url):
    from tools.connectors.custom import endpoint
    with pytest.raises(ValueError):endpoint(url)


def test_api_schema_and_credentials_do_not_permit_arbitrary_code_or_destination():
    from tools.connectors.http_api import validate_api, request_parts
    spec = api_definition(); op = spec["operations"][0]
    assert request_parts(spec, op, {"path": {"item_id": "a/b"}})[0] == "https://api.example.test/v1/items/a%2Fb"
    with pytest.raises(ValueError):request_parts(spec, op, {"path": {"item_id": ".."}})
    with pytest.raises(ValueError):request_parts(spec, op, {"path": {"item_id": "x"}, "headers": {"Host": "other"}})
    bad = copy.deepcopy(spec);bad["operations"][0]["path"] = "https://other.test/"
    with pytest.raises(ValueError):validate_api(bad)
    bad = copy.deepcopy(spec);bad["command"] = "sh"
    with pytest.raises(ValueError):validate_api(bad)


def test_api_errors_and_echoed_credentials_are_not_exposed(monkeypatch):
    from tools.connectors import http_api, api_mcp
    class Response:
        code = 401
        def read(self, _):return b'{"echo":"Bearer synthetic-key","message":"unauthorized"}'
        def __enter__(self):return self
        def __exit__(self, *_):return False
    class Opener:
        def open(self, request, timeout):
            assert request.headers["Authorization"] == "Bearer synthetic-key"
            return Response()
    monkeypatch.setattr(http_api, "build_opener", lambda *_: Opener())
    result = api_mcp.dispatch({"method": "tools/call", "params": {"name": "lookup", "arguments": {"path": {"item_id": "x"}}}}, api_definition(), {"Authorization": "Bearer synthetic-key"})
    assert result["isError"] is True
    assert "synthetic-key" not in json.dumps(result) and "REDACTED" in json.dumps(result)


def test_redirects_never_forward_authorization():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from tools.connectors.http_api import execute
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):pass
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/sink":
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            else:
                self.send_response(307)
                self.send_header("Location", "http://localhost:" + str(self.server.server_port) + "/sink")
                self.end_headers()
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True);worker.start()
        try:
            spec = api_definition();spec["base_url"] = "http://127.0.0.1:" + str(server.server_port)
            result = execute(spec, "lookup", {"path": {"item_id": "x"}}, {"Authorization": "Bearer synthetic-key"})
            assert result["status"] == 307
            assert seen == [("/items/x", "Bearer synthetic-key")]
        finally:
            server.shutdown();worker.join(5)
