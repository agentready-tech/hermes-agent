"""Local, profile-owned configuration for the autonomous service."""

import io
import secrets
from pathlib import Path

from hermes_yaml import roundtrip_yaml, safe_load


def read_config(home):
    path = Path(home) / "config.yaml"
    value = safe_load(path.read_text()) if path.exists() else {}
    if value is not None and not isinstance(value, dict):
        raise ValueError("config.yaml must contain a mapping")
    return value or {}


def settings(home):
    return read_config(home).get("autonomous", {})


def initialize(home):
    """Add service defaults without replacing any existing user configuration."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from utils import atomic_write_text

    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    yaml = roundtrip_yaml()
    config = yaml.load(path.read_text()) if path.exists() else {}
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a mapping")
    if not path.exists():
        config["_config_version"] = DEFAULT_CONFIG["_config_version"]
    autonomous = config.setdefault("autonomous", {})
    if not isinstance(autonomous, dict):
        raise ValueError("autonomous must be a mapping in config.yaml")
    for key, value in {"enabled": True, "host": "127.0.0.1", "port": 9119,
                       "gmail": {"enabled": False, "query": "in:inbox", "poll_seconds": 60,
                                 "allowed_senders": []}}.items():
        autonomous.setdefault(key, value)
    # Persist these defaults for native consumers that read raw YAML, while
    # retaining any policy the operator explicitly selected for this home.
    for section, key in (("approvals", "mode"), ("display", "show_reasoning")):
        values = config.setdefault(section, {})
        if isinstance(values, dict):
            values.setdefault(key, DEFAULT_CONFIG[section][key])
    existing_state = (home / "state.db").exists() or (home / "memory_store.db").exists()
    existing_notes = any(p.stat().st_size for p in (home / "memories").glob("*.md"))
    if not existing_state and not existing_notes:
        memory = config.setdefault("memory", {})
        if isinstance(memory, dict):
            memory.setdefault("provider", "holographic")
    if not existing_state:
        # A portable standalone default also works on Docker Desktop bind mounts.
        # Native Hermes supports DELETE journals for weak-fsync/shared filesystems.
        database = config.setdefault("database", {})
        if isinstance(database, dict):
            database.setdefault("journal_mode", "delete")
    output = io.StringIO()
    yaml.dump(config, output)
    atomic_write_text(path, output.getvalue(), mode=0o600)
    # Native auth, model and channel secrets all stay in the same .env.
    env = home / ".env"
    from dotenv import dotenv_values
    existing = dotenv_values(env) if env.exists() else {}
    defaults = {"HERMES_DASHBOARD_SESSION_TOKEN": secrets.token_urlsafe(32),
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "hermes",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": secrets.token_urlsafe(24)}
    missing = {k: v for k, v in defaults.items() if k not in existing}
    if missing:
        previous = env.read_text() if env.exists() else ""
        atomic_write_text(env, previous.rstrip("\n") + "\n" + "".join(f"{k}={v}\n" for k, v in missing.items()), mode=0o600)
    for directory in ("workspace", "logs", "skills", "scripts"):
        (home / directory).mkdir(exist_ok=True)
    return config


def activate_home(home):
    from hermes_runtime.adapters.hermes import activate
    from hermes_runtime.adapters.state import ensure_home_session
    activate(Path(home).resolve())
    ensure_home_session()


def child_environment(home):
    from tools.environments.local import served_profile_child_env
    return served_profile_child_env(target_home=home, inherit_credentials=True)
