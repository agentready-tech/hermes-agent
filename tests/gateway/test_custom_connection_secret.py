"""A custom API setup asks the actual native viewer waiter, not a chat prompt."""

import json
import asyncio
import threading
from types import SimpleNamespace
import pytest


def test_custom_api_uses_native_secret_waiter_without_leaking_answer(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.session_events import SessionEvents
    from gateway.session_secret_prompt import secret_prompt_scope
    from gateway.session_sudo_prompt import resolve
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from hermes_cli.config import save_config
    from tools.connectors import custom, mcp
    from tools.connectors.tool import manage_connections
    from tools import mcp_tool_discovery
    from tools.thread_context import propagate_context_to_thread
    from tests.waiting import wait_until
    home = tmp_path / "home";home.mkdir()
    home_token = set_hermes_home_override(home)
    scope_token = set_secret_scope({}, profile_home=str(home))
    frames = []
    class Peer:
        def write(self, frame):frames.append(frame);return True
        def close(self):pass
    events = SessionEvents();events.fanout.attach(Peer())
    authority = SimpleNamespace(sessions={"agentready_home": SimpleNamespace(event_stream=events)})
    turn = SimpleNamespace(_approval_owner=(authority, "agentready_home", 1),
                           _ctx=SimpleNamespace(source=SimpleNamespace(platform=Platform.LOCAL)))
    monkeypatch.setattr(custom, "available", lambda: True)
    monkeypatch.setattr(mcp_tool_discovery, "register_mcp_servers", lambda *_: [])
    monkeypatch.setattr(mcp, "_registered_tool_names", lambda *_: ["mcp_inventory_list"])
    out = []
    try:
        save_config({"mcp_servers": {}})
        args = {"action": "install", "connectors": [{"name": "inventory", "mcp": True}],
                "api": {"base_url": "https://inventory.example.test", "auth": {"type": "bearer"},
                        "operations": [{"name": "list", "description": "List inventory", "method": "GET", "path": "items",
                                        "input_schema": {"type": "object", "properties": {}}}]}}
        with secret_prompt_scope(turn, SimpleNamespace()):
            worker = threading.Thread(target=propagate_context_to_thread(lambda: out.append(manage_connections(args))))
            worker.start()
            wait_until(lambda: any(f.get("method") == "secret" for f in frames), timeout=5)
            question = next(f for f in frames if f.get("method") == "secret")
            assert "inventory.example.test" in question["params"]["prompt"]
            resolve({"jsonrpc": "2.0", "id": question["id"], "result": {"value": "synthetic-private-api-key"}})
            worker.join(5)
        assert not worker.is_alive()
        assert json.loads(out[0])["targets"][0]["state"] == "connected"
        assert "synthetic-private-api-key" not in json.dumps(frames) + "".join(out)
        assert "synthetic-private-api-key" not in (home / "config.yaml").read_text()
        assert "synthetic-private-api-key" in (home / ".env").read_text()
        assert not any(f.get("method") == "prompt.submit" for f in frames)
    finally:
        reset_secret_scope(scope_token);reset_hermes_home_override(home_token)


@pytest.mark.parametrize("value", ["synthetic-adapter-secret", ""])
def test_messaging_secret_capability_uses_existing_out_of_band_transport(tmp_path, value):
    from gateway.config import Platform
    from gateway.session_secret_prompt import secret_prompt_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.skills_tool_setup import _capture_required_environment_variables
    home = tmp_path / "profile";home.mkdir();token = set_hermes_home_override(home)
    loop = asyncio.new_event_loop();worker = threading.Thread(target=loop.run_forever);worker.start()
    seen = []
    class Adapter:
        async def request_secret(self, source, *, label, site):
            seen.append((source.user_id, label, site));return value
    source = SimpleNamespace(platform=Platform.TELEGRAM, user_id="paired-user")
    turn = SimpleNamespace(_approval_owner=None, _runner=SimpleNamespace(_delivery_adapter_for=lambda _: Adapter()),
                           _ctx=SimpleNamespace(source=source, _loop_for_step=loop, _run_still_current=lambda: True))
    try:
        with secret_prompt_scope(turn, SimpleNamespace()):
            result = _capture_required_environment_variables("API", [{"name": "TEST_API_KEY", "prompt": "API key"}])
        assert bool(result["missing_names"]) is not bool(value)
        assert value not in json.dumps(result) if value else result["setup_skipped"]
        assert seen == [("paired-user", "API key", "TEST_API_KEY")]
        if value:assert value in (home / ".env").read_text()
        else:assert not (home / ".env").exists()
    finally:
        loop.call_soon_threadsafe(loop.stop);worker.join(5);loop.close();reset_hermes_home_override(token)
