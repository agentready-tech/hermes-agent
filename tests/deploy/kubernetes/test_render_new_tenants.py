"""New tenants omit the owner's migration-only SSH files without changing defaults."""

from deploy.kubernetes.render import render


def plan(**options):
    return render(client="new-tenant", worker="fixture-node", root="/srv/agentsee/agents/fixture",
                  home="/root/.hermes", hermes_image="fixture/runtime@sha256:" + "1" * 64,
                  sandbox_image="fixture/sandbox@sha256:" + "2" * 64, panel_ip="10.216.0.1", **options)


def test_new_tenant_has_no_owner_ssh_mounts_and_starts_inactive():
    workloads = [item for item in plan(external_ssh=False)["items"] if item["kind"] == "StatefulSet"]
    assert len(workloads) == 2
    for workload in workloads:
        assert workload["spec"]["replicas"] == 0
        pod = workload["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"agentsee.io/worker": "fixture-node"}
        for container in pod["containers"]:
            assert not any(mount["mountPath"].startswith("/root/.ssh/") for mount in container["volumeMounts"])


def test_migration_default_retains_readonly_owner_ssh_mounts():
    workloads = [item for item in plan()["items"] if item["kind"] == "StatefulSet"]
    for workload in workloads:
        mounts = workload["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        ssh = [mount for mount in mounts if mount["mountPath"].startswith("/root/.ssh/")]
        assert len(ssh) == 3
        assert all(mount["readOnly"] is True for mount in ssh)
