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

The application and publisher code stay in the Hermes fork. Run `.github/workflows/agentsee-hermes-release.yml` in the PRIVATE `agentready-tech/AgentReady` repository, passing the pushed immutable fork tag in `source_tag` and its exact full source SHA in `source_revision`. The workflow checks out that SHA from `agentready-tech/hermes-agent`, verifies the local and remote tag plus ancestry in `codex/agentready-runtime`, and invokes the fork's existing `deploy/kubernetes/publish_release.py`. Never pass a branch name or the AgentReady publisher SHA as the runtime source. The fork's former automatic tag publisher is removed so one tag cannot start a duplicate build.

The publisher exports a clean Git archive and builds the native Dockerfile from pinned OS stages. Dockerfile.agentready extends ONLY the source image built from that same source commit in that job, adding explicitly pinned dependencies. Private-package access and immutable-tag absence are checked before the heavy build and again before push.

Never use an existing official or previous AgentSee Hermes application image as a source base, copy selected patches onto it, or import a locally built image into production. Copying Git source over an installed application retains upstream-only and deleted modules. Dependency caches are acceptable; inherited application code is not.

Reject dirty or untracked source inputs. Verify the complete first-party Python file inventory against tagged Git source: changed, missing and unexpected modules fail publication. Pin additional provider plugins separately by repository revision and archive checksum. Completed fixes and all their source files must be included.

## Publication and rollout

Publish ONE private image to ghcr.io/agentready-tech/agentsee-hermes-runtime:VERSION. The package is owned by the PRIVATE AgentReady publisher, while OCI source labels and `sourceRevision` identify the Hermes fork. Keep `publisherRepository`/`publisherSourceRevision` separate from `sourceRepository`/`sourceRevision` in receipts. Check the exact existing package ID/name/owner, private visibility and private CI authority. GitHub may hide the package's administrative repository association from its workflow token; record that field as unavailable without blocking a valid image. An exposed conflicting association requires inspection. Never replace source-label, digest or actual full-pull verification with repository-association metadata.

Keep the registry digest, source revision, publication run and receipts together. CI verifies a complete private GHCR pull on its disposable runner and publishes only small release and source receipts. Preserve immutable tags and registry history for rollback. The historical public package migration and per-profile rollout scripts are archived procedures.

For production, run **Deploy Hermes** (`.github/workflows/deploy-hermes.yml` in `agentready-tech/AgentReady`, ref `main`). Pass the numeric ID of the successful canonical publisher run as `publish_run`. Dispatch in GitHub Actions or use:

```sh
gh workflow run deploy-hermes.yml --repo agentready-tech/AgentReady --ref main --field "publish_run=$PUBLISH_RUN_ID"
```

The workflow verifies publication and source receipts and updates only the common image of `agentsee-runtime/hermes-shared` on the current worker. Kubernetes pulls the digest directly from private GHCR with its scoped pull secret and `IfNotPresent`. The deploy command checks shared gateway activity, enrolled profiles, queues and human control under existing turn and desktop locks. It holds those locks until the old Pod exits. After checking the running digest and ordinary Pod readiness it updates the worker's future Hermes default. The worker keeps the current and previous Hermes images locally, with registry history and Kubernetes ControllerRevisions available for rollback.

When the workflow refuses a busy agent or queue, let the work finish and retry the same publication run. If deployment is interrupted, inspect its status and reconcile that target before starting a different release. Full event admission drain and rollout across multiple workers are future work.

Preserve agent homes, configs, credentials, history, queues and profiles. Use GitHub Actions for production releases. Avoid manual production rollout commands, workstation or host image builds and registry pushes, Mac image downloads, OCI relays or imports and new per-release backups. Runner registration and privilege provisioning on a new node are bootstrap. Agent data belongs to the existing backup system.

Respect the user's validation scope. Ordinary rollout readiness is separate from live model, mail, Telegram, browser, STT and Decision probes. Reconcile actual deployed image metadata in AgentReady and the Hermes fork following successful deployment. A source push and an image publication are separate outcomes from production deployment.

For adjacent releases, **Panel CI** publishes the Panel image and **Deploy Panel** takes its `digest`. **Deploy Firecracker worker** takes `revision`, a full AgentReady commit SHA reachable from `main`. The Sandbox publisher uses `agentsee-sandbox-vVERSION` from committed AgentReady source. There is no production deploy workflow for updating provisioner or for preparing and activating the common Firecracker snapshot. See `deploy/runtime-actions/README.md` in AgentReady for scope.

Monitor long-running Actions jobs sparsely. Follow the exact run and attempt, avoid duplicate launches, and notify the user when a run fails, completes or needs a decision.
