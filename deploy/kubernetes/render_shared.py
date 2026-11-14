"""Render the worker's shared Hermes workload and only its sandbox permissions."""
import ipaddress
from pathlib import PurePosixPath
import re

from deploy.kubernetes.render import object_

NAMESPACE = 'agentsee-runtime'
NAME = 'hermes-shared'
HOME = '/root/.hermes'


def sandbox_binding(namespace):
    if not re.fullmatch(r'agentsee-a-[a-f0-9]{32}|agentsee-mike', namespace):
        raise ValueError('Invalid sandbox namespace')
    binding = object_('RoleBinding', 'shared-hermes-sandbox', 'rbac.authorization.k8s.io/v1',
        subjects=[{'kind': 'ServiceAccount', 'name': NAME, 'namespace': NAMESPACE}],
        roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'sandbox-exec'})
    binding['metadata']['namespace'] = namespace
    return binding


def render_shared(*, worker, root, image, panel_ip, memory_limit):
    """Start inactive so the operator can stage resources before moving any ownership."""
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', worker):
        raise ValueError('Invalid worker label')
    path = PurePosixPath(root)
    if not path.is_absolute() or '..' in path.parts or not str(path).startswith('/srv/agentsee/'):
        raise ValueError('Invalid shared Hermes root')
    if not re.fullmatch(r'\S+@sha256:[a-f0-9]{64}', image):
        raise ValueError('Shared Hermes image must be an immutable digest')
    address = str(ipaddress.IPv4Address(panel_ip))
    if not re.fullmatch(r'[1-9][0-9]*Gi', memory_limit):
        raise ValueError('Set an explicit worker memory limit')
    labels = {'app': NAME}
    env = {'HOME': '/root', 'HERMES_HOME': HOME, 'HERMES_WRITE_SAFE_ROOT': HOME,
           'HERMES_ALLOW_ROOT_GATEWAY': '1', 'XDG_RUNTIME_DIR': '/tmp/hermes-runtime',
           'KUBERNETES_SERVICE_HOST': address, 'KUBERNETES_SERVICE_PORT': '6443'}
    mounts = [{'name': 'runtime', 'mountPath': HOME, 'mountPropagation': 'HostToContainer'},
              {'name': 'tmp', 'mountPath': '/tmp'}, {'name': 'kubectl-cache', 'mountPath': '/root/.kube'},
              {'name': 'opt-data', 'mountPath': '/opt/data'}]
    pod = {'nodeSelector': {'agentsee.io/worker': worker}, 'serviceAccountName': NAME,
           'imagePullSecrets': [{'name': 'agentsee-ghcr-pull'}], 'terminationGracePeriodSeconds': 60,
           'volumes': [{'name': 'runtime', 'hostPath': {'path': str(path), 'type': 'Directory'}},
                       {'name': 'tmp', 'emptyDir': {'sizeLimit': '512Mi'}},
                       {'name': 'kubectl-cache', 'emptyDir': {'sizeLimit': '64Mi'}},
                       {'name': 'opt-data', 'emptyDir': {'sizeLimit': '1Mi'}}],
           'containers': [{'name': 'hermes', 'image': image, 'imagePullPolicy': 'IfNotPresent',
               'command': ['/usr/local/bin/agentsee-init', '-g', '--', '/opt/hermes/.venv/bin/python', '-m', 'hermes_runtime'],
               'args': ['--shared-profile-host', 'gateway', 'run', '--no-supervise', '--external-supervisor'],
               'workingDir': HOME, 'env': [{'name': key, 'value': val} for key, val in env.items()],
               'volumeMounts': mounts,
               'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'allowPrivilegeEscalation': False,
                   'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']},
                   'seccompProfile': {'type': 'RuntimeDefault'}},
               'resources': {'requests': {'cpu': '100m', 'memory': '512Mi'},
                             'limits': {'memory': memory_limit}}}]}
    namespace_item = object_('Namespace', NAMESPACE)
    account = object_('ServiceAccount', NAME)
    workload = object_('StatefulSet', NAME, 'apps/v1', spec={'replicas': 0, 'serviceName': NAME,
        'selector': {'matchLabels': labels}, 'template': {'metadata': {'labels': labels}, 'spec': pod}})
    policy = object_('NetworkPolicy', NAME, 'networking.k8s.io/v1', spec={
        'podSelector': {'matchLabels': labels}, 'policyTypes': ['Ingress', 'Egress'], 'ingress': [],
        'egress': [{'to': [{'ipBlock': {'cidr': '0.0.0.0/0', 'except': [
            '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16']}}]},
            {'to': [{'ipBlock': {'cidr': address + '/32'}}], 'ports': [
                {'protocol': 'TCP', 'port': 443}, {'protocol': 'TCP', 'port': 6443}]},
            {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                     'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
             'ports': [{'protocol': protocol, 'port': 53} for protocol in ('TCP', 'UDP')]}]})
    for item in (account, workload, policy):
        item['metadata']['namespace'] = NAMESPACE
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [namespace_item, account, workload, policy]}
