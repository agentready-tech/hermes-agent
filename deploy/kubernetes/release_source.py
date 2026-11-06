"""Export clean committed code and verify the complete application Python inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

MANIFEST = "agentsee-source-manifest.json"
PLUGIN_MANIFEST = "agentsee-plugin-manifest.json"
PLUGIN = "plugins/model-providers/claude-subscription-directsdk-experimental"
PLUGIN_REVISION = "2da429264671788748f69bca7d5a2216c1885b92"
MODULE_ROOTS = frozenset({"agent", "gateway", "tools", "hermes_runtime", "hermes_cli",
    "hermes_platform", "plugins", "cron", "tui_gateway", "pm", "acp_adapter", "docker", "deploy", "scripts"})


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, timeout=30).strip()


def source_path(name: str) -> bool:
    parts = Path(name).parts
    return name.endswith(".py") and (len(parts) == 1 or parts[0] in MODULE_ROOTS)


def digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected regular source file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export(root: Path, output: Path, revision: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or git(root, "rev-parse", "HEAD") != revision:
        raise ValueError("Build checkout must be the exact committed revision")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Refusing a dirty build checkout, including untracked files")
    git(root, "merge-base", "--is-ancestor", revision, "origin/main")
    output.mkdir(parents=True, exist_ok=False)
    archive = subprocess.Popen(["git", "-C", str(root), "archive", revision], stdout=subprocess.PIPE)
    try:
        subprocess.run(["tar", "-x", "-C", str(output)], stdin=archive.stdout, check=True, timeout=60)
    finally:
        archive.stdout.close()
    if archive.wait(timeout=60):
        raise RuntimeError("Git source export failed")
    names = git(root, "ls-tree", "-r", "--name-only", revision).splitlines()
    files = {name: digest(output / name) for name in names if source_path(name)}
    value = {"schemaVersion": 1, "sourceRevision": revision, "source": "clean-git-archive",
             "moduleRoots": sorted(MODULE_ROOTS), "files": files}
    (output / MANIFEST).write_text(json.dumps(value, sort_keys=True) + "\n")
    return value


def runtime_dependency(name: str, tool_roots: set[str]) -> bool:
    parts = Path(name).parts
    return len(parts) > 2 and parts[0] == "tools" and parts[1] in tool_roots


def dependency_roots(root: Path) -> set[str]:
    # PM-owned tool payloads are dependencies, not application module folders.
    facts = json.loads((root / "tools/facts.json").read_text())
    text = json.dumps(facts)
    roots = set()
    for path in (root / "tools").iterdir():
        if path.is_dir() and re.fullmatch(r"(?:python|node|npm|uv)-[0-9][A-Za-z0-9.+_-]*", path.name):
            if path.name not in text:
                raise ValueError(f"Unrecorded managed dependency: {path.name}")
            roots.add(path.name)
    return roots


def record_plugin(root: Path) -> dict:
    directory = root / PLUGIN
    files = {str(path.relative_to(root)): digest(path) for path in directory.rglob("*.py")}
    if not files:
        raise ValueError("Pinned official provider plugin is missing")
    value = {"repository": "NousResearch/hermes-official-plugins", "revision": PLUGIN_REVISION,
             "archiveSha256": "79b8aa8594ca03b44569646cca6a7bd36b22616224d8ad7e6f28d75ad43e3659", "files": files}
    (root / PLUGIN_MANIFEST).write_text(json.dumps(value, sort_keys=True) + "\n")
    return value


def verify(root: Path) -> dict:
    manifest = json.loads((root / MANIFEST).read_text())
    expected = dict(manifest["files"])
    plugin = root / PLUGIN_MANIFEST
    if plugin.exists():
        dependency = json.loads(plugin.read_text())
        if dependency["revision"] != PLUGIN_REVISION:
            raise ValueError("Provider plugin revision mismatch")
        expected.update(dependency["files"])
    for name, sha in expected.items():
        if digest(root / name) != sha:
            raise ValueError(f"Source checksum mismatch: {name}")
    tool_roots = dependency_roots(root)
    actual = {p.name for p in root.glob("*.py")}
    for name in manifest["moduleRoots"]:
        directory = root / name
        if directory.is_dir():
            actual.update(str(p.relative_to(root)) for p in directory.rglob("*.py")
                          if "__pycache__" not in p.parts and not runtime_dependency(str(p.relative_to(root)), tool_roots))
    extra = sorted(actual - expected.keys())
    if extra:
        raise ValueError(f"Unversioned application Python source: {extra}")
    stamp = json.loads((root / "install-stamp.json").read_text())
    if stamp["commit"] != manifest["sourceRevision"] or stamp.get("dirty"):
        raise ValueError("Source manifest and stamp must identify the same clean commit")
    return {"sourceRevision": manifest["sourceRevision"], "trackedPythonFiles": len(manifest["files"]),
            "verifiedPythonFiles": len(expected), "unexpectedPythonFiles": 0, "cleanSourceVerified": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "verify", "record-plugin"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    if args.operation == "export":
        value = export(args.root, args.output, args.revision)
        value = {"sourceRevision": value["sourceRevision"], "trackedPythonFiles": len(value["files"])}
    else:
        value = verify(args.root) if args.operation == "verify" else record_plugin(args.root)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
