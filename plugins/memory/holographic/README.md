# Holographic Memory Provider

Local SQLite fact store with source evidence, correction history, automatic recall,
background consolidation, FTS5 search, trust scoring, entity resolution and HRR retrieval.

## Requirements

None — uses SQLite (always available). NumPy optional for HRR algebra.
Automatic consolidation additionally uses the existing auxiliary model route. It does
not require a separate server, embedding service or database.

## Setup

```bash
hermes memory setup    # select "holographic"
```

Or manually:
```bash
hermes config set memory.provider holographic
hermes config set plugins.hermes-memory-store.consolidation_enabled true
```

## Config

Config in `config.yaml` under `plugins.hermes-memory-store`:

| Key | Default | Description |
|-----|---------|-------------|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite database path |
| `auto_extract` | `false` | Auto-extract facts at session end |
| `consolidation_enabled` | `true` in new setup | One bounded background consolidation pass after each completed primary-agent turn; absent in an older config means off |
| `default_trust` | `0.5` | Default trust score for new facts |
| `hrr_dim` | `1024` | HRR vector dimensions |

## Tools

| Tool | Description |
|------|-------------|
| `fact_store` | add, search, probe, related, reason, contradict, update, remove, list, history, explain |
| `fact_feedback` | Rate facts as helpful/unhelpful (trains trust scores) |

## Evidence and corrections

Facts keep a stable ID. Updating their content creates a revision linked to its
predecessor, including the supporting quote, session, message locator and timestamp.
Normal recall returns the current revision. `fact_store(action="history", fact_id=...)`
returns the correction trail; `explain` returns the current fact and its evidence.
An optional stable `key` (for example `project.database`) identifies one subject and
attribute across corrections.

For explicit writes, the runtime supplies actual conversation messages. An optional
`source_quote` must match user text or a tool result; fabricated quotes are rejected.
Without a quote the latest genuine user message is recorded as the originating request,
which is provenance, not proof that every inference is true. Calls without runtime
evidence are labeled `agent_record`, never given an invented message citation.
Original messages can be recovered with `session_search`; evidence URIs are locators,
not filesystem paths.

Existing databases migrate additively. Existing facts remain available and receive
an initial revision marked `legacy`, since their original sources are unknown.
`remove` deletes the fact and its revisions, and suppresses replay of its known source
events from the ingestion queue.

## Automatic recall and consolidation

When this provider is selected, native `MemoryManager.prefetch_all` retrieves current
facts before the model turn, with revision IDs and evidence locators. This adds bounded
turn context (at most 6,000 characters), without rewriting the cached system prompt.

The existing profile-bound background worker persists new user evidence into an
idempotent SQLite queue, then makes one auxiliary call using
`auxiliary.memory_consolidation` (or the native default route). Each pass processes at
most four source chunks of 4,000 characters plus at most 8,000 characters of candidate
facts. Steering messages are included; old conversations are not silently bulk-imported.
Assistant replies, compaction summaries, scheduled wakes and delegated context are not
promoted into new user facts.

The model proposes facts with exact supporting quotes. Code validates the proposals
and atomically commits revisions with the queue acknowledgement. A cross-process lease
serializes consolidation; revision checks prevent an old model result from overwriting
a newer correction. Failed or malformed model output leaves the facts unchanged and
keeps the job pending for the next completed turn, including after restart. Completed
queue entries retain event hashes, not copies of unrelated user text.

This is automatic consolidation on new activity, not a separate hourly daemon.
New setup offers it enabled; existing configurations without the setting keep their
previous behavior until explicitly enabled with the command above.
It may make an additional model request for a completed turn. Disable it with
`plugins.hermes-memory-store.consolidation_enabled: false`; automatic recall and
explicit fact tools still work. The legacy regex extractor remains independently
controlled by `auto_extract`.

The compactor, checkpoint archives, same-session emergency recovery and Gateway reset
policy are unchanged. Memory extraction does not participate in their commit transaction
and cannot make a failed model extraction cancel context recovery.
