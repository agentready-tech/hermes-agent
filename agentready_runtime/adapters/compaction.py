"""Hermes compression state, cancellation and auxiliary-model binding."""

import threading
import time
from agentready_runtime.checkpoints import persist_bounded_checkpoint
from agentready_runtime.adapters.checkpoint_files import FileCheckpointArchive
from agentready_runtime.adapters.hermes import managed_home
from agentready_runtime.adapters.state import current_home_session_id

_CHECKPOINT_DEPTH = threading.local()


def estimate_checkpoint_tokens(text):
    try:
        from agent.model_metadata import estimate_messages_tokens_rough

        return int(estimate_messages_tokens_rough([{"role": "user", "content": text}]))
    except Exception:
        return max(1, (len(text) + 2) // 3)


def generate_summary(self, proceed, *args, **kwargs):
    from agent.conversation_compression import (
        _raise_if_stale_attempt,
        _caller_attempt_is_current,
    )
    from agent.auxiliary_client import AuxiliaryExplicitCancellation

    active = getattr(_CHECKPOINT_DEPTH, "active", None)
    if active is None:
        active = _CHECKPOINT_DEPTH.active = set()
    if id(self) in active:
        # Hermes retries a failed auxiliary route via self._generate_summary.
        # Archive and bound only once, after that retry returns to its owner.
        return proceed(*args, **kwargs)
    active.add(id(self))
    previous = getattr(self, "_previous_summary", None)
    previous_provenance = getattr(self, "_summary_has_user_turn", None)
    try:
        result = proceed(*args, **kwargs)
        body = getattr(self, "_previous_summary", None)
        if not result or not body:
            return result
        try:
            _raise_if_stale_attempt(self)

            def summarize(prompt):
                from agent.agent_runtime_helpers import strip_think_blocks
                from agent.context_compressor import (
                    _redact_compaction_text,
                    _extract_pruned_skill_names,
                    _reinject_pruned_skill_markers,
                )

                content = self._call_summary_llm(prompt, time.monotonic())
                _raise_if_stale_attempt(self)
                content = _redact_compaction_text(
                    strip_think_blocks(None, content).strip()
                )
                if not content:
                    raise RuntimeError("Checkpoint recompression returned no summary")
                content = _reinject_pruned_skill_markers(
                    content, _extract_pruned_skill_names(body)
                )
                has_user_turn = getattr(self, "_summary_has_user_turn", None)
                if has_user_turn is None:
                    turns = args[0] if args else kwargs.get("turns_to_summarize", [])
                    has_user_turn = self._transcript_has_real_user_turn(turns)
                self._validate_summary_user_provenance(content, has_user_turn)
                return content

            bounded = persist_bounded_checkpoint(
                getattr(self, "_session_id", "") or current_home_session_id(),
                body,
                archive=FileCheckpointArchive(managed_home() / "workspace"),
                estimate=estimate_checkpoint_tokens,
                summarize=summarize,
                previous_summary=previous,
            )
            _raise_if_stale_attempt(self)
        except AuxiliaryExplicitCancellation:
            if _caller_attempt_is_current(self):
                self._previous_summary = previous
                self._summary_has_user_turn = previous_provenance
            raise
        except Exception as error:
            _raise_if_stale_attempt(self)
            self._previous_summary = previous
            self._summary_has_user_turn = previous_provenance
            # Return failure to Hermes' abort_on_summary_failure path. Never
            # publish oversized or mechanically truncated summaries, nor retry
            # the entire transcript just to shrink this one checkpoint.
            self._last_summary_error = (
                "AgentReady checkpoint recompression failed ("
                + type(error).__name__
                + "); compaction aborted"
            )
            self._record_compression_failure_cooldown(30, self._last_summary_error)
            return None
        self._previous_summary = bounded
        return self._with_summary_prefix(bounded)
    finally:
        active.discard(id(self))
