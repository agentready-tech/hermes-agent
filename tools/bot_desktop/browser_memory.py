"""Reapply native Memory Saver when an ephemeral desktop sandbox starts again."""
import inspect
import json
import logging
import subprocess
from pathlib import Path

POLICY = {"HighEfficiencyModeEnabled": True, "MemorySaverModeSavings": 1}
ROOTS = ("etc/opt/chrome/policies/managed", "etc/opt/chrome_for_testing/policies/managed",
         "etc/chromium/policies/managed")


def install(root=Path("/")):
    for relative in ROOTS:
        directory = Path(root) / relative
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "agentsee-memory.json"
        data = json.dumps(POLICY, sort_keys=True) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8-sig") != data:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(data, encoding="utf-8")
            temporary.chmod(0o644)
            temporary.replace(path)


def configure(env):
    """Exec-default/root writes only policy; the browser keeps running as pn."""
    from tools.environments import streams
    code = ("import json\nfrom pathlib import Path\nPOLICY=" + repr(POLICY) + "\nROOTS=" + repr(ROOTS)
            + "\n" + inspect.getsource(install) + "\ninstall()\n")
    try:
        result = streams.run_in(env, ["python3", "-c", code], timeout=15)
        if result.returncode == 0:
            return True
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass
    logging.getLogger(__name__).warning("Native browser Memory Saver needs a writable managed-policy directory")
    return False
