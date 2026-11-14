"""The shared workload starts inactive and may access only enrolled sandboxes."""
import pytest

from deploy.kubernetes.render_shared import FIRECRACKER_SOCKETS, HOME, NAME, NAMESPACE, render_shared, sandbox_binding


def test_shared_plan_mounts_only_the_operator_runtime_root_and_socket_directory():
    plan = render_shared(worker='hermes-vm', root='/srv/agentsee/shared-hermes',
                         image='fixture/runtime@sha256:' + '1' * 64, panel_ip='10.216.0.1', memory_limit='4Gi')
    workload, = (resource for resource in plan['items'] if resource['kind'] == 'StatefulSet')
    assert workload['spec']['replicas'] == 0
    spec = workload['spec']['template']['spec']
    assert spec['nodeSelector'] == {'agentsee.io/worker': 'hermes-vm'}
    assert spec['serviceAccountName'] == NAME
    assert [volume['hostPath']['path'] for volume in spec['volumes'] if 'hostPath' in volume] == [
        '/srv/agentsee/shared-hermes', FIRECRACKER_SOCKETS,
    ]
    assert spec['containers'][0]['volumeMounts'][0] == {'name': 'runtime', 'mountPath': HOME, 'mountPropagation': 'HostToContainer'}
    assert spec['containers'][0]['volumeMounts'][1] == {
        'name': 'firecracker-sockets', 'mountPath': FIRECRACKER_SOCKETS, 'readOnly': True,
    }
    assert not any(resource['kind'] in ('PersistentVolume', 'PersistentVolumeClaim', 'ClusterRoleBinding') for resource in plan['items'])


def test_binding_reuses_only_a_tenants_named_sandbox_role():
    ns = 'agentsee-a-' + 'a' * 32
    binding = sandbox_binding(ns)
    assert binding['metadata']['namespace'] == ns
    assert binding['roleRef']['kind'] == 'Role' and binding['roleRef']['name'] == 'sandbox-exec'
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': NAME, 'namespace': NAMESPACE}]
    with pytest.raises(ValueError):
        sandbox_binding('kube-system')
