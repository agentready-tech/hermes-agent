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
returns headers with cursor pagination in `folder` (default `INBOX`). `Sent`,
`Drafts` and `Archive` resolve IMAP special-use folders; arbitrary IMAP names
are exact, including Unicode and hierarchy delimiters. A missing or ambiguous
alias fails explicitly. Gmail uses its system labels or an exact custom label;
its `Archive` view means non-INBOX mail outside Spam/Trash, including sent mail.
Use `mail_folders(connectionId)` before searching an unfamiliar folder. Copy a
returned selectable `name` to `mail_search.folder` with `folderExact=true`. It returns
the server's exact Unicode `name`, server-derived `role` (or null), `selectable`
and hierarchy `delimiter` (or null). Nonselectable IMAP rows are hierarchy
placeholders; Gmail rows are existing labels, without an invented Archive label.
IMAP discovery performs one authenticated LIST, without selecting a mailbox,
reading UIDVALIDITY, searching or fetching messages. Empty lists stay empty;
failures return safe codes and are not retried. The existing `mailRead` grant,
own-profile vault and bounded sandbox transport apply to this tool too.
`folderExact` defaults to false, retaining `Sent`, `Drafts` and `Archive` alias
behavior. True selects a literal mailbox/label even when its name collides with
an alias. IMAP cursors and opaque message IDs retain the resolved mailbox, so
subsequent reads and attachments select it directly, without reinterpreting aliases.
Optional boolean `hasAttachment` filters attachment metadata without downloading
payloads. IMAP filtering can produce an empty page with `hasMore=true`;
`totalMatches` is unknown for that filtered view. Keep folder/query/filters fixed
while following a cursor. When an Exchange
server refuses UTF-8 search, results explicitly identify the header-only fallback;
the caller must continue while `hasMore`, including empty pages. Reads and proactive
collection fetch text sections and attachment metadata. Only `mail_attachment`
retrieves an attachment payload. An uncertain SMTP or Google write is never retried.

Pass the opaque ID returned by search unchanged to `mail_read` and
`mail_attachment`: non-INBOX IMAP IDs retain the exact resolved mailbox, UIDVALIDITY
and UID. They read the original Sent message, without needing a quoted reply.
Legacy `UIDVALIDITY:UID` IDs and version-1 search cursors remain INBOX-only.
Proactive delivery continues to collect INBOX only; folder search does not change it.

`google_schema` and `google_gws` preserve the existing JSON resource/method/params/body
interface using official Google discovery schemas. Five service origins are pinned;
shell flags, auth overrides, binary downloads and arbitrary endpoints are refused.
Authorization-code exchange and token refresh execute inside the same sandbox.

Set `autonomous.workspace.native: true` to start the existing mailbox worker.
Its IMAP/Gmail polling runs in a bounded subprocess inside Hermes even when
the sandbox is stopped. The shared gateway starts a collector for each served
managed profile. Each poll uses the current per-mailbox `proactive` setting,
sender rules and saved cursor. Mail tools continue to use the sandbox transport.
The worker owns filtering and event formatting and submits events to the existing
single `agentready_home` durable queue. Cursor location and source/event identities
are unchanged; cursors advance only after durable admission. Legacy Panel URLs
never perform a request and leave collection paused until the native cutover.
