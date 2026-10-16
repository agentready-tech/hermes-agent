"""hermes-memory-store — holographic memory plugin (MemoryProvider): structured fact storage with entity
resolution, trust scoring, and HRR-based compositional retrieval. Original plugin by dusterbloom (PR #2351).
Config in $HERMES_HOME/config.yaml under plugins.hermes-memory-store: db_path ($HERMES_HOME/memory_store.db),
auto_extract (false), default_trust (0.5), min_trust_threshold (0.3), temporal_decay_half_life (0),
hrr_dim (1024), hrr_weight (0.3)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import is_truthy_value
from .store import MemoryStore
from .retrieval import FactRetriever
from .consolidation import Consolidator
from .evidence import tool_sources
from hermes_cli.config import cfg_get

logger = logging.getLogger(__name__)

# Expanded by initialize() against the profile that opens it. A concrete path is copied by a profile clone
# and outlives a rename, so it keeps naming the old profile's DB.
_DEFAULT_DB_PATH = "$HERMES_HOME/memory_store.db"


FACT_STORE_SCHEMA = {
    "name": "fact_store",
    "description": (
        "Deep structured memory with algebraic reasoning. Use alongside the memory tool — memory for always-on "
        "context, fact_store for deep recall and compositional queries.\n\nACTIONS (simple → powerful):\n"
        "• add — Store a fact the user would expect you to remember.\n• search — Keyword lookup ('editor config', 'deploy process').\n"
        "• probe — Entity recall: ALL facts about a person/thing.\n• related — What connects to an entity? Structural adjacency.\n"
        "• reason — Compositional: facts connected to MULTIPLE entities simultaneously.\n"
        "• contradict — Memory hygiene: find facts making conflicting claims.\n• update/remove/list — CRUD operations.\n"
        "• history — Previous revisions and evidence.\n• explain — Current fact and its sources.\n\n"
        "IMPORTANT: Before answering questions about the user, ALWAYS probe or reason first."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "search", "probe", "related", "reason", "contradict", "update", "remove", "list", "history", "explain"]},
            "content": {"type": "string", "description": "Fact content (required for 'add')."},
            "key": {"type": "string", "description": "Optional stable subject.attribute identity. Corrections preserve revisions."},
            "source_quote": {"type": "string", "description": "Optional exact quote from actual user text or tool results supporting the fact. Runtime verifies it."},
            "query": {"type": "string", "description": "Search query (required for 'search')."},
            "entity": {"type": "string", "description": "Entity name for 'probe'/'related'."},
            "entities": {"type": "array", "items": {"type": "string"}, "description": "Entity names for 'reason'."},
            "fact_id": {"type": "integer", "description": "Fact ID for 'update'/'remove'."},
            "category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
            "tags": {"type": "string", "description": "Comma-separated tags."},
            "trust_delta": {"type": "number", "description": "Trust adjustment for 'update'."},
            "min_trust": {"type": "number", "description": "Minimum trust filter (default: 0.3)."},
            "limit": {"type": "integer", "description": "Max results (default: 10)."},
        },
        "required": ["action"],
    },
}

FACT_FEEDBACK_SCHEMA = {
    "name": "fact_feedback",
    "description": ("Rate a fact after using it. Mark 'helpful' if accurate, 'unhelpful' if outdated. "
                    "This trains the memory — good facts rise, bad facts sink."),
    "parameters": {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["helpful", "unhelpful"]},
                       "fact_id": {"type": "integer", "description": "The fact ID to rate."}},
        "required": ["action", "fact_id"],
    },
}

# Auto-extraction (on_session_end): (patterns, category) — user preferences -> user_pref, decisions -> project.
_EXTRACT_CATEGORIES = (
    ([re.compile(r'\bI\s+(?:prefer|like|love|use|want|need)\s+(.+)', re.IGNORECASE),
      re.compile(r'\bmy\s+(?:favorite|preferred|default)\s+\w+\s+is\s+(.+)', re.IGNORECASE),
      re.compile(r'\bI\s+(?:always|never|usually)\s+(.+)', re.IGNORECASE)], "user_pref"),
    ([re.compile(r'\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)', re.IGNORECASE),
      re.compile(r'\bthe\s+project\s+(?:uses|needs|requires)\s+(.+)', re.IGNORECASE)], "project"),
)


def _load_plugin_config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly  # canonical: managed-scope overlay + ${VAR} expansion
        return cfg_get(load_config_readonly(), "plugins", "hermes-memory-store", default={}) or {}
    except Exception:
        logger.debug("Holographic configuration unavailable", exc_info=True)
        return {}


def _results(items: list, key: str = "results") -> str:
    return json.dumps({key: items, "count": len(items)})


def _limit(args: dict) -> int:
    return int(args.get("limit", 10))


def _tool_handler(actions: dict):
    """(self, args) handler dispatching on args["action"] over ``actions``; unknown action -> tool_error."""
    return lambda self, args: (actions[args["action"]](self, args) if args["action"] in actions
                               else tool_error(f"Unknown action: {args['action']}"))


class HolographicMemoryProvider(MemoryProvider):
    """Holographic memory with structured facts, entity resolution, and HRR retrieval."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._store = self._retriever = self._consolidator = None
        self._session_id = ""
        # Existing profiles did not opt into auxiliary model calls. New setup offers true;
        # absence in an older config preserves its previous behavior.
        self._consolidation_enabled = is_truthy_value(self._config.get("consolidation_enabled", False))
        self._min_trust = float(self._config.get("min_trust_threshold", 0.3))

    @property
    def name(self) -> str:
        return "holographic"

    def is_available(self) -> bool:
        return True  # SQLite is always available, numpy is optional

    def save_config(self, values, hermes_home):
        """Write config to config.yaml under plugins.hermes-memory-store."""
        # The canonical writer: config lock, managed-mode refusal, default stripping, atomic replace.
        # ``merge_existing`` keeps every other section; *hermes_home* is the active profile already.
        from hermes_cli.config import save_config
        values = dict(values)
        # This profile's own DB spelled out (older setups wrote it; the dashboard form re-submits what it
        # read) pins every clone and rename of the profile to this file, so it is stored as the placeholder.
        db_path = values.get("db_path")
        if isinstance(db_path, str) and Path(db_path).expanduser() == Path(hermes_home) / "memory_store.db":
            values["db_path"] = _DEFAULT_DB_PATH
        save_config({"plugins": {"hermes-memory-store": values}}, merge_existing=True)

    def get_config_schema(self):
        return [
            {"key": "db_path", "description": "SQLite database path", "default": _DEFAULT_DB_PATH},
            {"key": "auto_extract", "description": "Auto-extract facts at session end", "default": "false", "choices": ["true", "false"]},
            {"key": "consolidation_enabled", "description": "Consolidate sourced facts after turns in the background", "default": "true", "choices": ["true", "false"]},
            {"key": "default_trust", "description": "Default trust score for new facts", "default": "0.5"},
            {"key": "hrr_dim", "description": "HRR vector dimensions", "default": "1024"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        from hermes_constants import get_hermes_home
        _hermes_home = str(kwargs.get("hermes_home") or get_hermes_home())
        db_path = self._config.get("db_path", _hermes_home + "/memory_store.db")
        if isinstance(db_path, str):  # expand $HERMES_HOME so paths resolve to the active profile
            db_path = db_path.replace("$HERMES_HOME", _hermes_home).replace("${HERMES_HOME}", _hermes_home)
        hrr_dim = int(self._config.get("hrr_dim", 1024))
        self._store = MemoryStore(db_path=db_path, default_trust=float(self._config.get("default_trust", 0.5)), hrr_dim=hrr_dim)
        self._retriever = FactRetriever(store=self._store, hrr_dim=hrr_dim, hrr_weight=float(self._config.get("hrr_weight", 0.3)),
                                        temporal_decay_half_life=int(self._config.get("temporal_decay_half_life", 0)))
        self._session_id = session_id
        self._consolidator = Consolidator(self._store, self._retriever)
        self._primary = kwargs.get("agent_context", "primary") == "primary"

    def system_prompt_block(self) -> str:
        if not self._store:
            return ""
        try:
            total = self._store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            logger.debug("Holographic fact count unavailable", exc_info=True)
            total = 0
        body = ("Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                if total == 0 else
                f"Active. {total} facts stored with entity resolution and trust scoring.\n"
                "Use fact_store to search, probe entities, reason across entities, or add facts.\n")
        return ("# Holographic Memory\n" + body
                + "Use fact_feedback to rate facts after using them (trains trust scores). "
                "For corrections, update the existing fact_id or reuse its stable key; do not add a second conflicting fact. "
                "Use history/explain and session_search to verify source evidence.")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._retriever or not query:
            return ""
        try:
            with self._store._lock:
                results = self._store.describe_facts(self._retriever.search(query, min_trust=self._min_trust, limit=5))
            lines, size = [], 0
            for result in results:
                item = {key: result[key] for key in ("fact_id", "version_id", "content", "key")}
                item["sources"] = [{key: source[key] for key in ("kind", "uri", "message_id") if key in source}
                                   for source in result["sources"]]
                line = json.dumps(item, ensure_ascii=False)
                if size + len(line) > 6000:
                    break
                lines.append(line)
                size += len(line)
            return "## Holographic Memory\n" + "\n".join(lines) if lines else ""
        except Exception as e:
            logger.debug("Holographic prefetch failed: %s", e, exc_info=True)
            return ""

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [FACT_STORE_SCHEMA, FACT_FEEDBACK_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], *, messages=None, session_id="", **kwargs) -> str:
        if tool_name not in self._TOOL_HANDLERS:
            return tool_error(f"Unknown tool: {tool_name}")
        try:
            if tool_name == "fact_store" and args.get("action") in ("add", "update"):
                args = {**args, "_evidence": tool_sources(args, messages, session_id or self._session_id)}
            with self._store._lock:
                return self._TOOL_HANDLERS[tool_name](self, args)
        except KeyError as exc:
            return tool_error(f"Missing required argument: {exc}")
        except Exception as exc:
            logger.debug("Holographic tool failed", exc_info=True)
            return tool_error(str(exc))

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        # is_truthy_value: auto_extract is a string enum ("false"/"true"); plain truthiness would treat "false" as on.
        if is_truthy_value(self._config.get("auto_extract", False)) and self._store and messages:
            self._auto_extract_facts(messages)

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        # MemoryManager already invokes this on its serialized, profile-bound background worker.
        if not self._consolidation_enabled or not self._primary or not self._consolidator:
            return
        if turn_author and turn_author.get("is_bot"):
            return
        evidence = messages if messages is not None else [{"role": "user", "content": user_content}]
        self._consolidator.enqueue(evidence, session_id or self._session_id, current_user_content=user_content)
        self._consolidator.run_once()

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        """Mirror corrections/removals as well as additions, retaining write provenance."""
        if not self._store:
            return
        metadata = metadata or {}
        sources = [{"kind": "memory_write", "session_id": metadata.get("session_id", self._session_id),
                    "uri": f"memory://{target}", "quote": content, "write_origin": metadata.get("write_origin")}]
        with self._store._mutation():
            old = metadata.get("previous_content") or metadata.get("old_text")
            row = self._store._one("SELECT fact_id FROM facts WHERE content=?", (old,)) if old else None
            if action == "remove" and row:
                self._store.remove_fact(row[0])
            elif action == "replace" and row:
                self._store.update_fact(row[0], content=content, sources=sources)
            elif action in ("add", "replace") and content:
                self._store.add_fact(content, category="user_pref" if target == "user" else "general", sources=sources)

    def shutdown(self) -> None:
        # Close on the caller's thread: leaving the shared connection (+ write lock) to GC keeps it alive on a gateway.
        if self._store is not None:
            try:
                self._store.close()
            except Exception as e:
                logger.debug("Holographic shutdown close() failed: %s", e, exc_info=True)
        self._store = self._retriever = self._consolidator = None

    # Tool handlers (self, args) -> str. KeyError from args[...] / Exception -> tool_error in handle_tool_call;
    # argument coercion order (and therefore which error surfaces first) mirrors the underlying call order.

    def _entity_query(self, method: str, a: dict) -> str:
        """'probe' / 'related': single-entity retriever queries."""
        return _results(self._store.describe_facts(getattr(self._retriever, method)(
            a["entity"], category=a.get("category"), limit=_limit(a))))

    _TOOL_HANDLERS = {
        "fact_store": _tool_handler({
            "add": lambda self, a: json.dumps({"fact_id": self._store.add_fact(
                a["content"], category=a.get("category", "general"), tags=a.get("tags", ""),
                sources=a.get("_evidence"), key=a.get("key")), "status": "added"}),
            "search": lambda self, a: _results(self._store.describe_facts(self._retriever.search(
                a["query"], category=a.get("category"), min_trust=float(a.get("min_trust", self._min_trust)), limit=_limit(a)))),
            "probe": lambda self, a: self._entity_query("probe", a),
            "related": lambda self, a: self._entity_query("related", a),
            "reason": lambda self, a: _results(self._store.describe_facts(self._retriever.reason(a["entities"], category=a.get("category"), limit=_limit(a))))
            if a.get("entities") else tool_error("reason requires 'entities' list"),
            "contradict": lambda self, a: _results(self._retriever.contradict(category=a.get("category"), limit=_limit(a))),
            "update": lambda self, a: json.dumps({"updated": self._store.update_fact(
                int(a["fact_id"]), content=a.get("content"), trust_delta=float(a["trust_delta"]) if "trust_delta" in a else None,
                tags=a.get("tags"), category=a.get("category"), sources=a.get("_evidence"))}),
            "remove": lambda self, a: json.dumps({"removed": self._store.remove_fact(int(a["fact_id"]))}),
            "list": lambda self, a: _results(self._store.describe_facts(self._store.list_facts(
                category=a.get("category"), min_trust=float(a.get("min_trust", 0.0)), limit=_limit(a))), key="facts"),
            "history": lambda self, a: _results(self._store.fact_history(int(a["fact_id"]), limit=_limit(a)), key="versions"),
            "explain": lambda self, a: _results(self._store.describe_facts(
                [dict(row)] if (row := self._store._one("SELECT * FROM facts WHERE fact_id=?", (int(a["fact_id"]),))) else [])),
        }),
        "fact_feedback": lambda self, a: json.dumps(self._store.record_feedback(int(a["fact_id"]), helpful=a["action"] == "helpful")),
    }

    def _auto_extract_facts(self, messages: list) -> None:
        # Compaction handoff summaries arrive as role="user" and match the decision patterns; never store the
        # compactor's own output as a fact. A merge-into-tail row holds genuine prior user text BEFORE
        # _MERGED_SUMMARY_DELIMITER (after the header) and the summary AFTER it — harvest only that segment.
        from agent.context_compressor import _MERGED_PRIOR_CONTEXT_HEADER, _MERGED_SUMMARY_DELIMITER, is_compaction_summary_message  # heavy; lazy
        extracted = 0
        for msg in messages:
            content = msg.get("content", "") if msg.get("role") == "user" else None
            pre = content.split(_MERGED_SUMMARY_DELIMITER, 1)[0].removeprefix(_MERGED_PRIOR_CONTEXT_HEADER).strip() \
                if isinstance(content, str) and _MERGED_SUMMARY_DELIMITER in content else ""
            if pre:
                content = pre
            elif content is None or is_compaction_summary_message(msg):
                continue
            if not isinstance(content, str) or len(content) < 10:
                continue
            for patterns, category in _EXTRACT_CATEGORIES:
                if any(p.search(content) for p in patterns):
                    try:
                        from .evidence import message_sources
                        sources = list(message_sources([msg], self._session_id))
                        self._store.add_fact(content[:400], category=category, sources=sources[-1:] or None)
                        extracted += 1
                    except Exception:
                        logger.debug("Holographic extraction failed", exc_info=True)
        if extracted:
            logger.info("Auto-extracted %d facts from conversation", extracted)

def register(ctx) -> None:
    """Register the holographic memory provider with the plugin system."""
    ctx.register_memory_provider(HolographicMemoryProvider(config=_load_plugin_config()))
