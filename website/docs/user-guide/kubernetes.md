---
title: Kubernetes terminal backend
description: Run terminal, browser and desktop tools in a platform-managed sandbox Pod.
---

# Kubernetes terminal backend

The `kubernetes` backend uses the Kubernetes API through `kubectl` to execute in
one existing sandbox Pod. The platform owns creation, images, resources, storage,
networking and restart policy. Hermes never creates, deletes or stops that Pod,
including on cleanup or idle timeout. Docker and other backends remain available.

```yaml
terminal:
  backend: kubernetes
  kubernetes_namespace: agentsee-mike
  kubernetes_pod: sandbox-0
  kubernetes_container: sandbox
  cwd: /workspace
  timeout: 180
```

Use `hermes setup terminal`, or set these keys through `hermes config set`.
An explicit namespace is required. The default Pod/container names are
`sandbox-0`/`sandbox`. Install a compatible `kubectl` in the **Hermes runtime**;
the sandbox needs bash and Python3. The optional `docker/Dockerfile.kubernetes`
adds the pinned client to an already-built fork image.

Inside Kubernetes use the runtime's projected ServiceAccount with only `get` on
the named Pod and `get`/`create` on that Pod's `pods/exec` subresource. No Pod
creation/deletion, Secret access, node access or cluster-admin role is needed.
RBAC scopes the Pod, not individual containers in it; keep the sandbox Pod under
platform control. Do not mount a ServiceAccount token into the sandbox. Outside
the cluster, supply a dedicated scoped `KUBECONFIG`; never forward that file or
token into terminal commands.

Commands retain cwd and exported shell state through the existing terminal
snapshot mechanism. stdin, file tools and binary streams use the same exec
transport. Commands and approved environment values travel in a framed stdin
payload instead of API request arguments. Kubernetes audit configuration can
still record transport metadata; avoid credential-logging auth plugins/proxies.
A timeout/interruption attempts to kill only that command's verified process
group, not the Pod. If API connectivity is lost, remote cancellation cannot be
guaranteed until access is restored.

Pod UID/container identity changes invalidate the old shell and screen marker;
there is no fallback onto the host or another client. Files/profile persistence
requires platform-provided volumes. Installed packages in a container's writable
layer are lost on replacement unless baked into the image or stored on volumes.

Native Bot Screen, browser/CDP and CUA use `kubectl exec` streams into the same
sandbox. No public VNC/CDP port is required. Desktop images need `runuser` and a
non-root desktop user (the shipped image uses `pn`). Chromium's sandbox stays
enabled; the platform must provide a compatible seccomp/AppArmor/user-namespace
policy. Computer installation preserves an explicitly configured Kubernetes
target; its default remains Docker.

For the inactive two-Pod deployment template, optional NetBird/TUN setup,
private control plane on Panel, preservation checks and rollback sequence, see
[`deploy/kubernetes/README.md`](https://github.com/agentready-tech/hermes-agent/blob/codex/agentready-runtime/deploy/kubernetes/README.md).
