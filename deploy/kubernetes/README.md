# Agentsee: Kubernetes preparation

This is an **inactive migration plan**, not a provisioner or an automatic cutover.
The additional terminal backend attaches to an existing Pod; Docker, SSH and other
backends remain available. No production cluster has been installed by this change.

## Placement and installation gates

| Host | Private address | Role |
| --- | --- | --- |
| `agent-ready` (Panel) | `10.216.0.1` | K3s server/control plane, no customer workloads |
| `hermes-vm` | `10.216.0.2` | Worker, Hermes Pod + desktop sandbox Pod per client |

Use `k3s-panel.yaml` and `k3s-worker.yaml` as `/etc/rancher/k3s/config.yaml`
on the respective hosts. Choose and verify a pinned K3s release in the tested
Kubernetes minor (1.35); use the same release on both nodes. Keep join tokens in
a separate root-only `token-file`, never in these files or Git. The server is
tainted `NoSchedule`; only system workloads may tolerate that taint. Traefik and
ServiceLB are disabled so existing Caddy/Panel retain ports 80/443. K3s network
policy enforcement must remain enabled.

Do not install before these gates pass:

* Capacity: on 2026-10-31 Panel had only about **952 MiB MemAvailable**, 6 GB total,
  no swap. Reserve at least 2 GiB for the control plane plus measured Panel peak
  headroom before installation. This is a capacity gate, not proof of sufficiency.
  Do not stop Panel workloads, resize servers or order VMs implicitly. Worker
  requests/limits also need sizing against observed browser/VPN usage; current
  per-container upper limits can exceed node RAM in aggregate.
* Compare `10.240.0.0/16` (Pods), `10.241.0.0/16` (Services) and `10.216.0.0/24`
  (management) with Docker, host and **NetBird container** routes. Choose different
  CIDRs before installation if they overlap. Validate routes inside the VPN, not
  only on the host.
* Save root-only firewall/routing snapshots on both hosts. Keep UFW enabled and
  preserve SSH, Caddy, WireGuard and existing desktop access. Allow API TCP6443
  only from worker/private management, UDP8472 only between nodes over
  `wg-agentsee`, and kubelet TCP10250 only between private nodes. Never expose
  VXLAN, kubelet, API, NodePorts or desktop ports publicly. Permit only required
  CNI forwarding. Recheck external reachability after installation: Docker and
  Kubernetes forwarding can bypass ordinary UFW INPUT rules.
* Extend the existing `AS-MGMT`/`DOCKER-USER` management restriction to CNI traffic
  in the actual forwarding path. Allow only Hermes workload IPs to Panel443 and
  API6443; block sandbox/other tenant access to node/control services. Kubernetes
  NetworkPolicy alone does not secure node-local/host traffic. Retain the old
  Docker rules through rollback. Reconcile these rules on Pod IP changes before
  allowing customer workloads; a fixed one-time Pod IP rule is insufficient.

This stage does not implement Panel scheduling, node enrolment, VM creation,
autoscaling, HA or a custom Kubernetes operator. The existing private network and
Panel ownership/connector configuration are retained.

## Images, data and inactive manifests

Build the fork normally, then optionally add the pinned API client:

```sh
docker build -f docker/Dockerfile.kubernetes \
  --build-arg HERMES_IMAGE=private-registry/hermes@sha256:REPLACE \
  -t private-registry/hermes-kubernetes:RELEASE .
```

The base must contain this backend. `kubectl` 1.35.8 is copied from the official
multiarchitecture image by digest; keep its minor within the API server's
supported version skew. A real init binary is also copied from the pinned
official Docker image: Kubernetes does not provide Compose's `init: true`.
Both workloads use `/usr/local/bin/agentsee-init` to reap children and forward
signals; the fork's legacy `/usr/bin/tini` is an s6 compatibility shim, not this
binary. Do not install kubectl into the sandbox. Extend the private sandbox
snapshot using `docker/Dockerfile.kubernetes-sandbox`, supplying immutable
`HERMES_IMAGE` (the extension above) and `SANDBOX_IMAGE` build arguments; this
copies only the init binary, with no Hermes files or client credentials.

For the existing client, render with **real immutable digests** (the placeholders
below intentionally fail validation). The renderer only writes JSON to stdout:

```sh
python deploy/kubernetes/render.py \
  --client mike --worker hermes-vm --root /srv/agentsee/mike \
  --home /root/.hermes --panel-ip 10.216.0.1 --vpn \
  --hermes-image private-registry/hermes-kubernetes@sha256:REPLACE \
  --sandbox-image private-registry/mike-sandbox@sha256:REPLACE > plan.json
```

Both StatefulSets have **replicas=0**. There is no Service, Ingress, hostPort,
NodePort, Docker socket or CRI socket. Stable targets are `hermes-0` and
`sandbox-0` in namespace `agentsee-mike`. The local PV uses `Retain`, is pre-bound
to this client's claim and tied to worker label `agentsee.io/worker=hermes-vm`.
The 50 Gi capacity declaration does not allocate, limit or back up the filesystem;
check free space and actual data size. Both Pods must stay on this worker until
storage is deliberately migrated. This is not cross-node automatic recovery.

Expected root-owned bundle on that worker (0700 parent):

```text
/srv/agentsee/mike/
  home/                         # cold copy of complete /root/.hermes
  external/ssh/
    config
    known_hosts_gitlab
    slotegrator_gitlab_ed25519
```

Keep all UIDs, modes, symlinks, ACLs and xattrs. The three SSH files are the
current external binds of both live containers; inventory again before cutover
and add any newly introduced external binds. Do not copy arbitrary host dirs.
Hermes mounts the complete home; sandbox receives only the **existing** selected
subdirectories and the three read-only GitLab files. In particular:

* `sandboxes/docker/default/home` → `/root`;
* `sandboxes/docker/default/desktop-home` → `/home/pn`, including Chromium profile
  `.hermes/bot-desktop/browser-profile`;
* `sandboxes/docker/default/workspace` → `/workspace`;
* main `workspace` → `/root/.hermes/workspace` (a **different directory**);
* `desktop-machine-id` remains `/etc/machine-id` read-only;
* `.netbird/lib` in sandbox root remains also mounted at `/var/lib/netbird`.

The old sandbox's installed writable image layer is also data. At the cold stage,
save a **private** image snapshot with `docker commit` + `docker save`; mounts are
not included in that image and must be copied separately. Never push a user's
snapshot to a public registry. For a single worker it can be imported into
K3s/containerd locally and referenced by its immutable digest. Preserve its
original image/security/mount metadata and image archive for rollback.

## Chromium and optional VPN

Chromium keeps its own sandbox. No `--no-sandbox`, `SYS_ADMIN`, host networking,
host PID namespace or privileged tenant container is introduced. Desktop tools
use `kubectl exec` followed by `runuser -u pn`; do not replace the desktop's user
with root. The target image must contain bash, Python3, runuser and the native
desktop/browser tools. Memory-backed `/dev/shm` remains 1 GiB.

Install a **resolved OCI seccomp** profile on the worker at the kubelet's
`seccomp/agentsee/chromium.json` before activation (normally under
`/var/lib/kubelet`). For the existing sandbox, the resolved profile was found at:

```text
/run/containerd/io.containerd.runtime.v2.task/moby/CONTAINER_ID/config.json
```

Read that file in a root-only script and write **only** its `linux.seccomp` object
to the new profile. Never output the full file (it includes environment).
Do not blindly copy Docker's `HostConfig.SecurityOpt` seccomp JSON: Docker/Moby
`includes`/`excludes`/`archMap` are not resolved OCI rules. Reject such fields in
the kubelet profile. Preserve AppArmor enforcement and verify `pn` can create the
needed user namespace on this worker without opening broader host capabilities.

VPN is optional for new clients. `--vpn` enables only NET_ADMIN/NET_RAW and a
`devic.es/tun` request, keeping drop-ALL/no-new-privileges. Install the pinned
`tun-device-plugin.yaml` on VPN-enabled workers first. It advertises only TUN,
not arbitrary `/dev` devices; the plugin is trusted node infrastructure with
access to the kubelet device-plugin socket. Merely mounting `/dev/net/tun` as a
hostPath is not proof of device-cgroup permission. Check allocation and a real
TUN ioctl in a disposable Pod before using client data.

For the current client, the main sandbox process is the existing
`/root/.local/bin/netbird service run --log-file /root/.netbird/log/client.log`.
Saved `/var/lib/netbird` profiles reconnect; no `netbird up`, host VPN installation
or new SSO login is scripted. Normal SSO/session expiry may still require a user
reauthorization. Verify corporate DNS/routes after switching; VPN routing stays
inside the sandbox's network namespace. No host NetBird is reintroduced.

Read-only preflight on 2026-10-31 found the **existing** sandbox's daemon in
`NeedsLogin`. Reauthorize and verify Management/Signal connectivity before
capturing a migration baseline; retained state alone does not establish a live
VPN session. Both original container IDs and StartedAt remained unchanged.

## Cold cutover procedure (later operational stage)

1. Complete capacity, network, device, image and security gates. Smoke-test the
   **full runtime image** using an isolated home, fake tokens and no outbound
   network; never a second real Telegram poller. Validate manifests with server
   dry-run, then apply them with replicas still zero. Local test-cluster schema
   validation does not prove production firewall or browser compatibility.
2. Record a private baseline: exact container IDs/StartedAt, mounts/security,
   main session/head, SQLite integrity and row counts, hashes of config/auth,
   vault decryptability **without values**, cron/inbox, workspace/skills/files,
   Chromium profile and NetBird state. Retain a complete original cold backup.
3. Quiesce Hermes gracefully, disable its automatic restart, wait for in-flight
   tools and persistence. Close Chromium gracefully through its native process,
   then stop NetBird/desktop and the **existing sandbox**. This is the first
   service interruption and is not part of preparation. Never run old and new
   pollers, profile writers or NetBird identities concurrently.
4. With all writers stopped, make an exclusive timestamped backup; checkpoint
   SQLite and verify integrity. Copy the entire home and external binds into a
   **fresh** bundle using `rsync -aHAX --numeric-ids`, then byte-compare. Snapshot
   and archive the stopped sandbox image. Never overwrite a previous backup.
5. Only in the staged copy: save original config, set `terminal.backend` to
   `kubernetes`, namespace `agentsee-mike`, Pod `sandbox-0`, container `sandbox`;
   keep `cwd`, computer/browser/Panel settings and immutable `agentready_home`.
   Preserve a pending-message policy that does not discard Telegram updates.
   Remove obsolete `DOCKER_HOST` from the new workload environment, not old
   rollback config. No session reset, history conversion or new agent identity.
6. Remove only copied ephemeral `SingletonLock`, `SingletonSocket`,
   `SingletonCookie` and `DevToolsActivePort` after confirming the old browser
   stopped. Keep all auth cookies, Login Data, sessions and preferences. A Pod
   hostname differs from the Docker hostname; stale singleton links otherwise
   prevent Chromium opening its own preserved profile. Old screen PID/socket
   markers may be discarded only in this staged copy.
7. Start only sandbox (scale to 1), verify device access, NetBird identity and
   Management/Signal connected. Then start only Hermes (scale to 1). Verify
   native ready status, **same** main conversation and old messages, vault
   decryptability, Telegram polling, real terminal/files, Chromium sandbox and
   existing profile. Test native browser/CDP, CUA and Panel Bot Screen against
   this same Pod. Do not send synthetic Telegram messages or create schedules.
   The user verifies a real task and delivery.
8. Verify actual enforcing NetworkPolicies with a temporary unrelated client:
   no other-tenant/node/API/Panel access from sandbox, only scoped exec from
   Hermes, permitted internet/VPN flows and corporate DNS. Test public ports
   externally on both hosts. Store a redacted production proof, leave original
   containers/Compose and backups stopped and intact through acceptance.

**Rollback:** stop both Pods and wait for writers to exit; checkpoint/validate the
latest bundle. Copy **newly written state** back into the old bind locations
while old containers remain stopped; restore only saved Docker-specific config
keys and ephemeral markers. Do not restore an old SQLite DB over new messages.
Retain both complete snapshots for reconciliation if validation differs. Start
the original sandbox with its unchanged image/mounts/security and then the old
Compose runtime. Verify the same acceptance gates. Do not delete the Retain PV,
original image, bundle or backups as part of switching.

## References

Validation for this preparation used a disposable kind cluster on Kubernetes
1.35.8: real exec/stdin/exit codes, exported state, files/binary streams, loopback
forwarding, process-group cancellation, denied cross-Pod/Secret access, a real
TUN ioctl without privileged mode, and in-cluster ServiceAccount auth with a
read-only client root. The rendered plan passed API server dry-run. Existing
config defaults and Docker/SSH compatibility tests also passed. This does not
verify the future production CNI/firewall, Chromium profile migration or a live
corporate VPN session in Kubernetes.

Reproduce the opt-in API tests only against a disposable cluster:

```sh
HERMES_TEST_KUBECONFIG=/path/to/disposable-kubeconfig \
HERMES_TEST_IMAGE=preloaded-kubectl-client-image \
  scripts/run_tests.sh tests/tools/test_kubernetes_backend_real.py -q
```

Preload `python:3.11-slim` and the optional client image. Install the optional TUN
plugin for its device test; absent optional prerequisites are reported as skips.

* [Kubernetes named-resource RBAC, including pods/exec](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
* [NetworkPolicy semantics and limitations](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
* [K3s networking and policy controller](https://docs.k3s.io/networking/networking-services)
* [Generic device plugin](https://github.com/squat/generic-device-plugin/tree/generic-device-plugin-0.2.0)
