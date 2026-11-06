"""HTTP socket connections use the owning profile's existing terminal sandbox.

The SDK keeps TLS, SNI, authentication and streaming. Only TCP connection establishment
changes: the existing exec-stream relay resolves and connects from inside the sandbox.
"""

from tools.environments import streams


class SandboxTransportUnavailable(RuntimeError):
    """A non-local terminal backend cannot provide the requested sandbox transport."""


def sandbox_environment():
    """Return the current profile's sandbox; None means an explicitly local backend."""
    from hermes_constants import get_hermes_home
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tools.terminal_tool import _get_env_config, ensure_task_env
    token = install_profile_terminal_scope(get_hermes_home())
    try:
        if _get_env_config()["env_type"] == "local":
            return None
        environment = ensure_task_env("agentready_home")
        if environment is None or not streams.supports_streams(environment):
            raise SandboxTransportUnavailable("The configured sandbox cannot host this connection; host execution is disabled")
        return environment
    finally:
        reset_terminal_scope(token)


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
