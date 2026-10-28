"""Code Mode uses real kernels, scoped dispatch and durable steps without live services."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.code_execution_context import CodeToolScope, bind_scope
from tools.code_execution_tool import execute_code
from tools.code_kernel import shutdown_all_kernels

pytest_plugins = ["tests.tools._child_env_fixtures"]


def definition(name):
    return {"type": "function", "function": {"name": name, "description": "Read demo pipeline status",
        "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}}}}


@pytest.fixture(autouse=True)
def kernels():
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def execute(scope, code="", **kwargs):
    with bind_scope(scope):
        return json.loads(execute_code(code, task_id=scope.session_id, **kwargs))


def test_scoped_sdk_chains_and_filters_without_intermediate_context(child_env):
    calls = []
    def invoke(name, args, step_id):
        calls.append((name, args))
        return json.dumps({"id": args["id"], "failed": args["id"] == 2, "large": "x" * 10000})
    scope = CodeToolScope("demo-session", [definition("mcp_demo_pipeline")], invoke)
    result = execute(scope, '''
from hermes_tools import search_tools, describe_tools, call_tool
import json
name = search_tools("pipeline")[0]["name"]
assert describe_tools([name])[name]["parameters"]["type"] == "object"
failed = []
for item in [1, 2, 3]:
    row = call_tool(name, {"id": item}, step=f"pipeline-{item}")
    if row["failed"]:
        failed.append(row["id"])
print(json.dumps(failed))
''')
    assert result["status"] == "success", result
    assert json.loads(result["output"]) == [2]
    assert len(calls) == 3
    assert "large" not in result["output"]
    # Completed replay returns the saved final output without any tool execution.
    replay = execute(scope, resume_run_id=result["code_run_id"])
    assert replay == result
    assert len(calls) == 3


def test_sdk_cannot_reach_another_agent_or_invalid_arguments(child_env):
    calls = []
    scope = CodeToolScope("leaf", [definition("mcp_allowed"), definition("delegate_task")],
        lambda *args: calls.append(args))
    for code in [
        'call_tool("mcp_private", {})',
        'call_tool("delegate_task", {})',
        'call_tool("execute_code", {})',
        'call_tool("mcp_allowed", {"id": "wrong"})',
    ]:
        result = execute(scope, "from hermes_tools import call_tool\n" + code)
        assert result["status"] != "success", result
    assert calls == []


def test_recorded_steps_resume_after_real_process_exit(child_env, tmp_path):
    from tools.code_execution_runs import CodeRun
    witness, run_file = tmp_path / "effect.txt", tmp_path / "run.txt"
    # The process exits after a completed write step and before its next call.
    worker = '''
import os, sys
from pathlib import Path
from tools.code_execution_runs import CodeRun
run = CodeRun("same-session", "saved script")
Path(sys.argv[2]).write_text(run.run_id)
def write(name, args, uid):
    with open(args["path"], "a") as f: f.write("once\\n")
    return '{"written":true}'
run.call("write_file", {"path":sys.argv[1]}, write, step="write")
os._exit(0)
'''
    subprocess.run([sys.executable, "-c", worker, str(witness), str(run_file)], check=True, timeout=30,
        cwd=Path(__file__).resolve().parents[2], env=dict(os.environ))
    run = CodeRun("same-session", "", resume_run_id=run_file.read_text())
    def no_repeat(*_):
        pytest.fail("a completed write must not be executed twice")
    assert json.loads(run.call("write_file", {"path": str(witness)}, no_repeat, step="write"))["written"]
    assert witness.read_text() == "once\n"
    # Same key with changed arguments never reuses the wrong result.
    run.used_steps.clear()
    with pytest.raises((RuntimeError, ValueError)):
        run.call("write_file", {"path": "different"}, no_repeat, step="write")
    with pytest.raises(ValueError, match="Unknown code run"):
        CodeRun("another-session", "", resume_run_id=run.run_id)


def test_unknown_effect_and_busy_resource_are_never_retried(child_env):
    from tools.code_execution_runs import CodeRun
    run = CodeRun("same", "script")
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise OSError("response lost after write")
    with pytest.raises(OSError):
        run.call("write_file", {}, uncertain, step="write")
    # A fresh in-memory runner represents recovery, not another external attempt.
    run.used_steps.clear()
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        run.call("write_file", {}, uncertain, step="write")
    assert len(calls) == 1
    other = CodeRun("same", "other")
    row, started = other.store.admit("same", "busy", "busy", "browser_navigate", {}, "desktop", "elsewhere")
    assert started
    with pytest.raises(RuntimeError, match="Resource busy"):
        other.call("browser_click", {}, uncertain)
    assert len(calls) == 1
    other.store.complete(row["operation_id"], "{}")


@pytest.mark.parametrize("step", [None, "write-once"])
def test_rpc_response_loss_reuses_the_same_step(child_env, step):
    from tools.code_execution_runs import CodeRun
    run = CodeRun("same", "script")
    calls = []
    def write(name, args, uid):
        calls.append(uid)
        return '{"written":true}'
    first = run.call("write_file", {}, write, step=step, request_id="same-wire-request")
    repeated = run.call("write_file", {}, write, step=step, request_id="same-wire-request")
    assert first == repeated
    assert len(calls) == 1


def test_script_resumes_in_a_fresh_kernel_without_repeating_completed_steps(child_env, tmp_path):
    marker = tmp_path / "continue"
    calls = []
    def invoke(name, args, uid):
        calls.append(args["id"])
        return json.dumps({"id": args["id"]})
    script = f'''
from pathlib import Path
from hermes_tools import call_tool
first = call_tool("mcp_demo_read", {{"id": 1}}, step="first")
if not Path({str(marker)!r}).exists():
    raise RuntimeError("simulated local stop between calls")
second = call_tool("mcp_demo_read", {{"id": 2}}, step="second")
print(first["id"] + second["id"])
'''
    first = execute(CodeToolScope("same", [definition("mcp_demo_read")], invoke), script)
    assert first["status"] == "error", first
    assert calls == [1]
    shutdown_all_kernels()
    marker.touch()
    resumed = execute(CodeToolScope("same", [definition("mcp_demo_read")], invoke), resume_run_id=first["code_run_id"])
    assert resumed["status"] == "success", resumed
    assert resumed["output"].strip() == "3"
    assert calls == [1, 2]


def test_real_local_mcp_server_through_agent_policy(child_env, tmp_path):
    from agent.code_execution import code_execution_scope
    from run_agent import AIAgent
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    server = tmp_path / "demo_mcp.py"
    server.write_text('''
from mcp.server import MCPServer
mcp = MCPServer("local-code-demo")
@mcp.tool(annotations={"readOnlyHint": True})
def pipeline(id: int) -> dict:
    return {"id": id, "failed": id == 2, "url": f"https://example.test/pipelines/{id}"}
mcp.run(transport="stdio")
''')
    try:
        names = register_mcp_servers({"code-demo": {"command": sys.executable, "args": [str(server)], "trust": "full"}})
        name = next(n for n in names if n.endswith("pipeline"))
        with patch("agent.process_bootstrap.OpenAI"):
            agent = AIAgent(api_key="test-only", base_url="http://127.0.0.1:9/v1", provider="custom",
                model="gpt-4.1", quiet_mode=True, session_id="mcp-demo-session", skip_context_files=True,
                skip_memory=True, enabled_toolsets=["mcp-code-demo"])
        with code_execution_scope(agent, agent.session_id, "demo-code"):
            result = json.loads(execute_code(f'''
from hermes_tools import call_tool
import json
rows = [call_tool({name!r}, {{"id": i}}, step=f"pipeline-{{i}}") for i in [1,2,3]]
print(json.dumps(rows))
''', task_id=agent.session_id))
        assert result["status"] == "success", result
        assert "example.test/pipelines/2" in result["output"]
        assert result["tool_calls_made"] == 3
        # The real agent binding retains its grant, even though other registry
        # tools are available to other sessions in this process.
        with code_execution_scope(agent, agent.session_id, "outside-scope"):
            denied = json.loads(execute_code('from hermes_tools import call_tool\ncall_tool("terminal", {"command":"true"})',
                task_id=agent.session_id))
        assert denied["status"] == "error", denied
        from agent.tool_guardrails import ToolGuardrailDecision
        decision = ToolGuardrailDecision(action="block", code="test", message="blocked-by-native-policy", tool_name=name)
        with patch.object(agent._tool_guardrails, "before_call", return_value=decision) as guard:
            with code_execution_scope(agent, agent.session_id, "guardrail"):
                denied = json.loads(execute_code(f'from hermes_tools import call_tool\ncall_tool({name!r}, {{"id": 4}})',
                    task_id=agent.session_id))
        assert denied["status"] == "error", denied
        assert "blocked-by-native-policy" in json.dumps(denied)
        guard.assert_called_once_with(name, {"id": 4})
    finally:
        shutdown_mcp_servers(names={"code-demo"})
