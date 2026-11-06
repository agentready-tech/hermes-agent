"""Catalog Git installs in the owning terminal sandbox, without host paths or credentials."""
from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath

from tools.environments import streams


# Stable version paths keep bootstrap-created venv shebangs valid. Owner-side config publication
# selects a completed version; a failed reinstall leaves the previous install and config intact.
_INSTALL_SCRIPT = r'''
set -eu
action=$1
name=$2
shift 2
case "${HOME:-}" in /*) ;; *) exit 21 ;; esac
home=$(cd -- "$HOME" && pwd -P) || exit 21
root="$home/mcp-installs"
root_marker="$root/.hermes-mcp-installs"
if [ "$action" = install ]; then
    command -v git >/dev/null 2>&1 || exit 22
fi
if [ -e "$root" ] || [ -L "$root" ]; then
    [ -d "$root" ] && [ ! -L "$root" ] && [ -f "$root_marker" ] &&
        [ ! -L "$root_marker" ] && [ "$(cat -- "$root_marker")" = hermes-mcp-installs-v1 ] || exit 21
elif [ "$action" = purge ]; then
    exit 0
else
    mkdir -- "$root" || exit 21
    printf '%s\n' hermes-mcp-installs-v1 > "$root_marker"
fi
target="$root/$name"
marker="$target/.hermes-mcp-install"
if [ -e "$target" ] || [ -L "$target" ]; then
    [ -d "$target" ] && [ ! -L "$target" ] && [ -f "$marker" ] &&
        [ ! -L "$marker" ] && [ "$(cat -- "$marker")" = "hermes-mcp-install-v1:$name" ] || exit 21
elif [ "$action" = purge ]; then
    exit 0
else
    mkdir -- "$target" || exit 21
    printf '%s\n' "hermes-mcp-install-v1:$name" > "$marker"
fi
if [ "$action" = purge ]; then
    rm -rf -- "$target" || exit 21
    printf '%s\n' removed
    exit 0
fi
url=$1
ref=$2
shift 2
stage=$(mktemp -d "$target/install-XXXXXXXX") || exit 21
trap 'rm -rf -- "$stage"' EXIT
if [[ "$ref" =~ ^[0-9a-f]{7,40}$ ]]; then
    full_clone=1
elif git clone --depth 1 --branch "$ref" -- "$url" "$stage" >/dev/null 2>&1; then
    full_clone=0
else
    rm -rf -- "$stage"
    mkdir -- "$stage"
    full_clone=1
fi
if [ "$full_clone" = 1 ]; then
    git clone -- "$url" "$stage" >/dev/null 2>&1 || exit 23
    git -C "$stage" checkout "$ref" >/dev/null 2>&1 || exit 23
fi
for command in "$@"; do
    (cd -- "$stage" && bash -c "$command") >/dev/null 2>&1 || exit 24
done
printf '%s\n' "$stage"
trap - EXIT
'''


def installation_environment():
    from hermes_cli.mcp_catalog import CatalogError
    from tools.environments.network_transport import SandboxTransportUnavailable, sandbox_environment

    try:
        return sandbox_environment()
    except SandboxTransportUnavailable:
        raise CatalogError("The configured MCP sandbox is unavailable", failure_class="clone_failed") from None


def _run(environment, action: str, name: str, *args: str) -> bytes:
    from hermes_cli.mcp_catalog import CatalogError

    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise CatalogError("Invalid catalog install name")
    try:
        result = streams.run_in(
            environment, ["bash", "-c", _INSTALL_SCRIPT, "hermes-mcp-catalog", action, name, *args],
            child_env={"GIT_TERMINAL_PROMPT": "0"}, timeout=600,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise CatalogError("MCP catalog sandbox operation could not run", failure_class="clone_failed") from None
    failures = {
        21: ("MCP catalog sandbox install ownership check failed", "config_invalid"),
        22: ("git is required inside the MCP sandbox", "git_missing"),
        23: ("MCP catalog sandbox git clone or checkout failed", "clone_failed"),
        24: ("MCP catalog sandbox bootstrap step failed", "bootstrap_failed"),
    }
    if result.returncode:
        message, failure_class = failures.get(result.returncode, ("MCP catalog sandbox operation failed", "clone_failed"))
        raise CatalogError(message, failure_class=failure_class)
    return result.stdout


def install_git(environment, entry) -> PurePosixPath:
    from hermes_cli.mcp_catalog import CatalogError

    install = entry.install
    output = _run(environment, "install", entry.name, install.url, install.ref, *install.bootstrap)
    try:
        path = PurePosixPath(output.decode("utf-8").strip())
    except UnicodeError:
        raise CatalogError("MCP sandbox returned an invalid install directory") from None
    if (not path.is_absolute() or path.parent.name != entry.name or path.parent.parent.name != "mcp-installs"
            or not re.fullmatch(r"install-[A-Za-z0-9]+", path.name)):
        raise CatalogError("MCP sandbox returned an invalid install directory")
    return path


def purge_install(environment, name: str) -> bool:
    return _run(environment, "purge", name).strip() == b"removed"
