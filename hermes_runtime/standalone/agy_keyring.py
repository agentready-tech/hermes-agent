"""Make only this credential generation available on a private Secret Service bus."""

from contextlib import contextmanager
import os
import secrets
import subprocess
import time

from providers.agy_policy import AgyError
from providers.agy_process import close_child


@contextmanager
def background(argv, env, *, password=None):
    process = subprocess.Popen(argv, env=env, start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               stdin=subprocess.PIPE if password is not None else subprocess.DEVNULL)
    try:
        if password is not None:
            process.stdin.write(password)
            process.stdin.close()
        yield process
    finally:
        close_child(process)


def unlock_password(store):
    path = store / "unlock"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_urlsafe(32).encode("ascii"))
    return path.read_bytes()


def ready(environment):
    result = subprocess.run(
        ["/usr/bin/dbus-send", "--session", "--print-reply", "--reply-timeout=300",
         "--dest=org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
         "string:org.freedesktop.secrets"],
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=1, check=False,
    )
    return result.returncode == 0 and b"boolean true" in result.stdout


def wait_for(predicate, process):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and process.poll() is None:
        if predicate():
            return
        time.sleep(0.03)
    raise AgyError("unavailable")


@contextmanager
def private_keyring(store, scratch):
    # There are no service directories in this configuration, so the bus cannot
    # auto-start desktop apps, helpers or an ambient keyring.
    socket = scratch / "bus"
    address = f"unix:path={socket}"
    config = scratch / "dbus.conf"
    config.write_text(
        f"<busconfig><type>session</type><listen>{address}</listen><auth>EXTERNAL</auth>"
        '<policy context="default"><allow own="org.freedesktop.secrets"/><allow own="org.gnome.keyring"/>'
        '<allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>',
        encoding="utf-8",
    )
    env = {"HOME": str(scratch), "XDG_DATA_HOME": str(store), "XDG_RUNTIME_DIR": str(scratch),
           "DBUS_SESSION_BUS_ADDRESS": address, "LANG": "C.UTF-8"}
    with background(["/usr/bin/dbus-daemon", "--nofork", f"--config-file={config}"], env) as bus:
        wait_for(socket.is_socket, bus)
        argv = ["/usr/bin/gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets",
                f"--control-directory={scratch / 'keyring'}"]
        with background(argv, env, password=unlock_password(store)) as keyring:
            wait_for(lambda: ready(env), keyring)
            yield address
