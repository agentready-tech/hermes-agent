# Native Workspace

The existing `agentsee-workspace` MCP names execute in the owning Hermes profile.
Use `python -m hermes_runtime.workspace.mcp` as its stdio command. Tool arguments
contain connection IDs and operation data, never account credentials.

Connection metadata and credentials live in the native encrypted vault slot
`<HERMES_HOME>/vault/workspace/`. The settings transport accepts one bounded JSON
request on stdin via `python -m hermes_runtime.workspace.control --home PATH`.
It exposes list/import/upsert/verify/disconnect/rules/discover/google_exchange/settings
only; it does not expose mail search, message content, sends, or event collection.
The trusted `environmentId` must match `autonomous.workspace.environment_id`.

The agent's existing terminal transport executes reviewed IMAP/SMTP, DNS/XML
discovery and Google HTTPS programs in its own sandbox. An unavailable sandbox
fails closed. MIME formatting and extraction run inside the Hermes Pod. Search
returns headers with cursor pagination over the whole mailbox. When an Exchange
server refuses UTF-8 search, results explicitly identify the header-only fallback;
the caller must continue while `hasMore`, including empty pages. Reads and proactive
collection fetch text sections and attachment metadata. Only `mail_attachment`
retrieves an attachment payload. An uncertain SMTP or Google write is never retried.

`google_schema` and `google_gws` preserve the existing JSON resource/method/params/body
interface using official Google discovery schemas. Five service origins are pinned;
shell flags, auth overrides, binary downloads and arbitrary endpoints are refused.
Authorization-code exchange and token refresh execute inside the same sandbox.

Set `autonomous.workspace.native: true` to start the existing mailbox worker.
It owns filtering, event formatting and polling, and submits events to the existing
single `agentready_home` durable queue. Cursor location and source/event identities
are unchanged; cursors advance only after durable admission. Legacy Panel URLs
never perform a request and leave collection paused until the native cutover.
