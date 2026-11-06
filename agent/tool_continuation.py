"""Stop checks for managed receipts, based on the owning operation and durable inbox."""

import logging
import sqlite3

logger = logging.getLogger(__name__)


def continuation_evidence(agent):
    """Only native receipts from this turn's latest batch may witness a pending call."""
    if getattr(agent, "_agentready_active_turn", None) is None:
        return []
    receipts = getattr(agent, "_managed_tool_receipts", None) or []
    if not receipts:
        return []
    from hermes_runtime.adapters.hermes import managed_home
    from hermes_runtime.adapters.tool_operations import ToolOperationStore

    store = ToolOperationStore(managed_home())
    evidence = []
    for receipt in receipts:
        identity = receipt.get("operation_id") or receipt.get("waiting_for")
        state = {"state": "not_registered", "return_path": None}
        if identity:
            try:
                state = store.continuation_state(identity, agent.session_id)
            except (OSError, sqlite3.Error):
                logger.warning("Cannot confirm managed tool continuation", exc_info=True)
                state = {"state": "unavailable", "return_path": None}
        evidence.append({"tool": receipt["tool"], "tool_call_id": receipt["tool_call_id"],
            "status": receipt["status"], "reason": receipt.get("reason"),
            "operation_id": identity, **state, "return_guaranteed": bool(state["return_path"])})
    return evidence


def review_tool_continuation(agent):
    """Return (correction, terminal notice). Never dispatch or blindly retry a tool.

    Give each skipped batch one reassessment, bounded by the existing turn limit.
    A model ignoring that correction must leave a visible unresolved outcome.
    """
    evidence = continuation_evidence(agent)
    missing = [item for item in evidence if not item["return_guaranteed"]]
    if not missing:
        return None, None
    from agent.continuation_guard import _config, _eligible

    names = ", ".join(dict.fromkeys(item["tool"] for item in missing))
    notice = (
        f"Execution check: {names} has no confirmed completion or automatic return. "
        "Any still-required work is incomplete, with no confirmed automatic continuation. "
        "Check existing outcomes before retrying an uncertain action."
    )
    if not _eligible(agent):
        budget = getattr(agent, "iteration_budget", None)
        if budget is not None and budget.remaining <= 0 and not getattr(agent, "_interrupt_requested", False):
            logger.warning("Unconfirmed tool continuation at budget limit (session=%s tools=%s)", agent.session_id, names)
            return None, notice
        return None, None
    if (getattr(agent, "_managed_tool_recheck_requested", False)
            or getattr(agent, "_continuation_guard_nudges", 0) >= _config().max_continuations):
        logger.warning("Tool continuation remains unresolved (session=%s tools=%s)", agent.session_id, names)
        return None, notice
    agent._managed_tool_recheck_requested = True
    logger.info("Native tool continuation recheck required (session=%s tools=%s)", agent.session_id, names)
    return (
        "[Continuation check] Native tool state does not confirm a future return for " + names + ". "
        "A not_started call has not performed its work. Handle the latest user input, then "
        "re-evaluate the skipped work under the current instructions. If it is still required, "
        "perform the next authorized step now. For a busy resource or an uncertain earlier "
        "operation, establish its actual outcome first; do not repeat an unknown external action. "
        "Do not promise a completion notification without a confirmed owning operation and "
        "return path. If blocked or superseded, explain that clearly instead of promising later work.",
        None,
    )
