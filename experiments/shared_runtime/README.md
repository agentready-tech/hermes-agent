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

Completed local experiment: all four matched trials passed, including two real
sandboxes, native asynchronous persistent children, completion admitted into each
parent's main conversation, full process restart from the same directories, and a
new task sent to each existing child. A separate real OpenRouter probe passed one
bounded request per profile through the same shared native gateway.

See [the recorded result](result-2026.11.6.prototype.16.json) for exact source,
image, per-trial PSS/cgroup figures, usage, limitations and original proof hashes.

One process provides application-level tenant separation. It does not provide the
same crash or memory-corruption boundary as separate processes. Agent-controlled
tools must execute only in their own sandbox; a shared Docker/Kubernetes control
credential in the trusted host is outside the sandbox's authority.

Build images only from a clean committed snapshot of this experiment branch.
The canonical stable fork branch remains `codex/agentready-runtime`; plain `main`
continues to be the upstream baseline. Push only the prototype branch; keep all
prototype tags local because the stable workflow also matches their prefix.

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
Four trials reverse the shared/separate ordering. The paid provider probe is
separate from the scripted delegation workload; its two real requests verify
provider routing and independent main responses, not paid subagent delegation.

## Observed memory, version 16

Median of two trials per mode, each using seven PSS samples per phase. Both modes
used the same Linux ARM64 image and two runtime folders, tools and sandboxes.
PSS avoids counting shared resident pages twice. Controller/model-fixture memory
is excluded from runtime PSS; cgroup figures include that controller separately.

| Phase | One process, two runtimes | Two processes | Saved |
| --- | ---: | ---: | ---: |
| Idle | 261.11 MiB | 427.68 MiB | 166.57 MiB / 38.95% |
| After terminal/memory work | 330.60 MiB | 526.42 MiB | 195.81 MiB / 37.20% |
| Both persistent children running | 348.12 MiB | 553.08 MiB | 204.97 MiB / 37.06% |
| Children completed | 346.75 MiB | 551.47 MiB | 204.72 MiB / 37.12% |

With children running, the two shared trial medians were 330.18 and 366.06 MiB;
separate trials were 552.91 and 553.26 MiB. These are observed local workload
figures, not a production capacity promise. GUI/browser memory is excluded,
production AMD64 was not measured, and one process remains one failure domain.
The real-provider check used two calls total, one per profile (5 and 6 output tokens respectively); it did not run paid subagents. Temporary provider copies,
native homes and owned experiment containers/volume were removed after evidence
export/hash verification. Production agents were not changed.
