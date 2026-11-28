"""Inert ACP peer and the real worker broker, over subprocess pipes and MCP HTTP."""

import json
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen


def send(row):
    print(json.dumps({"jsonrpc": "2.0", **row}), flush=True)


def receive(method):
    request = json.loads(sys.stdin.readline())
    assert request["method"] == method
    return request


def agent(model):
    request = receive("initialize")
    send({"id": request["id"], "result": {"protocolVersion": 1,
          "agentCapabilities": {"mcpCapabilities": {"http": True}}}})
    request = receive("session/new")
    options = [{"id": "model", "currentValue": "gemini-4-neon",
                "options": [{"group": "Gemini", "options": [
                    {"value": "gemini-4-neon", "name": "Gemini 4 Neon"},
                    {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
                ]}]}, {"id": "thought_level", "currentValue": "high"}]
    send({"id": request["id"], "result": {"sessionId": "fixture", "configOptions": options}})
    endpoints = request["params"]["mcpServers"]
    if model in {"probe", "models"}:
        assert not endpoints
        if model == "probe":
            prompt = receive("session/prompt")
            text = prompt["params"]["prompt"][0]["text"]
            assert "Reply with exactly AGE11_READY" in text
            send({"method": "session/update", "params": {"sessionId": "fixture", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "AGE11_READY"},
            }}})
            send({"id": prompt["id"], "result": {"stopReason": "end_turn"}})
        return
    if model == "gemini-3.8-flash-low":
        for option, value in zip(options, ("gemini-3.8-flash", "low"), strict=True):
            request = receive("session/set_config_option")
            assert request["params"]["configId"] == option["id"] and request["params"]["value"] == value
            option["currentValue"] = value
            send({"id": request["id"], "result": {"configOptions": options}})
    else:
        assert model == "gemini-4-neon"
    prompt = receive("session/prompt")
    if not endpoints:
        assert "TURNS TO SUMMARIZE" in prompt["params"]["prompt"][0]["text"]
        send({"method": "session/update", "params": {"sessionId": "fixture", "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "## Goal\nKeep the archived checklist marker."},
        }}})
        send({"id": prompt["id"], "result": {"stopReason": "end_turn"}})
        return
    endpoint, = endpoints

    def mcp(method, request_id, params=None, *, stream=False):
        payload = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}).encode()
        headers = {row["name"]: row["value"] for row in endpoint["headers"]}
        request = Request(endpoint["url"], payload, {**headers, "Content-Type": "application/json"})
        with urlopen(request, timeout=8) as response:
            body = response.read().decode()
        if stream:
            body = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")][-1]
        return json.loads(body)

    send({"id": 100, "method": "fs/read_text_file", "params": {"sessionId": "fixture", "path": "/fixture"}})
    assert json.loads(sys.stdin.readline())["error"]["code"] == -32601
    send({"id": 101, "method": "session/request_permission", "params": {
        "sessionId": "fixture", "toolCall": {"name": "read_file", "title": "Run mcp_hermes_fixture_echo"},
        "options": [{"kind": "allow_once", "optionId": "fixture-allow"}],
    }})
    assert json.loads(sys.stdin.readline())["result"]["outcome"]["outcome"] == "cancelled"
    assert mcp("tools/list", 1)["result"]["tools"][0]["name"] == "fixture_echo"
    first = "first"
    for request_id, value in ((2, first), (3, "second")):
        send({"id": 100 + request_id, "method": "session/request_permission", "params": {
            "sessionId": "fixture", "toolCall": {"name": "mcp_hermes_fixture_echo", "title": "Run mcp_hermes_fixture_echo"},
            "options": [{"kind": "allow_once", "optionId": "fixture-allow"}],
        }})
        assert json.loads(sys.stdin.readline())["result"]["outcome"]["optionId"] == "fixture-allow"
        args = {"name": "fixture_echo", "arguments": {"text": value}, "_meta": {"progressToken": request_id}}
        result = mcp("tools/call", request_id, args, stream=True)["result"]["content"]
        assert json.loads(result[0]["text"]) == {"text": value}
        if value == first:
            assert "поправка пользователя" in result[1]["text"]
            assert mcp("tools/call", request_id, args)["error"]["code"] == -32602
    send({"method": "session/update", "params": {"sessionId": "fixture", "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Проверено ACP ☃"},
    }}})
    send({"id": prompt["id"], "result": {"stopReason": "end_turn"}})


def worker(workdir, model):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from hermes_runtime.standalone.agy_acp_worker import run
    from hermes_runtime.standalone.agy_slot import read_command

    incoming = sys.stdin.fileno()
    _, payload = read_command(incoming)
    child = subprocess.Popen([sys.executable, "-I", __file__, "agent", model], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    return run(child, incoming, payload, Path(workdir))


if __name__ == "__main__":
    raise SystemExit(agent(sys.argv[2]) if sys.argv[1] == "agent" else worker(*sys.argv[1:]))
