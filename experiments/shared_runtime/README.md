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
