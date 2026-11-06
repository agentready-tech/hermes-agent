"""Stdio MCP servers execute in the same native sandbox as terminal tools.

Only reviewed first-party custody brokers may stay beside the Hermes vault. They
must execute all external I/O through the same sandbox transport as other tools.
"""

from pathlib import Path
import sys

from tools.environments import streams
from tools.environments.network_transport import SandboxTransportUnavailable, sandbox_environment

# Runs as the sandbox account. The first frame is private startup data, consumed
# before the actual server sees stdin. Neither credentials nor config reach argv.
BOOTSTRAP = '''import json, os, signal, subprocess, sys, threading
process = None
try:
    line = sys.stdin.buffer.readline(8*1024*1024+1)
    if len(line) > 8*1024*1024: raise ValueError()
    frame = json.loads(line)
    if frame.get("method") != "hermes/sandbox/launch": raise ValueError()
    spec = frame["params"]
    argv, env, cwd = spec["argv"], spec["env"], spec.get("cwd")
    if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or "\\0" in x for x in argv): raise ValueError()
    if not isinstance(env,dict) or any(not isinstance(k,str) or not isinstance(v,str) or "\\0" in k+v or "=" in k for k,v in env.items()): raise ValueError()
    if cwd is not None and (not isinstance(cwd,str) or not os.path.isabs(cwd)): raise ValueError()
    process = subprocess.Popen(argv, env={**os.environ, **env}, cwd=cwd, stdin=subprocess.PIPE, start_new_session=True)
    def feed():
        try:
            while True:
                data = sys.stdin.buffer.read1(65536)
                if not data: break
                process.stdin.write(data); process.stdin.flush()
        except (OSError,ValueError): pass
        finally:
            try: process.stdin.close()
            except OSError: pass
            try: os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try: os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError: pass
    threading.Thread(target=feed,daemon=True).start()
    process.wait()
except (OSError,ValueError,TypeError,KeyError):
    print("MCP server could not start in its configured sandbox; check its executable and working directory there",file=sys.stderr)
finally:
    if process is not None:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
'''


def custody_broker(config):
    """Recognize exact immutable first-party modules, never an arbitrary server name."""
    command = config.get("command")
    if not isinstance(command, str) or Path(command).resolve() != Path(sys.executable).resolve():
        return False
    args = config.get("args") or []
    if not isinstance(args, list):
        return False
    from hermes_constants import get_hermes_home
    configured_home = (config.get("env") or {}).get("HERMES_HOME")
    if configured_home is not None and Path(configured_home).resolve() != get_hermes_home().resolve():
        return False
    # -m must resolve reviewed installed code, never an arbitrary configured cwd/PYTHONPATH.
    if config.get("cwd") != str(Path(__file__).resolve().parents[1]):
        return False
    if set(config.get("env") or {}) - {"HERMES_HOME"}:
        return False
    if args == ["-m", "hermes_runtime.workspace.mcp"]:
        return True
    if (len(args) == 3 and args[:2] == ["-m", "tools.connectors.api_mcp"]
            and isinstance(args[2], str) and config.get("hermes_api")):
        import re
        return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", args[2]))
    return False


def stdio_sandbox(config):
    """Select the native sandbox, retaining host-only first-party vault custody."""
    if custody_broker(config):
        return None
    return sandbox_environment()


def remote_parameters(environment, config, safe_env):
    """Return non-secret exec argv/client env plus protected server startup data."""
    command = streams.remote_command(environment, ["python3", "-I", "-c", BOOTSTRAP], interactive=True)
    if command is None:
        raise SandboxTransportUnavailable("The configured sandbox cannot host an MCP server; host execution is disabled")
    # Infrastructure belongs to the exec client, not to a tool running in the sandbox.
    excluded = {"HERMES_HOME", "HOME", "PATH", "USER", "SHELL", "TMPDIR",
                "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBERNETES_SERVICE_PORT_HTTPS"}
    remote_env = {k: v for k, v in safe_env.items() if k not in excluded}
    # Explicit server env is intentional, but never export the Hermes profile/vault path.
    remote_env.update({k: v for k, v in (config.get("env") or {}).items() if k != "HERMES_HOME"})
    spec = {"argv": [config["command"], *(config.get("args") or [])], "env": remote_env,
            "cwd": config.get("cwd") or getattr(environment, "cwd", None)}
    return command, streams.client_environment(), spec


async def send_startup(write_stream, spec):
    from mcp import types
    from mcp.shared.message import SessionMessage
    try:
        request = types.JSONRPCNotification(jsonrpc="2.0", method="hermes/sandbox/launch", params=spec)
        message = SessionMessage(request)
    except (ValueError, TypeError):
        raise SandboxTransportUnavailable("Invalid protected MCP sandbox startup data") from None
    await write_stream.send(message)


def custody_arguments(config):
    """An isolated interpreter loads only the reviewed installed module tree."""
    module = config["args"][1]
    arguments = config["args"][2:]
    root = str(Path(__file__).resolve().parents[1])
    code = ("import runpy,sys;sys.path.insert(0," + repr(root) + ");sys.argv=" +
            repr([module, *arguments]) + ";runpy.run_module(" + repr(module) + ",run_name='__main__')")
    return ["-I", "-c", code]
