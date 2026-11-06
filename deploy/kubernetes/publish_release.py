"""CI-only clean-source common Hermes image publication; never deploys agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

if __package__:
    from .release_source import export
else:
    from release_source import export

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/agentready-tech/agentsee-hermes"
PACKAGE = "orgs/agentready-tech/packages/container/" + IMAGE.rsplit("/", 1)[1]


def output_root() -> Path:
    return Path(os.environ["RUNNER_TEMP"]) / "agentsee-hermes-release"


def run(args: list[str], *, data: bytes | None = None, log: str | None = None) -> bytes:
    if log:
        with (output_root() / log).open("ab") as stream:
            subprocess.run(args, input=data, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=5400)
        return b""
    result = subprocess.run(args, input=data, capture_output=True, check=False, timeout=600)
    if result.returncode:
        detail = result.stdout + result.stderr
        token = os.environ.get("GH_TOKEN", "").encode()
        if token:
            detail = detail.replace(token, b"[REDACTED]")
        failure = output_root() / "failure.private.log"
        failure.write_bytes(detail[-20000:])
        failure.chmod(0o600)
        raise RuntimeError(f"Release command failed: {args[0]} {args[1]}")
    return result.stdout


def phase(value: str, **details) -> None:
    (output_root() / "status.json").write_text(json.dumps({"phase": value, **details}, indent=2))
    print(value, flush=True)


def prepare(revision: str, version: str) -> Path:
    if os.environ.get("GITHUB_REPOSITORY") != "agentready-tech/hermes-agent":
        raise ValueError("Only the Hermes fork owns this image")
    if os.environ.get("GITHUB_REF_NAME") != "agentsee-v" + version:
        raise ValueError("Release version must identify the immutable Git tag")
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.[1-9][0-9]*", version):
        raise ValueError("Use the AgentSee YYYY.M.D.N release version")
    source = output_root() / "source"
    export(ROOT, source, revision)
    run(["python3", str(ROOT / "scripts/write_install_stamp.py"), "--output", str(source / "install-stamp.json"),
         "--commit", revision, "--branch", "codex/agentready-runtime", "--display-version", version, "--source", "ci",
         "--distribution", "docker", "--update-mechanism", "external"])
    return source


def build(source: Path, revision: str, version: str) -> tuple[str, dict]:
    native = "agentsee-native-source:" + revision
    candidate = IMAGE + ":" + version
    phase("building_native_source", sourceRevision=revision)
    run(["docker", "build", "--platform", "linux/amd64", "--target", "runtime", "--file", str(source / "Dockerfile"),
         "--tag", native, str(source)], log="native-build.log")
    phase("building_common_release")
    run(["docker", "build", "--platform", "linux/amd64", "--file", str(source / "Dockerfile.agentready"),
         "--build-arg", "HERMES_SOURCE_IMAGE=" + native, "--build-arg", "HERMES_GIT_SHA=" + revision,
         "--build-arg", "AGENTSEE_RELEASE_VERSION=" + version, "--tag", candidate, str(source)], log="release-build.log")
    phase("verifying_source_inventory")
    result = run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--read-only",
                  "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                  "--entrypoint", "/opt/hermes/.venv/bin/python", candidate,
                  "/opt/hermes/deploy/kubernetes/release_source.py", "verify", "/opt/hermes"])
    proof = json.loads(result)
    if proof["sourceRevision"] != revision or not proof["cleanSourceVerified"]:
        raise ValueError("Built image does not match the release source")
    labels = json.loads(run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", candidate]))
    if labels["org.opencontainers.image.revision"] != revision or labels["org.opencontainers.image.version"] != version:
        raise ValueError("Image labels do not identify this release")
    (output_root() / "source-proof.json").write_text(json.dumps(proof, indent=2))
    return candidate, proof


def registration(package: dict) -> dict:
    if ((package.get("repository") or {}).get("full_name") != "agentready-tech/hermes-agent"
            or package.get("visibility") not in {"private", "public", "internal"}):
        raise ValueError("Release package must be linked to the fork with known visibility")
    return {"packageVisibility": package["visibility"], "sourceRepository": "agentready-tech/hermes-agent"}


def publish(candidate: str) -> tuple[str, dict]:
    token = os.environ["GH_TOKEN"].encode()
    username = os.environ["GITHUB_ACTOR"]
    auth = output_root() / "auth"
    auth.mkdir(mode=0o700)
    os.environ["DOCKER_CONFIG"] = str(auth)
    os.environ["REGISTRY_AUTH_FILE"] = str(auth / "registry-auth.json")
    run(["docker", "login", "ghcr.io", "--username", username, "--password-stdin"], data=token)
    run(["skopeo", "login", "--username", username, "--password-stdin", "ghcr.io"], data=token)
    package_check = subprocess.run(["gh", "api", PACKAGE], capture_output=True, check=False, timeout=120)
    if package_check.returncode:
        if b"HTTP 404" not in package_check.stderr:
            raise ValueError("Cannot establish whether the release package exists")
    else:
        package = json.loads(package_check.stdout)
        registration(package)
        absent = subprocess.run(["skopeo", "inspect", "docker://" + candidate], capture_output=True, check=False, timeout=120)
        if not absent.returncode or not any(x in absent.stderr.decode(errors="replace").lower()
                                           for x in ("manifest unknown", "name unknown", "404", "not found")):
            raise ValueError("The immutable version tag must be provably absent")
    phase("publishing_registry_image")
    run(["docker", "push", candidate], log="push.log")
    digest = run(["skopeo", "inspect", "--format", "{{.Digest}}", "docker://" + candidate]).decode().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Registry did not return an immutable manifest digest")
    package = json.loads(run(["gh", "api", PACKAGE]))
    return IMAGE + "@" + digest, registration(package)


def archive(image: str) -> tuple[Path, str]:
    phase("verifying_registry_pull")
    layout = output_root() / "oci"
    run(["skopeo", "copy", "--all", "--preserve-digests", "--dest-oci-accept-uncompressed-layers",
         "docker://" + image, "oci:" + str(layout) + ":release"], log="registry-pull.log")
    index = json.loads((layout / "index.json").read_text())
    if len(index["manifests"]) != 1 or index["manifests"][0]["digest"] != image.split("@", 1)[1]:
        raise ValueError("Registry roundtrip changed the published manifest")
    for blob in (layout / "blobs/sha256").iterdir():
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != blob.name:
                raise ValueError("Registry blob checksum mismatch")
    phase("archiving_published_release")
    target = output_root() / "hermes-release.oci.tar"
    with tarfile.open(target, "w") as output:
        for path in sorted(layout.iterdir()):
            output.add(path, arcname=path.name)
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return target, digest


def main() -> None:
    os.umask(0o077)
    output_root().mkdir(exist_ok=False)
    revision = os.environ["GITHUB_SHA"]
    version = os.environ["GITHUB_REF_NAME"].removeprefix("agentsee-v")
    phase("exporting_committed_fork")
    source = prepare(revision, version)
    candidate, proof = build(source, revision, version)
    image, package = publish(candidate)
    bundle, checksum = archive(image)
    receipt = {"phase": "complete", "version": version, "gitTag": "agentsee-v" + version,
               "sourceRevision": revision, "image": image, "tag": candidate, "platform": "linux/amd64",
               "workflowRunId": int(os.environ["GITHUB_RUN_ID"]), "workflowAttempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
               "commonRuntimeForAllAgents": True, "fullSourceBuild": True,
               "private": package["packageVisibility"] == "private", **package,
               "registryFullPullVerified": True, "sourceProof": proof, "archive": bundle.name,
               "archiveSha256": checksum, "archiveBytes": bundle.stat().st_size,
               "productionTestsPerformed": False, "agentConfigurationIncluded": False}
    (output_root() / "release-proof.json").write_text(json.dumps(receipt, indent=2))
    phase("complete", image=image, version=version)


if __name__ == "__main__":
    main()
