"""Hosted Antigravity admission boundary. This release has no live launcher."""

from importlib import import_module
from pathlib import Path

from hermes_constants import get_hermes_home
from providers.agy_policy import AgyError, BINARY, LOCAL_ENDPOINT, MODEL, PROVIDER, SETTINGS


def admit(home, operation):
    """Return a scoped authority only after G1/G2; no user configuration can do this.

    The offline suite substitutes this function in its isolated interpreter.
    Presence of the immutable binary, credentials or model config is not admission.
    """
    raise AgyError("unavailable")


def print_command(prompt):
    return (BINARY, "--model", MODEL, "--output-format", "stream-json", "--disable-slash-commands",
            "--settings", SETTINGS, "--log-file", "/dev/null", "--print-timeout", "120s", "-p", prompt)


def child_environment(scratch):
    """Baseline for an already qualified scratch namespace, never inherited env."""
    root = Path(scratch)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise AgyError("unavailable")
    return {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8"}


# Any qualified launcher must isolate the CLI's own auth diagnostics in scratch.
LOGIN_COMMAND = (BINARY, "acp", "--login")


def profile_for(home):
    from agent.safe_worker_policy import safe_worker_enabled
    if safe_worker_enabled():
        raise AgyError("unavailable")
    authority = admit(home, "inference")
    generation = authority.active(home)
    if generation is None:
        raise AgyError("auth_required")
    module = import_module("plugins.model-providers.gemini-antigravity")
    return module.AgyProfile(home, authority, generation)


def runtime(target_model):
    profile_for(get_hermes_home())
    if target_model and target_model != MODEL:
        raise AgyError("unavailable")
    return {"provider": PROVIDER, "requested_provider": PROVIDER, "model": MODEL,
            "base_url": LOCAL_ENDPOINT, "api_mode": "chat_completions", "api_key": "local-process",
            "source": "external-process"}


def standalone_selection(selection, session_key, requested_model, requested_provider):
    """Validate a native apply precondition (not an auth credential or a route override)."""
    import re
    from hermes_cli.config import read_user_config_raw
    from hermes_runtime.sessions import HOME_SESSION_ID
    if (session_key != HOME_SESSION_ID or requested_model != MODEL or requested_provider != PROVIDER
            or not isinstance(selection, dict) or set(selection) != {"revision", "generation"}):
        raise AgyError("unavailable")
    revision, generation = selection["revision"], selection["generation"]
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{32}", revision) or not isinstance(generation, str):
        raise AgyError("unavailable")
    home = get_hermes_home()
    config = read_user_config_raw(home / "config.yaml")
    actual_revision = (config.get("autonomous") or {}).get("panel", {}).get("model_selection_revision")
    model = config.get("model")
    if model != {"provider": PROVIDER, "default": MODEL} or actual_revision != revision:
        raise AgyError("unavailable")
    if admit(home, "inference").active(home) != generation:
        raise AgyError("auth_required")
    return {**selection, "session_key": session_key, "model": MODEL, "provider": PROVIDER}
