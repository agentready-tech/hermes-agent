# Agent-controlled MCP and HTTP API setup

In the managed AgentReady runtime, `manage_connections` can install a remote HTTP
MCP outside the catalog, or expose a declared JSON HTTP API through the first-party
stdio MCP adapter. The call runs in the owning Hermes process. Its terminal can
remain isolated in a separate sandbox with no access to the Hermes configuration.

Use `action: install` and exactly one `connectors` entry with `name` and `mcp: true`.
Supply either `server: {url, auth}` or `api: {base_url, auth, operations}`. Authentication
is `none`, `bearer`, or `api_key` with a header name. No key values, command, executable,
environment overrides or generated source are accepted in this input.

For an authenticated connection, Hermes requests a missing key through its native
`secret` waiter. Panel already renders this as a masked form, separate from chat
submission and history. Cancelled or unavailable input leaves the connection
uninstalled; it must never be replaced with a request for a plaintext chat message.
The owning profile's `.env` holds the credential. The configuration holds only a
generated connection-specific reference. A failed new setup can request another
protected entry with `replace_key: true`. Existing named connections are preserved.

Messaging turns reuse an adapter's existing out-of-band `request_secret` capability
where available. Telegram consumes the specifically addressed secret reply before
ordinary admission/history and applies its existing deletion/quarantine rules.
Adapters without this capability receive no plaintext fallback request.

The API adapter accepts declared GET/POST/PUT/PATCH/DELETE operations. Each operation
has a relative path and a JSON Schema object for `path`, `query`, and `body` arguments.
Its trusted runtime resolves the credential and performs the request. Redirects do
not forward credentials, external schema references are not fetched, response sizes
are bounded, and literal reflected credential values are redacted. No arbitrary
agent-generated code is executed in the Hermes container.

Registering an API adapter proves that its MCP tools are available; it does not
prove an API action or authentication succeeded. A real operation returns its HTTP
status. An uncertain write must be checked before retrying. The server's own account
permissions remain authoritative.

Definitions and secrets persist in the existing Hermes home and survive Pod
replacement. Successful results use the existing MCP target contract, so the inline
executor adds the server to the current agent's tool selection without resetting its
conversation or changing its already-sent tool schema. Native `skill_manage` creates
reusable skills in the same persistent home; skills refer to connection/tool names
and procedures, never credential values. Existing skill write-approval policy stays
in force. OAuth authorization of configured servers retains the native authorize
action; vendor client registration and interactive remote OAuth are separate from
this public/API-key setup path.
