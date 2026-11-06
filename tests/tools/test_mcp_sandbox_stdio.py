"""The real SDK executes arbitrary stdio servers in the selected sandbox, never Hermes."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tools import mcp_sandbox_stdio as sandbox


SERVER = '''import json, os, sys
for line in sys.stdin:
    message=json.loads(line)
    if "id" not in message: continue
    method=message["method"]
    if method=="initialize":
        result={"protocolVersion":message["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}
    elif method=="tools/list":
        result={"tools":[{"name":"where_am_i","description":"Synthetic process metadata","inputSchema":{"type":"object"}}]}
    elif method=="tools/call":
        result={"content":[{"type":"text","text":json.dumps({"marker":os.environ.get("SANDBOX_MARKER"),"cwd":os.getcwd(),"secretPresent":os.environ.get("MCP_FIXTURE_KEY")=="synthetic-only-secret","pid":os.getpid()})}]}
    elif method=="ping": result={}
    else: result={}
    print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
'''


def test_real_sdk_stdio_runs_selected_remote_process_and_secret_uses_only_stdin(tmp_path, monkeypatch):
    from tools import mcp_tool
    server_file = tmp_path / "server.py"
    server_file.write_text(SERVER)
    remote_cwd = tmp_path / "sandbox-workspace"
    remote_cwd.mkdir()
    environment = SimpleNamespace(cwd=str(remote_cwd), namespace="own-target", pod="sandbox-0", container="sandbox")
    monkeypatch.setattr(sandbox, "sandbox_environment", lambda: environment)
    seen = []
    def remote(selected, argv, **kwargs):
        assert selected is environment and argv[:2] == ["python3", "-I"]
        seen.append((argv, kwargs))
        code = "import os; os.environ['SANDBOX_MARKER']='existing-own-sandbox'; exec(" + repr(argv[-1]) + ")"
        return [sys.executable, "-I", "-c", code]
    monkeypatch.setattr(sandbox.streams, "remote_command", remote)
    async def run():
        server = mcp_tool.MCPServerTask("any-unrelated-mcp-name")
        await server.start({"command": sys.executable, "args": ["-u", str(server_file)],
                            "env": {"MCP_FIXTURE_KEY": "synthetic-only-secret"}, "connect_timeout": 5})
        try:
            result = await server.session.call_tool("where_am_i", {})
            return json.loads(result.content[0].text)
        finally:
            await server.shutdown()
    result = asyncio.run(run())
    assert result["marker"] == "existing-own-sandbox"
    assert result["cwd"] == str(remote_cwd) and result["secretPresent"]
    assert result["pid"] != os.getpid()
    assert len(seen) == 1 and "synthetic-only-secret" not in repr(seen)


def test_only_exact_reviewed_custody_modules_can_remain_with_own_vault(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    root = str(Path(sandbox.__file__).resolve().parents[1])
    config = {"command": sys.executable, "args": ["-m", "hermes_runtime.workspace.mcp"],
              "cwd": root, "env": {"HERMES_HOME": str(tmp_path)}}
    token = set_hermes_home_override(tmp_path)
    try:
        assert sandbox.custody_broker(config)
        for changed in ({"args": ["-m", "hermes_runtime.workspace.mcp", "unreviewed"]},
                        {"args": ["-c", "arbitrary host code"]}, {"cwd": str(tmp_path)},
                        {"env": {"PYTHONPATH": str(tmp_path)}}, {"env": {"HERMES_HOME": str(tmp_path / "foreign")}},
                        {"command": str(tmp_path / "python")}):
            assert not sandbox.custody_broker({**config, **changed})
        assert sandbox.custody_broker({**config, "args": ["-m", "tools.connectors.api_mcp", "fixture"], "hermes_api": {"operations": []}})
    finally:
        reset_hermes_home_override(token)


def test_unavailable_remote_stream_refuses_without_a_host_fallback(monkeypatch):
    monkeypatch.setattr(sandbox.streams, "remote_command", lambda *_args, **_kwargs: None)
    with pytest.raises(sandbox.SandboxTransportUnavailable):
        sandbox.remote_parameters(object(), {"command": "no-host-executable"}, {})
