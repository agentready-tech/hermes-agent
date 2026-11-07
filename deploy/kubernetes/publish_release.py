"""CI-only clean-source common Hermes image publication; never deploys agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

if __package__:
    from .promote_private_runtime import verify_package
    from .release_source import export, git
else:
    from promote_private_runtime import verify_package
    from release_source import export, git

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/agentready-tech/agentsee-hermes-runtime"
PACKAGE = "orgs/agentready-tech/packages/container/" + IMAGE.rsplit("/", 1)[1]
SOURCE_REPOSITORY = "agentready-tech/hermes-agent"
PUBLISHER_REPOSITORY = "agentready-tech/AgentReady"


def output_root() -> Path:
    return Path(os.environ["RUNNER_TEMP"]) / "agentsee-hermes-release"


def record_failure(detail: bytes) -> None:
    token = os.environ.get("GH_TOKEN", "").encode()
    if token:
        detail = detail.replace(token, b"[REDACTED]")
    failure = output_root() / "failure.private.log"
    failure.write_bytes(detail[-20000:])
    failure.chmod(0o600)


def run(args: list[str], *, data: bytes | None = None, log: str | None = None) -> bytes:
    if log:
        with (output_root() / log).open("ab+") as stream:
            try:
                subprocess.run(args, input=data, stdout=stream, stderr=subprocess.STDOUT, check=True, timeout=5400)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 20000))
                record_failure(stream.read())
                raise
        return b""
    result = subprocess.run(args, input=data, capture_output=True, check=False, timeout=600)
    if result.returncode:
        record_failure(result.stdout + result.stderr)
        raise RuntimeError(f"Release command failed: {args[0]} {args[1]}")
    return result.stdout


def phase(value: str, **details) -> None:
    (output_root() / "status.json").write_text(json.dumps({"phase": value, **details}, indent=2))
    print(value, flush=True)


def release_request() -> tuple[str, str]:
    """Keep private CI authority separate from the immutable, pushed source fork tag."""
    if os.environ.get("GITHUB_REPOSITORY") != PUBLISHER_REPOSITORY:
        raise ValueError("The private AgentReady workflow owns registry publication")
    revision = os.environ.get("AGENTSEE_SOURCE_REVISION", "")
    tag = os.environ.get("AGENTSEE_SOURCE_TAG", "")
    match = re.fullmatch(r"agentsee-v([0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\.[1-9][0-9]*)", tag)
    if not match or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("An immutable AgentSee release tag and full source revision are required")
    if git(ROOT, "config", "--get", "remote.origin.url") not in (
            "https://github.com/" + SOURCE_REPOSITORY, "https://github.com/" + SOURCE_REPOSITORY + ".git"):
        raise ValueError("Release source must be the canonical Hermes fork")
    ref = "refs/tags/" + tag
    if git(ROOT, "rev-parse", "HEAD") != revision or git(ROOT, "rev-parse", ref + "^{commit}") != revision:
        raise ValueError("Build checkout and release tag must identify the same commit")
    advertised = {name: sha for sha, name in
                  (line.split() for line in git(ROOT, "ls-remote", "origin", ref, ref + "^{}").splitlines())}
    if advertised.get(ref + "^{}", advertised.get(ref)) != revision:
        raise ValueError("The immutable release tag must already be pushed at the exact source commit")
    git(ROOT, "merge-base", "--is-ancestor", revision, "origin/codex/agentready-runtime")
    return revision, match[1]


def prepare(revision: str, version: str) -> Path:
    if release_request() != (revision, version):
        raise ValueError("Publication inputs changed before source export")
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
    if (labels["org.opencontainers.image.revision"] != revision or labels["org.opencontainers.image.version"] != version
            or labels.get("org.opencontainers.image.source") != "https://github.com/" + SOURCE_REPOSITORY):
        raise ValueError("Image labels do not identify this release")
    (output_root() / "source-proof.json").write_text(json.dumps(proof, indent=2))
    return candidate, proof


def registration(package: dict) -> dict:
    verify_package(package, PUBLISHER_REPOSITORY)
    return {"packageVisibility": package["visibility"], "sourceRepository": SOURCE_REPOSITORY,
            "packagePublisherRepository": PUBLISHER_REPOSITORY, "packageId": package["id"],
            "packageRepositoryAssociationAvailable": bool((package.get("repository") or {}).get("full_name"))}


def registry_preflight(candidate: str) -> None:
    publisher = json.loads(run(["gh", "api", "repos/" + PUBLISHER_REPOSITORY]))
    if publisher.get("private") is not True:
        raise ValueError("Publication requires the private publisher repository")
    token = os.environ["GH_TOKEN"].encode()
    username = os.environ["GITHUB_ACTOR"]
    auth = output_root() / "auth"
    auth.mkdir(mode=0o700, exist_ok=True)
    os.environ["DOCKER_CONFIG"] = str(auth)
    os.environ["REGISTRY_AUTH_FILE"] = str(auth / "registry-auth.json")
    run(["docker", "login", "ghcr.io", "--username", username, "--password-stdin"], data=token)
    run(["skopeo", "login", "--username", username, "--password-stdin", "ghcr.io"], data=token)
    package_check = subprocess.run(["gh", "api", PACKAGE], capture_output=True, check=False, timeout=120)
    if package_check.returncode:
        raise ValueError("The private canonical package must already exist before any release build")
    registration(json.loads(package_check.stdout))
    absent = subprocess.run(["skopeo", "inspect", "docker://" + candidate], capture_output=True, check=False, timeout=120)
    if not absent.returncode or not any(x in absent.stderr.decode(errors="replace").lower()
                                       for x in ("manifest unknown", "name unknown", "404", "not found")):
        raise ValueError("The immutable version tag must be provably absent")


def publish(candidate: str) -> tuple[str, dict]:
    registry_preflight(candidate)
    phase("publishing_registry_image")
    run(["docker", "push", candidate], log="push.log")
    digest = run(["skopeo", "inspect", "--format", "{{.Digest}}", "docker://" + candidate]).decode().strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Registry did not return an immutable manifest digest")
    package = json.loads(run(["gh", "api", PACKAGE]))
    return IMAGE + "@" + digest, registration(package)


def verify_registry_pull(image: str, candidate: str) -> dict:
    if not candidate.startswith(IMAGE + ':') or image.split('@', 1)[0] != IMAGE:
        raise ValueError('Registry verification requires the canonical versioned registry reference')
    phase("verifying_registry_pull")
    layout = output_root() / "oci"
    run(["skopeo", "copy", "--all", "--preserve-digests", "--dest-oci-accept-uncompressed-layers",
         "docker://" + image, "oci:" + str(layout) + ":" + candidate], log="registry-pull.log")
    index = json.loads((layout / "index.json").read_text())
    if (len(index["manifests"]) != 1 or index["manifests"][0]["digest"] != image.split("@", 1)[1]
            or index['manifests'][0].get('annotations', {}).get('org.opencontainers.image.ref.name') != candidate):
        raise ValueError("Registry roundtrip changed the published manifest")
    for blob in (layout / "blobs/sha256").iterdir():
        with blob.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != blob.name:
                raise ValueError("Registry blob checksum mismatch")
    return {'registryFullPullVerified': True, 'delivery': 'kubernetes-native-ghcr-pull',
            'imageArchivePublished': False}


def main() -> None:
    os.umask(0o077)
    output_root().mkdir(exist_ok=False)
    revision, version = release_request()
    phase("verifying_private_publication_access")
    registry_preflight(IMAGE + ":" + version)
    phase("exporting_committed_fork")
    source = prepare(revision, version)
    candidate, proof = build(source, revision, version)
    image, package = publish(candidate)
    pull_proof = verify_registry_pull(image, candidate)
    receipt = {"phase": "complete", "version": version, "gitTag": "agentsee-v" + version,
               "sourceRevision": revision, "image": image, "tag": candidate, "platform": "linux/amd64",
               "workflowRunId": int(os.environ["GITHUB_RUN_ID"]), "workflowAttempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
               "publisherRepository": PUBLISHER_REPOSITORY, "publisherSourceRevision": os.environ["GITHUB_SHA"],
               "commonRuntimeForAllAgents": True, "fullSourceBuild": True,
               "private": package["packageVisibility"] == "private", **package,
               **pull_proof, "sourceProof": proof,
               "productionTestsPerformed": False, "agentConfigurationIncluded": False}
    (output_root() / "release-proof.json").write_text(json.dumps(receipt, indent=2))
    phase("complete", image=image, version=version)


if __name__ == "__main__":
    main()
