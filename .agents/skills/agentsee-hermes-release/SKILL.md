---
name: agentsee-hermes-release
description: Release the AgentSee Hermes fork through GitHub Actions and GHCR, preserving agent-owned state. Use for Hermes image builds, version publication and agent runtime rollouts.
---

# AgentSee Hermes releases

The application source is agentready-tech/hermes-agent. ONE common immutable release serves every agent. Configuration, credentials, providers, history, memory, sessions, browser profiles and sandbox selection belong to each agent's data directory, never to image variants.

## Source before image

Inspect all tracked and untracked changes. Identify their ownership and whether they have already been delivered. Never label a completed deployed fix as unrelated WIP and exclude it from future releases. Preserve genuinely unrelated work without claiming it is included. Resolve required source changes before building.

Commit and push completed release code, keep it on the fork's canonical `codex/agentready-runtime` branch, then create an immutable agentsee-vVERSION Git tag at that exact revision. The revision must be reachable from remote `codex/agentready-runtime`. This is the project's effective main branch. `main` is reserved for the upstream baseline and future upstream updates/rebases. Never merge our working branch into `main` or change the default branch to `main` when the user says merge to main; resolve that wording against this established fork convention. A temporary overlay or source checksum list does not replace the source commit.

## Canonical build

The application and publisher code stay in the Hermes fork. Run `.github/workflows/agentsee-hermes-release.yml` in the PRIVATE `agentready-tech/AgentReady` repository, passing the pushed immutable fork tag and its exact full source SHA. The workflow checks out that SHA from `agentready-tech/hermes-agent`, verifies the local and remote tag plus ancestry in `codex/agentready-runtime`, and invokes the fork's existing `deploy/kubernetes/publish_release.py`. Never pass a branch name or the AgentReady publisher SHA as the runtime source. The fork's former automatic tag publisher is removed so one tag cannot start a duplicate build.

The publisher exports a clean Git archive and builds the native Dockerfile from pinned OS stages. Dockerfile.agentready extends ONLY the source image built from that same source commit in that job, adding explicitly pinned dependencies. Private-package access and immutable-tag absence are checked before the heavy build and again before push.

Never use an existing official or previous AgentSee Hermes application image as a source base, copy selected patches onto it, or import a locally built image into production. Copying Git source over an installed application retains upstream-only and deleted modules. Dependency caches are acceptable; inherited application code is not.

Reject dirty or untracked source inputs. Verify the complete first-party Python file inventory against tagged Git source: changed, missing and unexpected modules fail publication. Pin additional provider plugins separately by repository revision and archive checksum. Completed fixes and all their source files must be included.

## Publication and rollout

Publish ONE private image to ghcr.io/agentready-tech/agentsee-hermes-runtime:VERSION. The package is owned by the PRIVATE AgentReady publisher, while OCI source labels and `sourceRevision` identify the Hermes fork. Keep `publisherRepository`/`publisherSourceRevision` separate from `sourceRepository`/`sourceRevision` in receipts. Check the exact existing package ID/name/owner, private visibility and private CI authority. GitHub may hide the package's administrative repository association from its workflow token; record that field as unavailable without blocking a valid image. An exposed conflicting association requires inspection. Never replace source-label, digest or actual full-pull verification with repository-association metadata.

Kubernetes authenticates through its scoped imagePullSecret. Record the registry manifest digest, source revision, CI run and source receipt. Retain the previous image for rollback. Never overwrite a version tag or build employee-specific images. Registry-copy recovery can publish the verified active image receipt immediately after its actual full private pull; historical copies must finish before retiring the old public package, and do not block that already verified active image's rollout.

The former public `agentsee-hermes` package is retired only after exact manifests have been copied to the private canonical package and all authorized agents/future defaults use private references. Preserve every historical release digest for rollback; copying an existing release must not rebuild it or change its source revision. Public package retirement is a separate reviewed final step, never part of the copy workflow.

Delivery is GitHub Actions → GHCR → Kubernetes. Set the common immutable digest on the existing Hermes StatefulSets with `imagePullPolicy: IfNotPresent`; kubelet/containerd downloads the image directly from GHCR. Use the centralized registry rollout operator in AgentReady, `deploy/kubernetes/rollout_hermes_registry.py`. Never download release images/OCI archives to the operator’s Mac, relay them through a workstation, manually preload/import them, or create extra release backup copies. Retain published versions and Kubernetes ControllerRevisions for rollback. Agent data backups belong to the existing backup system, not to each image rollout.

CI verifies a complete registry pull on its own disposable runner. Publish only small release/source receipts as artifacts, never image archives. Sandbox-owned code is built in the common sandbox image by AgentReady's `agentsee-sandbox-release.yml`, tagged `agentsee-sandbox-vVERSION`, from committed/pushed AgentReady main. An authorized sandbox feature rollout changes that StatefulSet's image at idle and necessarily restarts its container/browser; preserve its persistent profile, mounts and hard memory limit. Do not inject watchdog application source through Hermes exec/bootstrap.

Respect the user's validation scope. When production tests are excluded, do not run model, mail, Telegram, STT, Decision, browser or GUI probes, or repeat production audits. Ordinary rollout readiness is separate from exercising user functionality. Do required packaging checks before publication.

Update ONLY the authorized agents' Hermes image fields, sequentially at idle. Preserve all per-agent folders, configs, keys, providers, history, queues, sandbox/browser Pods, profiles and PVCs. Do not recreate sandboxes or restore databases. An uncertain update is inspected before retrying; resume from durable checkpoints.

Set the future-agent runtime default to the same digest. Commit and push actual deployed image metadata in AgentReady and Hermes. Source commits, registry publication, tests and completed rollout are distinct outcomes; do not claim one proves the others.

Monitor long-running CI/download/rollout jobs sparsely with durable checkpoints: one targeted check per heartbeat, no polling loops or duplicate launchers, notify only failure/completion/required action. Delete the monitor when complete.
