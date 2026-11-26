"""Build the browser CLI payload for the sandbox image's system Python."""

from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


def locked_requirements(root: Path) -> str:
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    pending = ["browser-harness", "psutil"]
    requirements = {}
    while pending:
        name = pending.pop()
        if name in requirements:
            continue
        matches = [p for p in packages if p["name"] == name and "registry" in p["source"]]
        if len(matches) != 1 or not matches[0].get("wheels"):
            raise ValueError(f"Sandbox browser needs one wheel-locked version of {name}")
        package = matches[0]
        hashes = " ".join("--hash=" + wheel["hash"] for wheel in package["wheels"])
        requirements[name] = f'{name}=={package["version"]} {hashes}'
        pending.extend(dependency["name"] for dependency in package.get("dependencies", []))
    return "\n".join(requirements[name] for name in sorted(requirements)) + "\n"


def build(root: Path, output: Path, uv: str, python: str) -> None:
    with tempfile.TemporaryDirectory(prefix="agentsee-browser-build-") as temp:
        directory = Path(temp)
        requirements = directory / "requirements.txt"
        requirements.write_text(locked_requirements(root))
        payload = directory / "payload"
        subprocess.run([uv, "pip", "install", "--python", python, "--target", str(payload),
                        "--only-binary", ":all:", "--no-deps", "--require-hashes",
                        "--requirement", str(requirements)], check=True)
        tools = payload / "tools"
        tools.mkdir()
        (tools / "__init__.py").touch()
        for name in ("browser_tab_harness.py", "browser_tab_resources.py", "browser_sandbox_lease.py"):
            shutil.copy2(root / "tools" / name, tools / name)
        subprocess.run([python, "-I", "-B", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        "import browser_harness.run, psutil, PIL; "
                        "from tools.browser_tab_harness import managed_tabs",
                       str(payload)], check=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as bundle:
            for path in sorted(payload.iterdir()):
                bundle.add(path, arcname=path.name)


if __name__ == "__main__":
    import sys

    build(Path(__file__).resolve().parents[2], Path(sys.argv[1]), sys.argv[2], sys.argv[3])
