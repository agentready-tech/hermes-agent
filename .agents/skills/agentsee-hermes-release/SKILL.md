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

Use .github/workflows/agentsee-release.yml in the fork. It exports a clean Git archive and builds the native Dockerfile from pinned OS stages. Dockerfile.agentready extends ONLY the source image built from that same commit in that job, adding explicitly pinned dependencies.

Never use an existing official or previous AgentSee Hermes application image as a source base, copy selected patches onto it, or import a locally built image into production. Copying Git source over an installed application retains upstream-only and deleted modules. Dependency caches are acceptable; inherited application code is not.

Reject dirty or untracked source inputs. Verify the complete first-party Python file inventory against tagged Git source: changed, missing and unexpected modules fail publication. Pin additional provider plugins separately by repository revision and archive checksum. Completed fixes and all their source files must be included.

## Publication and rollout

Publish ONE image to ghcr.io/agentready-tech/agentsee-hermes:VERSION. Record its registry manifest digest, source revision, CI run and source receipt. Retain the previous image for rollback. Never overwrite a version tag or build employee-specific images.

Respect the user's validation scope. When production tests are excluded, do not run model, mail, Telegram, STT, Decision, browser or GUI probes, or repeat production audits. Ordinary rollout readiness is separate from exercising user functionality. Do required packaging checks before publication.

Update ONLY the authorized agents' Hermes image fields, sequentially at idle. Preserve all per-agent folders, configs, keys, providers, history, queues, sandbox/browser Pods, profiles and PVCs. Do not recreate sandboxes or restore databases. An uncertain update is inspected before retrying; resume from durable checkpoints.

Set the future-agent runtime default to the same digest. Commit and push actual deployed image metadata in AgentReady and Hermes. Source commits, registry publication, tests and completed rollout are distinct outcomes; do not claim one proves the others.

Monitor long-running CI/download/rollout jobs sparsely with durable checkpoints: one targeted check per heartbeat, no polling loops or duplicate launchers, notify only failure/completion/required action. Delete the monitor when complete.
