"""Render an INACTIVE two-Pod migration plan; never contact Docker or Kubernetes.

The bundle preserves the existing desktop Docker layout. Copy data only during
the runbook's cold cutover; replicas=0 prevents duplicate pollers/VPN identities.
"""

import argparse
import json
from pathlib import PurePosixPath
import re


def object_(kind, name, api="v1", **fields):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name}, **fields}


def mount(source, destination, readonly=False):
    return {"name": "data", "subPath": source, "mountPath": destination, "readOnly": readonly}


def workload(name, container, namespace):
    return object_("StatefulSet", name, "apps/v1", spec={
        "replicas": 0, "serviceName": name,
        "selector": {"matchLabels": {"app": name}},
        "template": {"metadata": {"labels": {"app": name}}, "spec": {
            "nodeSelector": {"agentsee.io/worker": "hermes-vm"},
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 60,
            "containers": [container],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}}],
        }}})


def render(*, client, worker, root, home, hermes_image, sandbox_image, panel_ip, vpn=False):
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,38}[a-z0-9])?", client):
        raise ValueError("client must be a DNS label, at most 40 characters")
    for value in (root, home):
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) == "/":
            raise ValueError("root/home must be explicit absolute non-root paths")
    for image in (hermes_image, sandbox_image):
        if not re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", image):
            raise ValueError("images must use immutable repository@sha256 digests")
    import ipaddress
    ipaddress.IPv4Address(panel_ip)
    namespace = "agentsee-" + client
    base = "home/sandboxes/docker/default/"
    ssh = [mount("external/ssh/" + p, "/root/.ssh/" + p, True)
           for p in ("config", "known_hosts_gitlab", "slotegrator_gitlab_ed25519")]
    environment = {"HOME": "/root", "HERMES_HOME": home, "HERMES_WRITE_SAFE_ROOT": home,
                   # no-tmp: ok — runtime path INSIDE the Pod's explicit scratch volume.
                   "HERMES_ALLOW_ROOT_GATEWAY": "1", "XDG_RUNTIME_DIR": "/tmp/hermes-runtime"}
    environment.update({"XDG_" + k + "_HOME": home + "/" + v for k, v in (
        ("CACHE", "cache"), ("STATE", "state"), ("DATA", "data"), ("CONFIG", "xdg-config"))})
    hermes = workload("hermes", {
        "name": "hermes", "image": hermes_image, "workingDir": home,
        "command": ["/opt/hermes/.venv/bin/python", "-m", "hermes_runtime.standalone.cli"],
        "args": ["--home", home, "run", "--host", "0.0.0.0", "--port", "9119"],
        "env": [{"name": k, "value": v} for k, v in environment.items()],
        "securityContext": {"runAsUser": 0, "runAsGroup": 0,
                            "allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}},
        "resources": {"requests": {"cpu": "100m", "memory": "512Mi"},
                      "limits": {"cpu": "2", "memory": "2Gi"}},
        # no-tmp: ok — remote Pod scratch volume, not the controller's filesystem.
        "volumeMounts": [mount("home", home), *ssh, {"name": "tmp", "mountPath": "/tmp"},
                         {"name": "kubectl-cache", "mountPath": "/root/.kube"},
                         {"name": "opt-data", "mountPath": "/opt/data"}],
    }, namespace)
    hermes["spec"]["template"]["spec"].update({
        "serviceAccountName": "hermes", "automountServiceAccountToken": True,
        "hostAliases": [{"ip": panel_ip, "hostnames": ["agent-ready.tech"]}],
    })
    hermes["spec"]["template"]["spec"]["volumes"].append({
        "name": "kubectl-cache", "emptyDir": {"sizeLimit": "64Mi"}})
    hermes["spec"]["template"]["spec"]["volumes"].append({
        "name": "opt-data", "emptyDir": {"sizeLimit": "1Mi"}})
    mounts = [mount(base + "home", "/root"), mount(base + "desktop-home", "/home/pn"),
              mount(base + "desktop-machine-id", "/etc/machine-id", True),
              mount(base + "workspace", "/workspace"), mount("home/workspace", home + "/workspace"), *ssh]
    mounts += [mount("home/" + p, home + "/" + p, True) for p in (
        "attachments", "composer-pastes", "images", "skills", "cache/audio", "cache/delegation",
        "cache/documents", "cache/generated", "cache/images", "cache/screenshots", "cache/spillover",
        "cache/videos", "cache/web")]
    caps = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]
    command = ["sleep", "infinity"]
    limits = {"cpu": "4", "memory": "5Gi"}
    if vpn:
        caps += ["NET_ADMIN", "NET_RAW"]
        limits["devic.es/tun"] = "1"
        mounts += [mount(base + "home/.netbird/lib", "/var/lib/netbird")]
        command = ["/root/.local/bin/netbird", "service", "run", "--log-file", "/root/.netbird/log/client.log"]
    sandbox = workload("sandbox", {
        "name": "sandbox", "image": sandbox_image, "command": command, "workingDir": home + "/workspace",
        "securityContext": {"runAsUser": 0, "runAsGroup": 0, "allowPrivilegeEscalation": False, "privileged": False,
                            "capabilities": {"drop": ["ALL"], "add": caps},
                            "seccompProfile": {"type": "Localhost", "localhostProfile": "agentsee/chromium.json"},
                            "appArmorProfile": {"type": "RuntimeDefault"}},
        "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}, "limits": limits},
        # no-tmp: ok — remote Pod scratch volume, not the controller's filesystem.
        "volumeMounts": [*mounts, {"name": "tmp", "mountPath": "/tmp"}, {"name": "shm", "mountPath": "/dev/shm"}],
    }, namespace)
    sandbox["spec"]["template"]["spec"].update({"dnsPolicy": "Default"})
    sandbox["spec"]["template"]["spec"]["volumes"].append({
        "name": "shm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}})
    for item in (hermes, sandbox):
        item["spec"]["template"]["spec"]["nodeSelector"]["agentsee.io/worker"] = worker
        container = item["spec"]["template"]["spec"]["containers"][0]
        container["command"] = ["/usr/local/bin/agentsee-init", "-g", "--", *container["command"]]
    rules = [{"apiGroups": [""], "resources": ["pods"], "resourceNames": ["sandbox-0"], "verbs": ["get"]},
             {"apiGroups": [""], "resources": ["pods/exec"], "resourceNames": ["sandbox-0"], "verbs": ["get", "create"]}]
    pv_name = namespace + "-data"
    objects = [
        object_("Namespace", namespace),
        object_("PersistentVolume", pv_name, spec={
            "capacity": {"storage": "50Gi"}, "volumeMode": "Filesystem", "accessModes": ["ReadWriteOnce"],
            "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "", "local": {"path": root},
            "claimRef": {"namespace": namespace, "name": "data"},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "agentsee.io/worker", "operator": "In", "values": [worker]}]}]}}}),
        object_("PersistentVolumeClaim", "data", spec={"accessModes": ["ReadWriteOnce"], "storageClassName": "",
            "volumeName": pv_name, "resources": {"requests": {"storage": "50Gi"}}}),
        object_("ServiceAccount", "hermes"),
        object_("Role", "sandbox-exec", "rbac.authorization.k8s.io/v1", rules=rules),
        object_("RoleBinding", "sandbox-exec", "rbac.authorization.k8s.io/v1",
            subjects=[{"kind": "ServiceAccount", "name": "hermes", "namespace": namespace}],
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "sandbox-exec"}),
        hermes, sandbox,
    ]
    # Exec carries screen/CDP traffic through the API; neither Pod needs a network listener exposed to peers.
    objects.append(object_("NetworkPolicy", "deny-ingress", "networking.k8s.io/v1", spec={
        "podSelector": {}, "policyTypes": ["Ingress"], "ingress": []}))
    # Public egress is needed for provider APIs, Telegram and VPN endpoints. Deny management,
    # other tenants, service CIDRs and metadata by default. Node traffic also needs host firewall rules.
    for name in ("hermes", "sandbox"):
        destinations = [{"ipBlock": {"cidr": "0.0.0.0/0", "except": [
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]}}]
        if name == "hermes":
            destinations.append({"ipBlock": {"cidr": panel_ip + "/32"}})
        egress = [{"to": [destinations[0]]}, {"to": [{"namespaceSelector": {"matchLabels": {
            "kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")]}]
        if name == "hermes":
            egress += [{"to": destinations[1:], "ports": [{"protocol": "TCP", "port": 443}]}]
            # Explicit private API endpoint: kubectl's in-cluster endpoint is set to the private control plane.
            egress += [{"to": destinations[1:], "ports": [{"protocol": "TCP", "port": 6443}]}]
        objects.append(object_("NetworkPolicy", name + "-egress", "networking.k8s.io/v1", spec={
            "podSelector": {"matchLabels": {"app": name}}, "policyTypes": ["Egress"], "egress": egress}))
    hermes["spec"]["template"]["spec"]["containers"][0]["env"] += [
        {"name": "KUBERNETES_SERVICE_HOST", "value": panel_ip},
        {"name": "KUBERNETES_SERVICE_PORT", "value": "6443"}]
    for item in objects:
        if item["kind"] not in {"Namespace", "PersistentVolume"}:
            item["metadata"]["namespace"] = namespace
    return {"apiVersion": "v1", "kind": "List", "items": objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("client", "worker", "root", "home", "hermes-image", "sandbox-image", "panel-ip"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--vpn", action="store_true", help="NetBird + device-plugin TUN allocation (optional)")
    print(json.dumps(render(**vars(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
