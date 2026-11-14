"""Run fixed workspace programs through the selected network transport."""

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from hermes_runtime.workspace.store import WorkspaceError


@contextmanager
def profile_scope(home):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    home_token = set_hermes_home_override(Path(home))
    secret_token = set_secret_scope(build_profile_secret_scope(Path(home)), profile_home=str(home))
    terminal_token = install_profile_terminal_scope(Path(home))
    try:
        yield
    finally:
        reset_terminal_scope(terminal_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def poll_request(_home, program, request):
    """The background collector can read the mailbox with the sandbox stopped."""
    operation = request.get("operation")
    imap_poll = program == "sandbox_mail" and operation == "incoming"
    gmail_poll = (program == "sandbox_google" and operation == "request"
                  and request.get("service") == "gmail" and request.get("method") == "GET")
    if not (imap_poll or gmail_poll):
        raise WorkspaceError("INVALID_WORKSPACE_OPERATION")
    # The child receives the owning profile's request on stdin and does not
    # inherit the parent environment or its credentials.
    return run_request(program_argv(program, sys.executable), {}, program, request,
                       "INVALID_MAIL_HERMES_UNAVAILABLE")


def program_argv(program, executable):
    if program not in {"sandbox_mail", "sandbox_google", "sandbox_discovery"}:
        raise WorkspaceError("INVALID_WORKSPACE_OPERATION")
    source = Path(__file__).with_name(program + ".py").read_text()
    if program != "sandbox_mail":
        mail_source = Path(__file__).with_name("sandbox_mail.py").read_text()
        source = "__name__ = '_workspace_mail'\n" + mail_source + "\n__name__ = '__main__'\n" + source
    return [executable, "-I", "-c", source]


def sandbox_request(home, program, request):
    from tools.environments import streams
    from tools.terminal_tool import ensure_task_env
    argv = program_argv(program, "python3")
    with profile_scope(home):
        env = ensure_task_env("agentready_home")
        if env is None:
            raise WorkspaceError("INVALID_MAIL_SANDBOX_UNAVAILABLE")
        command = streams.remote_command(env, argv, user="nobody", interactive=True)
        if command is None:
            raise WorkspaceError("INVALID_MAIL_SANDBOX_UNAVAILABLE")
        return run_request(command, streams.client_environment(), program, request,
                           "INVALID_MAIL_SANDBOX_UNAVAILABLE")


def run_request(command, environment, program, request, unavailable):
    try:
        result = subprocess.run(command, input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=105, check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkspaceError(failure_code(program, request, unavailable)) from None
    if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
        raise WorkspaceError(failure_code(program, request, "INVALID_WORKSPACE_RESPONSE"))
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise WorkspaceError(failure_code(program, request, "INVALID_WORKSPACE_RESPONSE")) from None
    if not isinstance(value, dict):
        raise WorkspaceError(failure_code(program, request, "INVALID_WORKSPACE_RESPONSE"))
    if value.get("ok") is not True:
        code = value.get("error", "")
        if not isinstance(code, str) or not code.startswith(("INVALID_", "FORBIDDEN", "NOT_FOUND")) or len(code) > 100 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in code):
            code = failure_code(program, request, "INVALID_WORKSPACE_RESPONSE")
        raise WorkspaceError(code)
    if not isinstance(value.get("value"), dict):
        raise WorkspaceError(failure_code(program, request, "INVALID_WORKSPACE_RESPONSE"))
    return value.get("value")


def failure_code(program, request, ordinary):
    if program == "sandbox_mail" and request.get("operation") == "send":
        return "INVALID_MAIL_SEND_OUTCOME_UNKNOWN"
    if program == "sandbox_google" and (request.get("operation") == "gws" or
            (request.get("operation") == "request" and request.get("method") in {"POST", "PUT", "PATCH", "DELETE"})):
        return "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN"
    return ordinary
