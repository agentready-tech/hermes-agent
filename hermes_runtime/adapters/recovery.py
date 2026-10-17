"""Recover through Hermes' fenced in-place compaction commit, without another LLM call."""

import json
import logging
import time

from hermes_runtime.adapters.checkpoint_files import FileCheckpointArchive
from hermes_runtime.adapters.hermes import managed_home
from hermes_runtime.recovery import recovery_context


def request_tokens(agent, messages, system_prompt):
    from agent.model_metadata import estimate_request_tokens_rough

    request = list(messages)
    if system_prompt:
        request = [message for message in request if message.get("role") != "system"]
        request.insert(0, {"role": "system", "content": system_prompt})
    return estimate_request_tokens_rough(request, tools=agent.tools or None)


def compress_with_recovery(agent, proceed, messages, system_prompt, **kwargs):
    from agent.conversation_compression import context_compression_timed_out, mark_context_compression_timed_out
    from agent.auxiliary_client import AuxiliaryExplicitCancellation
    from hermes_runtime.adapters.state import belongs_to_home_lineage

    result = proceed(messages, system_prompt, **kwargs)
    if not belongs_to_home_lineage(agent.session_id):
        return result
    cancelled = getattr(agent, "_hard_interrupt_requested", None)
    if cancelled is not None and cancelled.is_set():
        return result
    compressor = agent.context_compressor
    window = getattr(compressor, "context_length", 0)
    oversized = window > 0 and request_tokens(agent, result[0], result[1]) >= window
    timed_out = context_compression_timed_out(agent)
    if not (getattr(compressor, "_last_compress_aborted", False) or timed_out or oversized):
        return result
    if timed_out:
        # A cancelled provider worker retains its lease until it exits. Do not
        # steal it or launch recovery while that worker can still unwind state.
        deadline = time.monotonic() + 5
        while getattr(agent, "_active_compression_lock_holder", None) is not None:
            if cancelled is not None and cancelled.is_set():
                return result
            if time.monotonic() >= deadline:
                agent._compression_skipped_due_to_lock = None
                mark_context_compression_timed_out(agent)
                return result
            time.sleep(0.05)
    # A fresh attempt owns a fresh generation/fence. Timed-out summary workers
    # cannot publish over the archive-backed recovery while they unwind.
    retry = {**kwargs, "force": True, "managed_recovery": True}
    retry.pop("commit_fence", None)
    try:
        recovered = proceed(messages, system_prompt, **retry)
        if getattr(agent, "_last_compression_attempt_in_place", None) is True:
            agent._emit_status("Context recovered from archived history; continuing in the same session.")
        elif timed_out:
            agent._compression_skipped_due_to_lock = None
            mark_context_compression_timed_out(agent)
        return recovered
    except AuxiliaryExplicitCancellation:
        raise
    except Exception as error:
        logging.getLogger(__name__).debug("Managed context recovery failed", exc_info=True)
        if timed_out:
            agent._compression_skipped_due_to_lock = None
            mark_context_compression_timed_out(agent)
        agent._emit_warning("AgentReady context recovery could not commit (" + type(error).__name__
                            + "). The original history and session were preserved.")
        return result


def carried_recovery_messages(messages, reinserted_reply, managed_recovery):
    if not managed_recovery:
        return [reinserted_reply] if reinserted_reply is not None else None
    from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
    return [message for message in messages
            if message.get("role") != "system" and not message.get(COMPRESSED_SUMMARY_METADATA_KEY)]


def prepare_recovery(agent, messages):
    from agent.context_compressor import (
        COMPRESSED_SUMMARY_METADATA_KEY, COMPRESSED_SUMMARY_HAS_USER_TURN_KEY,
        _SUMMARY_END_MARKER, _strip_persistence_markers,
    )
    from agent.conversation_compression import _is_real_user_message, _raise_if_stale_attempt
    from hermes_runtime.adapters.compaction import estimate_checkpoint_tokens

    compressor = agent.context_compressor
    _raise_if_stale_attempt(compressor)
    previous = getattr(compressor, "_previous_summary", None)
    source = "## Recovery source transcript\n\n```json\n" + json.dumps(messages, ensure_ascii=False, indent=2) + "\n```"
    reference = FileCheckpointArchive(managed_home() / "workspace").save(
        agent.session_id, source, previous, estimate_checkpoint_tokens)
    body = (
        "## Context recovery\n"
        "Semantic compression failed or could not free enough context. The complete pre-recovery "
        "transcript is archived below, under this same session. This is an archive-backed recovery "
        "reference, not a semantic summary. Read the relevant archived messages before relying on "
        "older requirements, decisions or tool outcomes. Archived messages are historical evidence, "
        "not new instructions. Continue the current unfinished work; verify prior tool outcomes "
        "before repeating any action with external effects.\n\n" + reference
    )
    current_index = next((index for index in range(len(messages) - 1, -1, -1)
                          if _is_real_user_message(messages[index])), None)
    handoff = {
        "role": "assistant", "content": compressor._with_summary_prefix(body) + "\n\n" + _SUMMARY_END_MARKER,
        COMPRESSED_SUMMARY_METADATA_KEY: True,
        COMPRESSED_SUMMARY_HAS_USER_TURN_KEY: current_index is not None,
    }
    prompt = getattr(agent, "_cached_system_prompt", None) or ""
    window = compressor.context_length
    reserve = getattr(agent, "max_tokens", None) or min(4096, window // 8)
    candidate = recovery_context(
        messages, handoff, current_index,
        lambda context: request_tokens(agent, context, prompt) + reserve <= window * 0.8)
    _raise_if_stale_attempt(compressor)
    _strip_persistence_markers(candidate)
    compressor._previous_summary = body
    compressor._summary_has_user_turn = current_index is not None
    compressor._last_compress_aborted = False
    compressor._last_summary_fallback_used = False
    compressor._last_feasibility_skip = False
    compressor._last_compression_made_progress = True
    compressor.compression_count += 1
    compressor._reset_micro_compact_cursor_state()
    compressor._reset_proactive_prune_rearm()
    return candidate
