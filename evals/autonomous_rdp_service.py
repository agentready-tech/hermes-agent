"""Disposable Linux xrdp proof: bootstrap without a viewer, reconnect twice, preserve history.

Requires root in a disposable container, the hermesrdp test account with
passwordless sudo, Xfce, Xvfb and xfreerdp3. The installer provisions the RDP
packages automatically. Never run on
a deployed host: it configures the container's real /etc/xrdp and starts daemons.
"""

import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import psutil

from hermes_platform.resolver import locate_command

ACCOUNT = "hermesrdp"
HOME = Path("/home/hermesrdp/data")
MARKER = "Hermes shared desktop proof"


def binary(name):
    result = locate_command(name).command
    if not result:
        raise RuntimeError(f"Missing proof dependency: {name}")
    return list(result)


def as_user(*args, timeout=90, **kwargs):
    try:
        return subprocess.run([*binary("runuser"), "-u", ACCOUNT, "--", *args],
            check=True, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.CalledProcessError as error:
        print(error.stdout, error.stderr, file=sys.stderr)
        raise


def action(name):
    value = as_user(sys.executable, "-m", "evals.autonomous_rdp_service", name)
    return json.loads(value.stdout.splitlines()[-1])


def wait_for(predicate, message):
    deadline = time.monotonic() + 30
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        threading.Event().wait(.1)


def stop(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)


def spawn(stack, folder, name, argv, **kwargs):
    log = stack.enter_context((folder / f"{name}.log").open("w"))
    process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    stack.callback(stop, process)
    return process


def seed():
    from hermes_state import SessionDB

    HOME.mkdir(exist_ok=True)
    (HOME / "config.yaml").write_text("# Keep my model\nmodel: proof-model\nmemory:\n  provider: holographic\n")
    with SessionDB(db_path=HOME / "state.db") as db:
        db.create_session("proof-main", source="cli", system_prompt="Keep the saved prefix")
        db.append_message("proof-main", "user", "Keep the ongoing work")
    return {"seeded": True}


def probe():
    from hermes_runtime.standalone.desktop_session import session_environment, xfce_sessions
    from hermes_state import SessionDB
    from PIL import ImageGrab

    os.environ.update(session_environment(HOME, {}))
    size = ImageGrab.grab(xdisplay=os.environ["DISPLAY"]).size
    tree = subprocess.run([*binary("xwininfo"), "-root", "-tree"], check=True, capture_output=True, text=True, timeout=10).stdout
    with SessionDB(db_path=HOME / "state.db") as db:
        assert db.get_session("proof-main")["system_prompt"] == "Keep the saved prefix"
        assert db.get_messages_as_conversation("proof-main")[0]["content"] == "Keep the ongoing work"
    sessions = xfce_sessions()
    assert len(sessions) == 1
    return {"display": os.environ["DISPLAY"], "size": size, "marker": MARKER in tree, "history": True}


def marker(stack, folder):
    from hermes_runtime.standalone.desktop_session import GUI_KEYS

    snapshot = json.loads((HOME / "autonomous" / "desktop-session.json").read_text())
    environment = {**os.environ, **{k: v for k, v in snapshot["environment"].items() if k in GUI_KEYS}}
    return spawn(stack, folder, "marker", [*binary("runuser"), "-u", ACCOUNT, "--", *binary("xmessage"),
        "-title", MARKER, "-buttons", "Keep:0", MARKER], env=environment)


def connect(stack, folder, environment, index):
    path = Path("/var/log/xrdp.log")
    offset = path.stat().st_size
    client = spawn(stack, folder, f"client-{index}", [*binary("xfreerdp3"), "/v:127.0.0.1", f"/u:{ACCOUNT}",
        "/p:Proof-only-42!", "/cert:ignore", "/sec:tls", "/size:1280x800", "/bpp:32", "/log-level:INFO"], env=environment)
    def connected():
        if client.poll() is not None:
            raise RuntimeError((folder / f"client-{index}.log").read_text())
        text = path.read_text()[offset:]
        return re.search(r"login successful|login was successful", text, re.IGNORECASE)
    wait_for(connected, "RDP client did not connect")
    return client


def prove():
    from evals.autonomous_desktop_service import display
    from hermes_platform.host.runtime import is_container

    if os.geteuid() != 0 or not is_container():
        raise RuntimeError("Run this proof only as root inside a disposable test container")
    subprocess.run([*binary("chpasswd")], input=f"{ACCOUNT}:Proof-only-42!\n", text=True, check=True, timeout=10)
    Path("/run/dbus").mkdir(exist_ok=True)
    subprocess.run([*binary("dbus-daemon"), "--system", "--fork"], check=True, timeout=10)
    Path("/run/xrdp").mkdir(exist_ok=True)
    runtime = Path("/run/user") / str(pwd.getpwnam(ACCOUNT).pw_uid)
    runtime.mkdir(parents=True, exist_ok=True)
    os.chown(runtime, pwd.getpwnam(ACCOUNT).pw_uid, pwd.getpwnam(ACCOUNT).pw_gid)
    runtime.chmod(0o700)
    action("seed")
    result = as_user(sys.executable, "-m", "hermes_runtime.standalone.cli", "--home", str(HOME),
        "install-service", "--rdp", timeout=1800)
    installed = json.loads(result.stdout)
    assert installed["rdp"]["backend"] == "Xorg"
    subprocess.run([*binary("systemd-analyze"), "verify", "--man=no", installed["rdp"]["unit"]], check=True, timeout=20)
    with tempfile.TemporaryDirectory(prefix="hermes-rdp-proof-") as temporary, ExitStack() as stack:
        folder = Path(temporary)
        spawn(stack, folder, "sesman", [*binary("xrdp-sesman"), "--nodaemon"])
        # xrdp-sesrun connects to this Unix socket, using the caller's real UID.
        wait_for(lambda: any(Path("/run/xrdp").rglob("*sesman*socket*")), "sesman socket missing")
        as_user(sys.executable, "-m", "hermes_runtime.standalone.cli", "--home", str(HOME), "rdp-start")
        before = action("probe")
        assert before["display"].startswith(":") and before["size"] == [1920, 1080]
        spawn(stack, folder, "xrdp", [*binary("xrdp"), "--nodaemon"])
        marker(stack, folder)
        wait_for(lambda: action("probe")["marker"], "marker window missing")
        gui = folder / "client-display"
        gui.mkdir()
        with display(gui) as screen:
            for index in range(2):
                client = connect(stack, folder, {**os.environ, **screen}, index)
                connected = action("probe")
                assert connected["display"] == before["display"] and connected["marker"]
                stop(client)
                assert action("probe")["marker"]
        reused = as_user(sys.executable, "-m", "hermes_runtime.standalone.cli", "--home", str(HOME), "rdp-start")
        assert json.loads(reused.stdout)["reused"]
        assert action("probe")["history"]
        return {"ok": True, "before_first_rdp": before, "rdp_reconnects": 2, "same_desktop": True, "history": True}


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "prove"
    if task != "prove":
        os.environ["HERMES_HOME"] = str(HOME)
    try:
        print(json.dumps({"seed": seed, "probe": probe, "prove": prove}[task]()))
    except BaseException:
        if task == "prove":
            for path in (Path("/var/log/xrdp.log"), Path("/var/log/xrdp-sesman.log")):
                if path.exists():
                    print(path.read_text()[-12000:], file=sys.stderr)
        raise
