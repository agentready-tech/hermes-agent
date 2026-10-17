# AgentReady managed Hermes fork

Upstream baseline: NousResearch/hermes-agent `main`,
commit `53bec2a311979ad6f77580ba491eca261ab988e0` (2026-10-16).
Managed runtime: `0.12.0`.

Run managed processes with `python -m hermes_runtime serve ...` or
`python -m hermes_runtime gateway run ...`. `--ensure-home` initializes the
existing home session. Ordinary Hermes entrypoints do not activate the adapter.
One managed process owns one home; hooks are disabled for other profile homes.

## Ownership

- `hermes_runtime/sessions.py`, `events.py`, `checkpoints.py` and `contracts.py`
  define AgentReady policies without importing Hermes or receiving engine objects.
- `adapters/hermes.py` explicitly activates the owning home. `adapters/state.py`
  translates its logical session into existing Hermes SQLite lineage.
- `adapters/turns.py` owns the file lock and Unix mailbox. Native hooks in
  `TurnFacadeMixin`, `InterruptControlMixin` and the full tool dispatcher invoke
  it directly. The existing Hermes model/tool loop still executes the turn.
- `adapters/compaction.py` binds the summary-only reducer to the existing auxiliary
  route and owns cancellation, stale-attempt checks and compressor state.
  `adapters/checkpoint_files.py` preserves atomic archive and index formats.
- `adapters/tui.py` and `adapters/gateway.py` translate native ingress and preserve
  control/approval handling. `session_event.py` implements the managed RPC client
  shared by cron and Gmail. No runtime class/function monkeypatch is installed.

Tool groups finish before queued events enter the next LLM request. Events arriving
while a final answer is produced cause a continuation in the same owned session.
The Unix inbox records accepted messages in the same `state.db` before acknowledging
them and retires their payloads only after admission into canonical history. This
does not promise exactly-once external effects. After bind, managed `serve` restores
the home through its native WebSocket resume path without requiring a human client.

## Persistent project delegates

The main agent creates project children on demand with `delegate_task(tasks=[{
"project": "stable-task-key", "goal": "...", "context": "..."}])`. The existing tool's
`send`, `read`, `list`, `stop` and `archive` actions manage those identities. Children
retain their native sessions, workspaces and evidence history; per-project SQLite
inboxes serialize follow-ups. Native async completion delivery reports results back
to the main conversation. Dead owners leave recoverable messages, completed native
answers are not executed again, and explicit stops remain paused until a new send.
No additional service or database is required.

The fork retains its enhanced bundled Holographic provider and its regression tests.
Upstream moved the stock provider to the external catalog; that migration must not
replace the fork's sourced facts, revision history or background consolidation.

## Native capabilities and independent sessions

AgentReady does not prohibit manual `/new`, `/reset`, `/resume` or `/branch`.
The managed home is the default business-event conversation; native branches,
resets and delegated children retain their own session IDs and execution loops.
Only actual compression continuations belong to the managed home lineage.

## Failed-compaction recovery

Managed compaction has a bounded emergency path when semantic summarization fails,
times out, or leaves a request above the model window. A fresh fenced attempt
archives the complete source transcript, then commits a compact retrieval handoff
and the latest user event through Hermes' existing in-place transaction. The full
history remains in SQLite and the workspace archive; no session is rotated. Large
current events are referenced verbatim in the archive rather than clipped.

The emergency path makes no LLM request. It budgets the system prompt, tool schemas
and output reservation, retains the current complete tail when it fits, and directs
the model to verify archived outcomes before repeating external actions. Explicit
cancellation never starts recovery. Archive/commit failure preserves the original
context and reports failure; it never installs a pointer to an unwritten archive.
Gateway's upstream compression-exhaustion reset is disabled for the managed home.

## Build and verify

`Dockerfile.agentready` overlays the complete fork source on the pinned upstream image,
preserving its entrypoint, built UI, Python 3.14 and SQLite. It rebuilds the pinned
environment through upstream PM and binds the source through the native assembler.
AgentReady's release image downloads this fork
by exact commit and archive checksum, adds managed Office dependencies, and is
referenced by immutable image digest in production.
Pass the fork revision when building the overlay: native session tool pins use this
identity, recorded in the canonical install stamp, to refresh changed tool contracts
after an update while keeping the session.

```sh
docker build --build-arg HERMES_GIT_SHA="$(git rev-parse HEAD)" -f Dockerfile.agentready -t agentready-hermes:local .
scripts/run_tests.sh tests/hermes_runtime/
```

If macOS Keychain rejects the Docker credential helper, use a separate public-build
Docker config with `{"auths":{"https://index.docker.io/v1/":{}}}` and pass its
directory through `docker --config`. An entirely empty config auto-detects
`osxkeychain` again. Keep the regular Docker config and saved credentials intact.
Both the native install stamp and immutable image provenance identify the fork
commit supplied at build time.

The AgentReady repository's `smoke:hermes-runtime` with
`AGENTREADY_HERMES_TEST_IMAGE` exercises real native steer and compaction hooks in
isolated containers, with controlled model/tool responses, plus multiprocess
ownership/failure and settings validation. No real model-quality claim is made.

## Upstream updates and rollback

Keep `upstream` pointing to NousResearch/hermes-agent. Rebase the complete fork
series onto a reviewed upstream snapshot, resolve the small native call sites, run policy,
adapter and image-backed AgentReady tests, then pin the new fork commit in
AgentReady. Do not overwrite the managed branch with upstream or copy AIAgent.

Before switching an existing agent, stop writers and back up its complete home
and Environment state. Keep the previous image. Rollback restores the previous
image together with its matching state backup. Session IDs, settings schema,
memories, workspace and checkpoint files remain unchanged by this port.

## Standalone operation

This fork can run without the AgentReady application. See [AUTONOMOUS.md](AUTONOMOUS.md)
for local setup, supervised startup, main-session events/schedules, Gmail and
matching image/data update rollback. The existing managed entrypoint remains supported.
