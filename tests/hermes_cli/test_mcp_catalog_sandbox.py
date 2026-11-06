"""Catalog installs execute in a fake existing sandbox with no real Git or network."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.platforms("posix")


def _entry():
    from hermes_cli.mcp_catalog import AuthSpec, CatalogEntry, InstallSpec, TransportSpec
    return CatalogEntry(
        name="demo", description="Fixture MCP", source="fixture",
        transport=TransportSpec(type="stdio", command="${INSTALL_DIR}/run.sh"),
        auth=AuthSpec(type="none"),
        install=InstallSpec(type="git", url="https://fixture.invalid/repo.git", ref="a" * 40,
                            bootstrap=["remote-package-manager install"]),
    )


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    from hermes_cli import git_credentials, mcp_catalog
    from tools.environments import network_transport, streams

    home = tmp_path / "sandbox home"
    owner = tmp_path / "owner-home"
    home.mkdir()
    owner.mkdir()
    bin_dir = home / "bin"
    bin_dir.mkdir()
    # Only fixture git/runtime and these filesystem utilities exist on the fake remote PATH.
    for name in ("bash", "cat", "mkdir", "mktemp", "rm", "chmod"):
        (bin_dir / name).symlink_to(shutil.which(name))
    git = bin_dir / "git"
    git.write_text('''#!/usr/bin/env bash
printf 'CALL\\n' >> "$HOME/git.calls"
printf '%s\\n' "$@" >> "$HOME/git.calls"
if [ "$1" = clone ]; then
    if [ "${FIXTURE_FAILURE:-}" = clone ] || { [ "${FIXTURE_FAILURE:-}" = shallow ] && [ "$2" = --depth ]; }; then
        printf '%s\\n' fixture-private-error >&2
        exit 1
    fi
    dest="${@: -1}"
    mkdir -p -- "$dest"
    printf '#!/usr/bin/env bash\\nexit 0\\n' > "$dest/run.sh"
    chmod +x "$dest/run.sh"
elif [ "${FIXTURE_FAILURE:-}" = checkout ]; then
    printf '%s\\n' fixture-private-error >&2
    exit 1
fi
''')
    git.chmod(0o755)
    runtime = bin_dir / "remote-package-manager"
    runtime.write_text('''#!/usr/bin/env bash
if [ "${FIXTURE_FAILURE:-}" = bootstrap ]; then
    printf '%s\\n' fixture-private-error >&2
    exit 1
fi
printf '%s\\n' "$PWD" > built-cwd
''')
    runtime.chmod(0o755)
    environment = object()
    state = SimpleNamespace(home=home, owner=owner, environment=environment, calls=[], failure="", git=git)
    run = subprocess.run

    def run_in(env, argv, *, child_env, timeout):
        assert env is environment
        assert child_env == {"GIT_TERMINAL_PROMPT": "0"}
        assert str(owner) not in " ".join(argv)
        state.calls.append(list(argv))
        if state.failure == "transport":
            raise RuntimeError("fixture-private-error")
        return run(argv, capture_output=True, timeout=timeout, check=False, cwd=home,
                   env={"HOME": str(home), "PATH": str(bin_dir), "FIXTURE_FAILURE": state.failure, **child_env})

    def no_host(*args, **kwargs):
        raise AssertionError("Catalog attempted host installation")

    monkeypatch.setenv("HERMES_HOME", str(owner))
    monkeypatch.setenv("OWNER_PRIVATE_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(network_transport, "sandbox_environment", lambda: environment)
    monkeypatch.setattr(streams, "run_in", run_in)
    monkeypatch.setattr(mcp_catalog.shutil, "which", no_host)
    monkeypatch.setattr(mcp_catalog, "_install_root", no_host)
    monkeypatch.setattr(mcp_catalog, "_run_bootstrap", no_host)
    monkeypatch.setattr(git_credentials, "run_git_with_credential_fallback", no_host)
    monkeypatch.setattr(mcp_catalog, "_apply_tool_selection", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_catalog, "record_mcp_install", lambda *args, **kwargs: None)
    return state


@pytest.mark.parametrize("surface", ["card", "cli"])
def test_remote_only_git_and_runtime_install_on_both_surfaces(sandbox, surface):
    from hermes_cli import mcp_catalog
    entry = _entry()
    if surface == "card":
        config = mcp_catalog.card_install_config(entry)
    else:
        mcp_catalog.install_entry(entry)
        config = mcp_catalog.installed_servers()[entry.name]

    install = Path(config["command"]).parent
    assert install.parent == sandbox.home / "mcp-installs" / "demo"
    assert (install / "built-cwd").read_text().strip() == str(install)
    assert config["command"] == str(install / "run.sh")
    assert not (sandbox.owner / "mcp-installs").exists()
    assert len(sandbox.calls) >= 1


@pytest.mark.parametrize("failure", ["clone", "checkout", "bootstrap", "transport", "git_missing"])
def test_failed_reinstall_preserves_existing_install_and_config(sandbox, failure, capsys):
    from hermes_cli import mcp_catalog
    from hermes_cli.config import save_config
    entry = _entry()
    previous = mcp_catalog.card_install_config(entry)
    old_install = Path(previous["command"]).parent
    save_config({"mcp_servers": {entry.name: previous}})
    config_path = sandbox.owner / "config.yaml"
    original = config_path.read_bytes()
    sandbox.failure = failure
    if failure == "git_missing":
        sandbox.git.unlink()

    with pytest.raises(mcp_catalog.CatalogError) as error:
        mcp_catalog.install_entry(entry)

    assert config_path.read_bytes() == original
    assert (old_install / "built-cwd").read_text().strip() == str(old_install)
    assert list(old_install.parent.glob("install-*")) == [old_install]
    assert "fixture-private-error" not in str(error.value)
    assert "fixture-private-error" not in capsys.readouterr().out


def test_successful_reinstall_keeps_stable_bootstrap_paths(sandbox):
    from hermes_cli.mcp_catalog import card_install_config
    first = Path(card_install_config(_entry())["command"]).parent
    second = Path(card_install_config(_entry())["command"]).parent
    assert first != second
    assert (first / "built-cwd").read_text().strip() == str(first)
    assert (second / "built-cwd").read_text().strip() == str(second)


def test_remote_branch_failure_uses_full_clone_in_same_sandbox(sandbox):
    from hermes_cli.mcp_catalog import card_install_config
    entry = _entry()
    entry.install.ref = "fixture-tag"
    sandbox.failure = "shallow"

    config = card_install_config(entry)

    assert Path(config["command"]).exists()
    commands = (sandbox.home / "git.calls").read_text()
    assert "--branch\nfixture-tag\n" in commands
    assert "checkout\nfixture-tag\n" in commands
    assert len(sandbox.calls) == 1


def test_unavailable_sandbox_preserves_config_without_host_fallback(sandbox, monkeypatch):
    from hermes_cli import mcp_catalog
    from hermes_cli.config import save_config
    from tools.environments import network_transport
    config = mcp_catalog.card_install_config(_entry())
    save_config({"mcp_servers": {"demo": config}})
    path = sandbox.owner / "config.yaml"
    original = path.read_bytes()
    previous_calls = len(sandbox.calls)

    def unavailable():
        raise network_transport.SandboxTransportUnavailable("fixture-private-error")

    monkeypatch.setattr(network_transport, "sandbox_environment", unavailable)
    with pytest.raises(mcp_catalog.CatalogError, match="configured MCP sandbox is unavailable"):
        mcp_catalog.install_entry(_entry())
    assert path.read_bytes() == original
    assert len(sandbox.calls) == previous_calls


def test_remote_git_arguments_are_not_shell_interpolated(sandbox):
    from hermes_cli.mcp_catalog import card_install_config
    entry = _entry()
    entry.install.url = "https://fixture.invalid/$(touch UNEXPECTED).git"
    entry.install.ref = "tag; touch UNEXPECTED"
    config = card_install_config(entry)
    assert Path(config["command"]).exists()
    assert not list(sandbox.home.rglob("UNEXPECTED"))
    assert entry.install.url in (sandbox.home / "git.calls").read_text()
    assert entry.install.ref in (sandbox.home / "git.calls").read_text()


@pytest.mark.parametrize("name", ["../other", "/absolute", "demo;touch UNEXPECTED"])
def test_invalid_catalog_names_never_reach_sandbox(sandbox, name):
    from hermes_cli.mcp_catalog import CatalogError, card_install_config
    with pytest.raises(CatalogError, match="Invalid catalog install name"):
        card_install_config(replace(_entry(), name=name))
    assert sandbox.calls == []


def test_remote_purge_removes_only_marked_sandbox_install(sandbox):
    from hermes_cli import mcp_catalog
    from hermes_cli.config import save_config
    config = mcp_catalog.card_install_config(_entry())
    install = Path(config["command"]).parent
    save_config({"mcp_servers": {"demo": config}})
    unrelated = sandbox.home / "keep"
    unrelated.write_text("preserve")

    assert mcp_catalog.uninstall_entry("demo") is True
    assert not install.parent.exists()
    assert unrelated.read_text() == "preserve"
    assert "demo" not in mcp_catalog.installed_servers()


@pytest.mark.parametrize("boundary", ["root_marker", "entry_marker", "root_symlink", "entry_symlink"])
def test_purge_refuses_unowned_or_symlink_roots_and_keeps_config(sandbox, boundary):
    from hermes_cli import mcp_catalog
    from hermes_cli.config import save_config
    config = mcp_catalog.card_install_config(_entry())
    root = sandbox.home / "mcp-installs"
    entry_root = root / "demo"
    save_config({"mcp_servers": {"demo": config}})
    config_path = sandbox.owner / "config.yaml"
    original = config_path.read_bytes()
    if boundary.endswith("marker"):
        marker = root / ".hermes-mcp-installs" if boundary == "root_marker" else entry_root / ".hermes-mcp-install"
        marker.unlink()
    else:
        target = root if boundary == "root_symlink" else entry_root
        original_target = sandbox.home / "protected-install"
        target.rename(original_target)
        target.symlink_to(original_target, target_is_directory=True)

    with pytest.raises(mcp_catalog.CatalogError, match="ownership check failed"):
        mcp_catalog.uninstall_entry("demo")

    assert config_path.read_bytes() == original
    assert Path(config["command"]).exists()


def test_explicit_local_backend_retains_existing_git_and_bootstrap_behavior(tmp_path, monkeypatch):
    from hermes_cli import git_credentials, mcp_catalog
    from tools.environments import network_transport
    root = tmp_path / "local-installs"
    root.mkdir()
    calls = []

    def git(argv, *args, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def bootstrap(cwd, commands):
        calls.append((cwd, commands))

    monkeypatch.setattr(network_transport, "sandbox_environment", lambda: None)
    monkeypatch.setattr(mcp_catalog, "_install_root", lambda: root)
    monkeypatch.setattr(mcp_catalog.shutil, "which", lambda name: "/fixture/local/git")
    monkeypatch.setattr(git_credentials, "run_git_with_credential_fallback", git)
    monkeypatch.setattr(mcp_catalog, "_run_bootstrap", bootstrap)

    result = mcp_catalog._do_git_install(_entry())

    assert result == root / "demo"
    assert calls[0][0] == ["/fixture/local/git", "clone", "https://fixture.invalid/repo.git", str(result)]
    assert calls[1][0] == ["/fixture/local/git", "-C", str(result), "checkout", "a" * 40]
    assert calls[2] == (result, ["remote-package-manager install"])
