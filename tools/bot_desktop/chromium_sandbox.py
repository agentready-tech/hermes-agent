"""Keep agent-browser's automatic Docker detection from disabling Chromium's sandbox."""


def launcher_script(executable: str) -> str:
    """agent-browser 0.26 adds --no-sandbox in Docker with no opt-out; filter it at exec.

    The browser still runs as pn and must successfully create its own sandbox. A kernel or
    operator policy denying that sandbox causes a startup failure, never a weaker retry.
    """
    return f'''#!/usr/bin/env python3
import os, sys
if os.geteuid() == 0:
    raise SystemExit("Sandboxed Chromium must run as the desktop user, not root")
args = [arg for arg in sys.argv[1:] if arg != "--no-sandbox" and not arg.startswith("--no-sandbox=")]
os.execv({executable!r}, [{executable!r}, *args])
'''


def sandboxed_executable(env) -> str:
    """Install the small launcher inside the desktop user's home, once per environment."""
    from tools.bot_desktop import sandbox_host
    from tools.environments import streams

    executable = sandbox_host.chromium_executable(env)
    if not executable:
        raise RuntimeError("The desktop sandbox image has no Chromium executable")
    cached = getattr(env, "_bd_sandboxed_chromium", None)
    if cached and cached[0] == executable:
        return cached[1]
    target = f"{sandbox_host.desktop_home(env).rstrip('/')}/.hermes/bot-desktop/chromium-sandbox-launcher"
    script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.argv[2]); p.chmod(0o700)"
    result = streams.run_in(env, ["python3", "-c", script, target, launcher_script(executable)],
                            user=sandbox_host._user_for(env), timeout=15)
    if result.returncode:
        raise RuntimeError("Could not install the sandboxed Chromium launcher")
    env._bd_sandboxed_chromium = (executable, target)
    return target
