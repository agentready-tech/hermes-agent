"""Hosted Antigravity admission and fixed launch operations."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from hermes_constants import get_hermes_home
from providers.agy_policy import AgyError, BINARY, LOCAL_ENDPOINT, MODEL, PROVIDER, require_model


def admit(home, operation):
    """Only an operator-provisioned profile can launch Antigravity."""
    from hermes_runtime.standalone.agy_local_authority import authority_for

    if operation not in {"catalog", "login", "inference", "disconnect"}:
        raise AgyError("unavailable")
    return authority_for(home)


def child_environment(scratch):
    """Baseline for an already qualified scratch namespace, never inherited env."""
    root = Path(scratch)
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise AgyError("unavailable")
    return {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"), "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8"}


# Any qualified launcher must isolate the CLI's own auth diagnostics in scratch.
LOGIN_COMMAND = (BINARY, "acp", "--login")
PROBE_ANSWER = "AGE11_READY"
PROBE_PROMPT = f"Reply with exactly {PROBE_ANSWER} as plain text, with no tools."


@dataclass(frozen=True)
class StagedStore:
    """Opaque non-credential binding minted by the host for one staged generation."""

    generation: str
    profile: Path
    slot: str


def require_authority(authority, home, operation):
    if admit(home, operation) is not authority:
        raise AgyError("unavailable")
    return authority


def start_login(authority, home, stage):
    # spawn_staged must reject stale/foreign bindings before exec and bind the
    # full lease to the same private store for both operations.
    return require_authority(authority, home, "login").spawn_staged(home, stage, "login", LOGIN_COMMAND)


def verify_staged(authority, home, stage, login_pid, cancelled):
    """Require a real ACP answer from the staged keyring before promotion."""
    from providers.agy_process import close_child

    if cancelled():
        raise AgyError("cancelled")
    owner = require_authority(authority, home, "login")
    child = owner.spawn_staged(home, stage, "probe", None)
    if child.pid <= 0 or child.pid == login_pid:
        close_child(child)
        raise AgyError("unavailable")
    module = import_module("plugins.model-providers.gemini-antigravity")
    session = module.AcpTurn(stage, child)
    try:
        result = session.receive(cancelled, {})
        if result["type"] != "final" or result["answer"].strip() != PROBE_ANSWER:
            raise AgyError("protocol")
    finally:
        session.close()


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
    model = target_model or MODEL
    require_model(model)
    return {"provider": PROVIDER, "requested_provider": PROVIDER, "model": model,
            "base_url": LOCAL_ENDPOINT, "api_mode": "chat_completions", "api_key": "local-process",
            "source": "external-process"}
