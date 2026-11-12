"""The bot's browser on its Bot Desktop: one executable, one persistent user-data-dir per profile.

The agent drives Chromium through agent-browser; a human who takes over clicks the dock's Browser
icon. Both must be THE SAME browser — same binary, same ``--user-data-dir`` — or the human logs in
to a jar the bot never sees. Chromium's singleton makes a second launch on the same user-data-dir
open a window in the running instance, which is exactly the hand-over we want — in ONE direction. When the
human's dock instance is already up, agent-browser's own launch is forwarded to it and dies without a
DevTools endpoint, so the dock exposes a debugging port and the agent ATTACHES to it (see
:func:`running_instance_cdp_port`) instead of launching.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

from tools.bot_desktop import runtime

DISK_CACHE_BYTES = 256 * 1024 * 1024

_SYSTEM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def profile_dir() -> Path:
    """User-data-dir the bot's browser uses on this profile's screen. ``AGENT_BROWSER_PROFILE`` pins your own:
    ``~`` expands, and a relative path is anchored at this profile's HERMES_HOME (where the rest of the screen's
    state lives), so ``pin`` means ``<HERMES_HOME>/pin`` and two profiles never share one jar by accident."""
    override = os.path.expanduser(os.environ.get("AGENT_BROWSER_PROFILE", "").strip())
    if override:
        return Path(override) if os.path.isabs(override) else runtime.get_hermes_home() / override
    return runtime.state_dir() / "browser-profile"


def executable() -> Optional[str]:
    """The Chromium agent-browser launches: an explicit ``AGENT_BROWSER_EXECUTABLE_PATH``, else the newest
    Playwright Chromium it bundles, else a system Chrome/Chromium. ``None`` when there is none.

    Non-root under ``kernel.apparmor_restrict_unprivileged_userns=1`` (Ubuntu 23.10+) flips the order:
    Playwright's bundle has no setuid ``chrome_sandbox`` and dies 'FATAL: No usable sandbox!' there, while
    a distro chromium ships the helper. The bundle stays the answer when it is the only browser — a dock
    icon that fails loudly beats a non-root ``--no-sandbox``.
    """
    explicit = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if explicit and os.access(explicit, os.X_OK) and not _is_headless_shell(explicit):
        return explicit
    finders = [_managed_executable, _system_executable]
    if not _is_root() and _userns_restricted():
        finders.reverse()
    return next((exe for find in finders if (exe := find())), None)


def _managed_executable() -> Optional[str]:
    from hermes_cli.browser_runtime import chromium_executable

    exe = chromium_executable(allow_override=False)
    return exe if exe and os.access(exe, os.X_OK) and not _is_headless_shell(exe) else None


def _is_headless_shell(exe: str) -> bool:
    """Playwright's ``chrome-headless-shell`` can drive pages but cannot open a window. The image's boot
    hook exports it as ``AGENT_BROWSER_EXECUTABLE_PATH`` (it is the only build the unsuffixed tags carry,
    and the lighter one everywhere), so trusting that override blindly would pin a windowless binary to
    the dock's Browser icon."""
    return "headless" in os.path.basename(exe).lower() or "headless_shell" in exe


def _system_executable() -> Optional[str]:
    return next((shutil.which(name) for name in _SYSTEM_BROWSERS if shutil.which(name)), None)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _userns_restricted() -> bool:
    from tools.browser_tool_session import apparmor_restricts_unprivileged_userns
    return apparmor_restricts_unprivileged_userns()


def dock_launch() -> Optional[tuple[str, str]]:
    """``(executable, user_data_dir)`` for the dock's Browser icon, or ``None`` when no Chromium exists."""
    exe = executable()
    return (exe, str(profile_dir())) if exe else None


def proxy_args(proxy: Optional[str] = None) -> list[str]:
    """One opt-in browser-only egress policy, shared by native dock and agent-browser."""
    if proxy is None:
        from hermes_cli.config import load_config_readonly
        proxy = (load_config_readonly().get("browser") or {}).get("proxy", "")
    if not proxy:
        return []
    import ipaddress
    from urllib.parse import urlsplit
    url = urlsplit(proxy)
    address = ipaddress.ip_address(url.hostname or "")
    if (url.scheme not in {"http", "https", "socks5"} or url.username or url.password
            or url.path not in {"", "/"} or url.query or url.fragment
            or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified):
        raise ValueError("browser.proxy requires an operator-managed private proxy without credentials")
    return [f"--proxy-server={proxy}", "--proxy-bypass-list=localhost;127.0.0.1;[::1]", "--disable-quic",
            "--dns-prefetch-disable", "--host-resolver-rules=MAP * ~NOTFOUND",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]


def dock_argv(exe: str, user_data_dir: str, *, sandbox_bypass: Optional[bool] = None) -> list[str]:
    """Common native headed Chrome arguments; browser_launch adds CDP inside its namespace."""
    # --test-type hides the "Chrome for Testing is only for automated testing" and unsupported-flag
    # (--no-sandbox as root) infobars, which otherwise sit at the top of the human's takeover view.
    # The same sandbox policy agent-browser starts this binary with (root, Docker, AppArmor userns): the
    # human's Browser is the bot's browser, in the same container; a stricter rule here just made the dock
    # icon die with 'No usable sandbox!' in the official image while the agent's own Chromium ran fine.
    from tools.browser_tool_session import CHROMIUM_SANDBOX_BYPASS_ARGS, _needs_chromium_sandbox_bypass
    # The profile is persistent by design (logins survive handoffs); its HTTP cache is not worth a
    # gateway's disk: uncapped it grows for months toward a hosted instance's 6 GB.
    return [exe, f"--user-data-dir={user_data_dir}", "--no-first-run",
            "--no-default-browser-check", "--test-type", f"--disk-cache-size={DISK_CACHE_BYTES}", *proxy_args(),
            *(CHROMIUM_SANDBOX_BYPASS_ARGS if (_needs_chromium_sandbox_bypass() if sandbox_bypass is None
                                              else sandbox_bypass) else ())]


def dock_exec_line(exe: str, user_data_dir: str, *, sandbox_bypass: Optional[bool] = None,
                   launch_helper: Optional[str] = None, memory_guard: Optional[dict] = None) -> str:
    """The ``Exec=`` line of the dock's ``.desktop`` entry. Every argument is double-quoted per the
    Desktop Entry spec (a browser under ``/opt/Google Chrome/`` or a profile under a spaced HERMES_HOME
    otherwise splits into garbage): inside the quotes ``" ` $ \\`` are backslash-escaped, and because the
    value is itself a string field, each of those backslashes is escaped once more."""
    def quote(arg: str) -> str:
        quoted = "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg)
        return '"' + quoted.replace("\\", "\\\\") + '"'
    helper = launch_helper or str(Path(__file__).with_name('browser_launch.py'))
    import json
    guard_args = ['--guard', json.dumps(memory_guard)] if memory_guard else []
    command = ['python3', helper, '--dock', *guard_args,
               *dock_argv(exe, user_data_dir, sandbox_bypass=sandbox_bypass)]
    return "Exec=" + " ".join(quote(arg) for arg in command)


def ensure_instance_endpoint(user_data_dir: str, *, exclude_session: Optional[str] = None):
    """Agent-first and human-first use the same native headed browser, then attach."""
    import json
    import subprocess
    import sys
    from tools.bot_desktop.browser_instance import admitted_endpoint
    from tools.environments.local import served_profile_child_env
    exe = executable()
    if not exe:
        from tools.bot_desktop.browser_instance import ProfileUnavailable
        raise ProfileUnavailable('unknown')
    child = env_for_agent(served_profile_child_env(inherit_credentials=False))
    child.update(runtime.published_env())
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('browser_launch.py'))],
                            input=json.dumps({'argv': dock_argv(exe, user_data_dir),
                                              'exclude_session': exclude_session}).encode(),
                            env=child, capture_output=True, timeout=20, check=False)
    try:
        value = json.loads(result.stdout) if result.returncode == 0 else {}
    except (ValueError, TypeError):
        value = {}
    return admitted_endpoint(value if isinstance(value, dict) else {})


def running_instance_cdp_port(user_data_dir: str, *, exclude_session: Optional[str] = None) -> Optional[int]:
    """Attach to a verified existing owner; never silently relaunch an occupied profile."""
    from tools.bot_desktop.browser_instance import admitted_port, inspect_profile
    return admitted_port(inspect_profile(user_data_dir, exclude_session=exclude_session))


def running_instance_endpoint(user_data_dir: str, *, exclude_session: Optional[str] = None):
    from tools.bot_desktop.browser_instance import admitted_endpoint, inspect_profile
    return admitted_endpoint(inspect_profile(user_data_dir, exclude_session=exclude_session))


def env_for_agent(env: dict) -> dict:
    """Pin agent-browser to the screen's browser identity unless the user pinned their own.

    A headless-shell pin is replaced, not kept: the image's boot hook exports one for ordinary browsing,
    and leaving it would put the agent and the dock on different binaries over one ``--user-data-dir``,
    where Chromium's singleton swallows the dock's launch into the windowless process. Only runs while a
    screen is up (:func:`runtime.desktop_env`), so the heavier build is pinned just when it is the point.
    """
    env.setdefault("AGENT_BROWSER_PROFILE", str(profile_dir()))
    exe = executable()
    if exe:
        pinned = env.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
        if not pinned or _is_headless_shell(pinned):
            env["AGENT_BROWSER_EXECUTABLE_PATH"] = exe
    return env


def open_google() -> bool:
    """Human settings action; never use agent-browser or a different terminal/profile fallback."""
    from tools.bot_desktop import placement
    where = placement.resolve()
    if where.where == placement.TERMINAL:
        from tools.bot_desktop import sandbox_host
        env = runtime._owned_sandbox_env()
        if env is None:
            raise RuntimeError('The own desktop environment is unavailable')
        return sandbox_host.open_google(env)
    if where.where != placement.GATEWAY:
        raise RuntimeError('The own desktop environment is unavailable')
    import json
    import subprocess
    import sys
    from tools.environments.local import served_profile_child_env
    exe = executable()
    if not exe:
        raise RuntimeError('The own native browser is unavailable')
    child = env_for_agent(served_profile_child_env(inherit_credentials=False))
    child.update(runtime.published_env())
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('browser_launch.py'))],
        input=json.dumps({'argv': dock_argv(exe, str(profile_dir())), 'open_google': True}).encode(),
        env=child, capture_output=True, timeout=30, check=False)
    return result.returncode == 0 and json.loads(result.stdout).get('opened') is True
