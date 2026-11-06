# Shared Hermes runtime experiment

Local prototype only. No production deployment or change to the stable release.

The source of truth is each agent's directory: configuration, scoped credentials,
SQLite transcript/admission ledger, native memory and persistent subagent projects.
A running profile is a projection of that directory, not a separate copy of state.
The host owns an explicit set of directories; the native profile scope binds every
activity to one home and its configured sandbox. Sessions may have the same logical
`agentready_home` id because their databases differ. Process-wide route and lease
keys must additionally identify the owning home.

`worker.py` uses the existing native profile multiplexer and continuous AgentReady
conversation. Its fixture ingress is synthetic: no Telegram bot is connected.
`probe.py OUTPUT` starts the real gateway and exercises its authenticated native
WebSocket transport with two profiles, different synthetic credentials/models and
a concurrent inference barrier. Only the external model is scripted.

The first milestone checks the actual gateway, scoped credentials, parallel turns
and separate durable transcripts. The complete experiment still requires two real
local sandboxes, persistent asynchronous delegation and restart recovery, a bounded
real-provider run, and a Linux image memory comparison against two identical
separate gateway processes. Do not present this first milestone as that benchmark.

One process provides application-level tenant separation. It does not provide the
same crash or memory-corruption boundary as separate processes. Agent-controlled
tools must execute only in their own sandbox; a shared Docker/Kubernetes control
credential in the trusted host is outside the sandbox's authority.

Build images only from a clean committed snapshot of this experiment branch.
The canonical stable fork branch remains `codex/agentready-runtime`; plain `main`
continues to be the upstream baseline. Push is deferred while GitHub is unavailable.

`local_run.py OUTPUT --image agentsee/shared-runtime:VERSION` verifies a clean
checkout and the exact image revision, creates two labelled disposable sandboxes,
and dispatches the committed Linux `image_probe.py`. Only the trusted experiment
controller receives the local Docker socket. Each sandbox has no network and can
see only its own agent directory and workspace. The controller is excluded from
runtime PSS; its cgroup total is reported separately. No GUI/browser is included.

Fixture databases and agent directories live in one owned Docker Linux volume;
each sandbox mounts only its own home/workspace subdirectories. macOS shared folders
carry exported evidence after the processes stop, never live SQLite WAL/SHM state.
After the controller exits, `python -m experiments.shared_runtime.local_volume
export OUTPUT` preserves the state and verifies exported JSON hashes. The volume
is retained until its evidence is reviewed and its exact ownership is checked for cleanup.
The paid probe keeps native state in the disposable container's Linux filesystem;
removing that stopped container also removes its temporary credential-bearing homes.

The scripted inference run exercises actual native terminal, memory and persistent
delegation tools, blocked children with responsive parents, own-only list/read/stop,
completion delivery, then a full gateway process restart using the same folders.
Four trials reverse the shared/separate ordering. The paid provider probe remains
a separate acceptance item; neither a scripted model nor unit tests prove it.
