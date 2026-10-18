"""Linux X11 login-hook proof using real authenticated displays and GLib's desktop-entry launcher.

Run from an installed checkout: python evals/autonomous_desktop_service.py.
Requires Xvfb, Pillow, libX11 and libgio; it does not install or restart any host service.
"""

import ctypes
import json
import os
import selectors
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from hermes_platform.resolver import locate_command


@contextmanager
def display(folder):
    cookie = os.urandom(16)
    fields = (b"", b"", b"MIT-MAGIC-COOKIE-1", cookie)
    authority = folder / "Xauthority"
    authority.write_bytes(struct.pack("!H", 65535) + b"".join(struct.pack("!H", len(v)) + v for v in fields))
    authority.chmod(0o600)
    read_fd, write_fd = os.pipe()
    command = locate_command("Xvfb").command
    if not command:
        raise RuntimeError("This proof requires Xvfb")
    process = subprocess.Popen([*command, "-displayfd", str(write_fd), "-screen", "0", "800x600x24",
        "-nolisten", "tcp", "-auth", str(authority)], pass_fds=(write_fd,), stdout=subprocess.DEVNULL)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as stream, selectors.DefaultSelector() as selector:
            selector.register(stream, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                raise TimeoutError("Xvfb did not become ready")
            number = stream.readline().strip()
        assert number.isdigit()
        yield {"DISPLAY": ":" + number, "XAUTHORITY": str(authority), "XDG_CURRENT_DESKTOP": "XFCE",
               "XDG_SESSION_TYPE": "x11"}
    finally:
        process.terminate()
        process.wait(timeout=10)


def launch_hook(path, environment):
    gio = ctypes.CDLL("libgio-2.0.so.0")
    pointer = ctypes.c_void_p
    gio.g_desktop_app_info_new_from_filename.argtypes = [ctypes.c_char_p]
    gio.g_desktop_app_info_new_from_filename.restype = pointer
    gio.g_app_launch_context_new.restype = pointer
    gio.g_app_launch_context_setenv.argtypes = [pointer, ctypes.c_char_p, ctypes.c_char_p]
    gio.g_app_info_launch.argtypes = [pointer, pointer, pointer, ctypes.POINTER(pointer)]
    gio.g_app_info_launch.restype = ctypes.c_int
    gio.g_object_unref.argtypes = [pointer]
    app = gio.g_desktop_app_info_new_from_filename(os.fsencode(path))
    assert app, "GLib refused the generated .desktop file"
    context = gio.g_app_launch_context_new()
    try:
        for name, value in environment.items():
            gio.g_app_launch_context_setenv(context, name.encode(), value.encode())
        error = pointer()
        assert gio.g_app_info_launch(app, None, context, ctypes.byref(error)), "GLib could not launch the Xfce hook"
    finally:
        gio.g_object_unref(context)
        gio.g_object_unref(app)


def prove():
    from hermes_runtime.standalone import desktop_session, service

    with tempfile.TemporaryDirectory(prefix="hermes-x11-") as temporary:
        root = Path(temporary)
        home = root / 'main data $ " %'
        home.mkdir()
        marker = home / "history-kept"
        marker.write_text("saved session and recovery state")
        hook = root / "hermes-login.desktop"
        hook.write_text(service.render_autostart(home, sys.executable))
        binary_dir = root / "bin"
        binary_dir.mkdir()
        control = binary_dir / "systemctl"
        control.write_text("#!/bin/sh\nprintf 'disabled\\n'\nexit 1\n")
        control.chmod(0o700)
        screenshots = []
        for index in range(2):
            folder = root / str(index)
            folder.mkdir()
            with display(folder) as gui:
                launch = {**os.environ, **gui, "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
                          "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
                old = desktop_session.snapshot_path(home)
                if old.exists():
                    old.unlink()
                launch_hook(hook, launch)
                deadline = time.monotonic() + 20
                while not old.exists() and time.monotonic() < deadline:
                    threading.Event().wait(.05)
                assert old.exists(), "Xfce Exec did not preserve the home path or capture its environment"
                effective = desktop_session.session_environment(home, {})
                assert effective["XAUTHORITY"] == gui["XAUTHORITY"]
                child_env = {**os.environ, **effective}
                result = subprocess.run([sys.executable, "-c", "from PIL import ImageGrab; import os,json; "
                    "im=ImageGrab.grab(xdisplay=os.environ['DISPLAY']); print(json.dumps(list(im.size)))"],
                    env=child_env, capture_output=True, text=True, check=True, timeout=10)
                assert json.loads(result.stdout) == [800, 600]
                screenshots.append(gui["XAUTHORITY"])
                stored = json.loads(old.read_text())
                stored["boot_time"] -= 1
                old.write_text(json.dumps(stored))
                assert "DISPLAY" not in desktop_session.session_environment(home, {})
        assert screenshots[0] != screenshots[1]
        assert marker.read_text() == "saved session and recovery state"
        return {"ok": True, "authenticated_x11_logins": 2, "glib_autostart": True, "previous_boot_rejected": True}


if __name__ == "__main__":
    print(json.dumps(prove()))
