"""Configure the native Docker desktop sandbox without installing a host desktop."""

import io
from pathlib import Path


def configure(home):
    from hermes_yaml import roundtrip_yaml
    from tools.bot_desktop.sandbox_host import SANDBOX_IMAGE_HINT
    from utils import atomic_write_text

    path = Path(home) / "config.yaml"
    yaml = roundtrip_yaml()
    config = yaml.load(path.read_text())
    for section in ("autonomous", "terminal", "browser", "bot_desktop"):
        if not isinstance(config.get(section, {}), dict):
            raise ValueError(f"{section} must be a mapping")
    autonomous = config.setdefault("autonomous", {})
    computer = autonomous.setdefault("computer", {})
    if not isinstance(computer, dict):
        raise ValueError("autonomous.computer must be a mapping")
    backup = path.with_suffix(".yaml.before-computer")
    if not backup.exists():
        atomic_write_text(backup, path.read_text(), mode=0o600)
    computer["enabled"] = True
    # Old host browser directories stay on disk; they must not pin a host executable in the sandbox.
    for key in ("browser_executable", "browser_profile"):
        computer.pop(key, None)
    autonomous["rdp"] = False
    config.setdefault("terminal", {}).update({
        "backend": "docker", "docker_image": SANDBOX_IMAGE_HINT,
        "container_persistent": True, "docker_persist_across_processes": True,
    })
    config.setdefault("browser", {}).update({"headed": True, "cdp_url": ""})
    desktop = config.setdefault("bot_desktop", {})
    desktop.update({"auto_start": True, "idle_stop_minutes": 0, "placement": "auto"})
    desktop.setdefault("geometry", "1600x900")
    output = io.StringIO()
    yaml.dump(config, output)
    atomic_write_text(path, output.getvalue(), mode=0o600)
    return {"backend": "docker", "image": SANDBOX_IMAGE_HINT, "config_backup": str(backup)}


def install(home):
    """Select Hermes' shipped sandbox; the native backend creates/reuses its container."""
    return configure(home)


def start_desktop():
    from tools.bot_desktop import placement, runtime

    where = placement.resolve()
    if where.backend != "docker" or where.where != placement.TERMINAL:
        raise ValueError("Computer mode requires the Docker sandbox; run install-service --computer")
    return runtime.start()


def run(home):
    """Start the native sandbox screen at boot and leave its lifecycle to Hermes."""
    import threading
    start_desktop()
    threading.Event().wait()
