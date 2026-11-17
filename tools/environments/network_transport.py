"""HTTP socket connections use the owning profile's existing terminal sandbox.

The SDK keeps TLS, SNI, authentication and streaming. Only TCP connection establishment
changes: the existing exec-stream relay resolves and connects from inside the sandbox.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from tools.environments import streams


class SandboxTransportUnavailable(RuntimeError):
    """A non-local terminal backend cannot provide the requested sandbox transport."""


class SandboxNetworkDormant(SandboxTransportUnavailable):
    """A background connection has no Ready on-demand sandbox to connect through."""


_passive_environment = ContextVar('passive_sandbox_environment', default=None)


def _probe_on_demand_environment():
    from hermes_constants import get_hermes_home
    from tools.environments.base import EnvironmentConnectionError
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import _get_env_config
    from tools.terminal_tool_backends import _build_kubernetes_env

    with install_and_reset_profile_terminal_scope(get_hermes_home()):
        config = _get_env_config()
        if not _on_demand(config):
            return None
        try:
            return _build_kubernetes_env(cwd='/workspace', timeout=15, cc=config, probe_only=True)
        except EnvironmentConnectionError as exc:
            raise SandboxNetworkDormant('The on-demand sandbox is not ready') from exc


@asynccontextmanager
async def passive_sandbox_connections():
    """Bind one existing Pod to a client's connection attempt and own its forwarding listeners."""
    env = await asyncio.to_thread(_probe_on_demand_environment)
    token = _passive_environment.set(env)
    try:
        yield
    finally:
        _passive_environment.reset(token)
        if env is not None:
            streams.close_port_forwards(env)


def _on_demand(config):
    return config['env_type'] == 'kubernetes' and bool(config['kubernetes_idle_seconds'])


def on_demand_sandbox_lifecycle():
    """Return the current profile's managed controller without accessing the cluster."""
    from hermes_constants import get_hermes_home
    from hermes_platform.resolver import locate_command
    from tools.environments.kubernetes_lifecycle import SandboxLifecycle
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import _get_env_config

    home = get_hermes_home()
    with install_and_reset_profile_terminal_scope(home):
        config = _get_env_config()
    if not _on_demand(config):
        return None
    if (config['kubernetes_pod'], config['kubernetes_container']) != ('sandbox-0', 'sandbox'):
        raise SandboxTransportUnavailable('The on-demand Kubernetes sandbox target is invalid')
    executable = locate_command('kubectl').command
    if not executable:
        raise SandboxTransportUnavailable('kubectl is required for the on-demand sandbox')
    return SandboxLifecycle(home, executable[0], config['kubernetes_namespace'], config['kubernetes_idle_seconds'])


def sandbox_environment():
    """Return the current profile's sandbox; None means an explicitly local backend."""
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import install_and_reset_profile_terminal_scope
    from tools.terminal_tool import _get_env_config, ensure_task_env
    probe = _passive_environment.get()
    if probe is not None:
        if probe.lifecycle.home != get_hermes_home():
            raise SandboxTransportUnavailable('The sandbox network probe belongs to another profile')
        return probe
    with install_and_reset_profile_terminal_scope(get_hermes_home()):
        if _get_env_config()['env_type'] == 'local':
            return None
        environment = ensure_task_env('agentready_home')
        if environment is None or not streams.supports_streams(environment):
            raise SandboxTransportUnavailable('The configured sandbox cannot host this connection; host execution is disabled')
        return environment


class _SyncSandboxBackend:
    def __init__(self, backend, environment):
        self.backend, self.environment = backend, environment

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        local_port = streams.forward_port(self.environment, port, remote_host=host, close_grace=1.0)
        return self.backend.connect_tcp("127.0.0.1", local_port, timeout=timeout, socket_options=socket_options)

    def connect_unix_socket(self, *args, **kwargs):
        raise SandboxTransportUnavailable("Host Unix sockets are unavailable to a sandbox connection")

    def sleep(self, seconds):
        return self.backend.sleep(seconds)


class _AsyncSandboxBackend:
    def __init__(self, backend, environment):
        self.backend, self.environment = backend, environment

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        local_port = streams.forward_port(self.environment, port, remote_host=host, close_grace=1.0)
        return await self.backend.connect_tcp("127.0.0.1", local_port, timeout=timeout, socket_options=socket_options)

    async def connect_unix_socket(self, *args, **kwargs):
        raise SandboxTransportUnavailable("Host Unix sockets are unavailable to a sandbox connection")

    async def sleep(self, seconds):
        return await self.backend.sleep(seconds)


def http_transport(httpx_module, *, async_mode=True, **kwargs):
    """SDK-compatible HTTP transport; all remote TCP/DNS uses the native sandbox.

    httpx and the MCP SDK's httpx2 expose no public network-backend argument. Their
    pinned httpcore pools share this single backend seam; keeping the original pool
    preserves its TLS, proxy, HTTP/2, timeout and connection-limit behavior.
    """
    environment = sandbox_environment()
    transport_class = httpx_module.AsyncHTTPTransport if async_mode else httpx_module.HTTPTransport
    transport = transport_class(**kwargs)
    if environment is not None:
        backend_class = _AsyncSandboxBackend if async_mode else _SyncSandboxBackend
        transport._pool._network_backend = backend_class(transport._pool._network_backend, environment)
    return transport


def sandbox_identity(environment):
    """Connection sharing must keep the owning profile and native target together."""
    from hermes_constants import get_hermes_home
    if environment is None:
        return None
    return {"profile": str(get_hermes_home()), "backend": type(environment).__name__,
            "target": {key: getattr(environment, key, None) for key in
                       ("namespace", "pod", "container", "kubernetes_identity", "_container_id", "instance_id")}}
