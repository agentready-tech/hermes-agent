"""Host-side Docker deployment, verified updates and matching image/data rollback."""

import hashlib
import json
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from hermes_runtime.standalone import snapshot


class Docker:
    def call(self, *args, input=None):
        result = subprocess.run(["docker", *args], input=input, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode:
            raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def image(self, reference):
        return self.call("image", "inspect", "--format", "{{.Id}}", reference)

    def preflight(self, image):
        self.call("run", "--rm", "--entrypoint", "/opt/hermes/.venv/bin/hermes-autonomous", image, "--help")

    def start(self, value):
        args = ["run", "-d", "--name", value["name"], "--restart", "unless-stopped", "--stop-timeout", "60",
                "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
                "--label", "hermes.autonomous=" + value["identity"],
                "-p", f"127.0.0.1:{value['port']}:9119", "-e", "HERMES_HOME=/opt/data"]
        for mount in value["mounts"]:
            args.extend(["--mount", f"type=bind,src={mount['source']},dst={mount['target']}"])
        self.call(*args, value["image"], "hermes-autonomous", "run", "--host", "0.0.0.0", "--port", "9119")

    def owned(self, value):
        actual = self.call("inspect", "--format", '{{index .Config.Labels "hermes.autonomous"}}', value["name"])
        if actual != value["identity"]:
            raise RuntimeError("Container ownership differs from the deployment manifest")

    def stop(self, value):
        self.owned(value)
        self.call("stop", "--time", "60", value["name"])

    def remove(self, value):
        self.owned(value)
        self.call("rm", value["name"])

    def healthy(self, value, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = subprocess.run(["docker", "exec", value["name"], "/opt/hermes/.venv/bin/hermes-autonomous",
                "status"], capture_output=True, text=True, timeout=10, check=False)
            if result.returncode == 0:
                return True
            threading.Event().wait(.5)
        return False


class Deployment:
    def __init__(self, directory, docker=None):
        self.directory, self.docker = Path(directory), docker or Docker()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self):
        from hermes_cli.backup import _backup_operation_lock
        with _backup_operation_lock(self.directory, timeout_seconds=0):
            if (self.directory / "transition.json").exists():
                raise RuntimeError("Interrupted update: run hermes-autonomous recover before another operation")
            yield

    def read(self):
        return json.loads((self.directory / "deployment.json").read_text())

    def write(self, name, value):
        from utils import atomic_json_write
        atomic_json_write(self.directory / name, value, mode=0o600, fsync_dir=True)

    def preparing(self, current):
        self.write("transition.json", {"snapshot": None, "candidate": current})

    def resume_previous(self, current):
        self.docker.call("start", current["name"])
        if not self.docker.healthy(current):
            raise RuntimeError("Previous container failed health; use recover")
        (self.directory / "transition.json").unlink(missing_ok=True)

    def backup_stopped(self, value):
        path = self.directory / "snapshots" / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8])
        snapshot.create(path, value)
        return path

    def up(self, home, image, port, mounts=()):
        with self.locked():
            if (self.directory / "deployment.json").exists():
                raise ValueError("Deployment already exists; use upgrade")
            home = Path(home).resolve()
            identity = hashlib.sha256(str(home).encode()).hexdigest()[:12]
            value = {"identity": identity, "name": "hermes-autonomous-" + identity, "port": port,
                     "image": self.docker.image(image),
                     "mounts": [{"source": str(home), "target": "/opt/data"}, *mounts]}
            home.mkdir(parents=True, exist_ok=True)
            snapshot.validate_roots(value["mounts"], home=home)
            if any(self.directory.resolve().is_relative_to(Path(m["source"]).resolve()) for m in value["mounts"]):
                raise ValueError("Deployment records must be outside persistent mounts")
            self.docker.preflight(value["image"])
            self.write("deployment.json", value)
            self.docker.start(value)
            if not self.docker.healthy(value):
                raise RuntimeError("Initial start failed health check; deployment and container logs retained")
            return value

    def backup(self):
        with self.locked():
            value = self.read()
            self.preparing(value)
            self.docker.stop(value)
            try:
                return str(self.backup_stopped(value))
            finally:
                self.resume_previous(value)

    def upgrade(self, image):
        with self.locked():
            old = self.read()
            candidate = {**old, "image": self.docker.image(image)}
            self.docker.preflight(candidate["image"])
            self.preparing(old)
            self.docker.stop(old)
            try:
                path = self.backup_stopped(old)
            except BaseException:
                self.resume_previous(old)
                raise
            self.write("transition.json", {"snapshot": str(path), "candidate": candidate})
            try:
                self.docker.remove(old)
                self.docker.start(candidate)
                if not self.docker.healthy(candidate):
                    raise RuntimeError("Candidate failed health check")
                self.write("deployment.json", candidate)
                self.write("last-update.json", {"snapshot": str(path), "previous_image": old["image"], "image": candidate["image"]})
                (self.directory / "transition.json").unlink()
            except BaseException:
                self._restore(path, candidate)
                raise
            return {"image": candidate["image"], "rollback_snapshot": str(path)}

    def _restore(self, path, current):
        # Inspect by exact name and ownership; a missing container is normal after
        # a crash between remove and create. Never remove an unrelated container.
        names = self.docker.call("ps", "-a", "--format", "{{.Names}}").splitlines()
        if current["name"] in names:
            self.docker.stop(current)
            self.docker.remove(current)
        previous = snapshot.restore(path)
        self.docker.start(previous)
        if not self.docker.healthy(previous):
            raise RuntimeError("Previous state restored, but health failed; transition retained for recover")
        self.write("deployment.json", previous)
        (self.directory / "transition.json").unlink(missing_ok=True)
        return previous

    def rollback(self, path):
        with self.locked():
            current = self.read()
            previous = snapshot.load(path)["deployment"]
            if (previous["identity"], previous["mounts"]) != (current["identity"], current["mounts"]):
                raise ValueError("Snapshot belongs to a different deployment")
            self.docker.preflight(previous["image"])
            self.preparing(current)
            self.docker.stop(current)
            try:
                rescue = self.backup_stopped(current)
            except BaseException:
                self.resume_previous(current)
                raise
            self.write("transition.json", {"snapshot": str(rescue), "candidate": previous})
            try:
                return self._restore(path, current)
            except BaseException:
                self._restore(rescue, previous)
                raise

    def recover(self):
        from hermes_cli.backup import _backup_operation_lock
        with _backup_operation_lock(self.directory, timeout_seconds=0):
            transition = json.loads((self.directory / "transition.json").read_text())
            if transition["snapshot"] is None:
                self.resume_previous(transition["candidate"])
                return transition["candidate"]
            return self._restore(transition["snapshot"], transition["candidate"])
