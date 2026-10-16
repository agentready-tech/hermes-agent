"""Complete stopped-service snapshots, including Git and recovery checkpoints."""

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path

from agentready_runtime.standalone.config import read_config


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def mapped_path(value, mounts):
    path = Path(value)
    for mount in sorted(mounts, key=lambda m: len(m["target"]), reverse=True):
        target = Path(mount["target"])
        if path.is_relative_to(target):
            return Path(mount["source"]) / path.relative_to(target)
    return path


def validate_roots(mounts, *, home):
    """Refuse an incomplete backup before stopping anything or claiming success."""
    roots = [Path(m["source"]).resolve() for m in mounts]
    if any(a == b or a.is_relative_to(b) for i, a in enumerate(roots) for j, b in enumerate(roots) if i != j):
        raise ValueError("Persistent mount roots must not overlap")
    references = []
    state = Path(home) / "state.db"
    if state.exists():
        with sqlite3.connect(f"{state.as_uri()}?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='delegated_projects'").fetchone():
                for row in db.execute("SELECT workspace_json FROM delegated_projects"):
                    workspace = json.loads(row[0])
                    references.extend(v for k, v in workspace.items() if k == "cwd" and v)
                    references.extend(v for k, v in (workspace.get("worktree") or {}).items()
                                      if k in ("path", "repo_root", "cwd") and v)
    memory = read_config(home).get("plugins", {}).get("hermes-memory-store", {}).get("db_path")
    if memory:
        references.append(memory.replace("${HERMES_HOME}", str(home)).replace("$HERMES_HOME", str(home)))
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Persistent mount does not exist: {root}")
        for folder, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                path = Path(folder) / name
                if path.is_symlink():
                    references.append(str(path.resolve()))
                elif name == ".git" and path.is_file():
                    content = path.read_text().strip()
                    if content.startswith("gitdir:"):
                        gitdir = Path(content.split(":", 1)[1].strip())
                        references.append(str(gitdir if gitdir.is_absolute() else path.parent / gitdir))
    uncovered = []
    for value in references:
        path = mapped_path(str(Path(value).expanduser()), mounts).resolve()
        if not any(path.is_relative_to(root) for root in roots):
            uncovered.append(str(path))
    if uncovered:
        raise ValueError("Uncovered persistent paths; mount their containing directories with --mount HOST_PATH:CONTAINER_PATH: " + ", ".join(sorted(set(uncovered))))


def _add_tree(archive, root, name, scratch):
    from hermes_cli.backup_sqlite import _safe_copy_db

    def include(info):
        relative = Path(info.name).relative_to(name)
        if name == "root-0" and (str(relative) in ("gateway.pid", "cron.pid", ".backup.lock") or relative.parts[:1] == ("autonomous",)):
            return None
        if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
            return None
        source = root / relative
        # Snapshot all SQLite stores, regardless of their filename; exclude WAL
        # sidecars only when a corresponding real SQLite main file exists.
        if info.isfile() and source.name.endswith(("-wal", "-shm")):
            main = source.with_name(source.name.rsplit("-", 1)[0])
            if main.is_file() and _is_sqlite(main):
                return None
        if info.isfile() and _is_sqlite(source):
            temporary = scratch / uuid.uuid4().hex
            if not _safe_copy_db(source, temporary):
                raise RuntimeError(f"Could not snapshot SQLite store: {source}")
            saved = archive.gettarinfo(str(temporary), arcname=info.name)
            saved.mode, saved.uid, saved.gid, saved.mtime = info.mode, info.uid, info.gid, info.mtime
            with temporary.open("rb") as handle:
                archive.addfile(saved, handle)
            temporary.unlink()
            return None
        return info

    archive.add(root, arcname=name, recursive=True, filter=include)


def _is_sqlite(path):
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\x00"


def create(destination, deployment):
    """Caller must have stopped every writer of these mounted directories."""
    from utils import atomic_json_write
    destination = Path(destination)
    mounts = deployment["mounts"]
    validate_roots(mounts, home=Path(mounts[0]["source"]))
    if any(destination.resolve().is_relative_to(Path(m["source"]).resolve()) for m in mounts):
        raise ValueError("The backup directory must be outside all persistent mounts")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    archive_path = destination / "state.tar.gz"
    try:
        with tempfile.TemporaryDirectory(dir=destination) as temporary:
            with archive_path.open("xb") as output:
                os.chmod(archive_path, 0o600)
                with tarfile.open(fileobj=output, mode="w:gz", dereference=False) as archive:
                    for index, mount in enumerate(mounts):
                        _add_tree(archive, Path(mount["source"]), f"root-{index}", Path(temporary))
                output.flush()
                os.fsync(output.fileno())
        manifest = {"format": 1, "deployment": deployment, "sha256": digest(archive_path)}
        atomic_json_write(destination / "manifest.json", manifest, mode=0o600, fsync_dir=True)
        return manifest
    except BaseException:
        # An incomplete directory is retained for diagnosis but never has a manifest.
        (destination / "manifest.json").unlink(missing_ok=True)
        raise


def load(snapshot):
    snapshot = Path(snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    if manifest.get("format") != 1 or digest(snapshot / "state.tar.gz") != manifest["sha256"]:
        raise ValueError("Invalid or damaged autonomous snapshot")
    return manifest


def restore(snapshot):
    """Stage first, then swap directories; retain replaced data for manual recovery."""
    snapshot = Path(snapshot)
    manifest = load(snapshot)
    mounts = manifest["deployment"]["mounts"]
    staged, swapped = [], []
    suffix = uuid.uuid4().hex
    try:
        with tarfile.open(snapshot / "state.tar.gz", "r:gz") as archive:
            for index, mount in enumerate(mounts):
                root = Path(mount["source"])
                temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-restore-", dir=root.parent))
                staged.append(temporary)
                prefix = f"root-{index}"
                members = [m for m in archive.getmembers() if m.name == prefix or m.name.startswith(prefix + "/")]
                # These are locally produced, checksummed snapshots. Preserve Git
                # and ordinary absolute symlinks without following them on extraction.
                archive.extractall(temporary, members=members, filter="tar")
                replacement = temporary / prefix
                if not replacement.is_dir():
                    raise ValueError("Snapshot is missing a persistent root")
            for index, mount in enumerate(mounts):
                root = Path(mount["source"])
                previous = root.with_name(root.name + ".before-restore-" + suffix)
                if root.exists():
                    root.rename(previous)
                else:
                    previous = None  # A prior controller may have died between directory renames.
                swapped.append((root, previous))
                (staged[index] / f"root-{index}").rename(root)
    except BaseException:
        for root, previous in reversed(swapped):
            if root.exists():
                root.rename(root.with_name(root.name + ".failed-restore-" + suffix))
            if previous is not None:
                previous.rename(root)
        raise
    finally:
        for temporary in staged:
            shutil.rmtree(temporary)
    return manifest["deployment"]
