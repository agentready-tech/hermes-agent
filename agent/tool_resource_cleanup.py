"""Per-turn tool cleanup respects live managed operations."""

import logging
import os

from tools.terminal_tool_lifecycle import is_persistent_env

logger = logging.getLogger(__name__)


def cleanup_task_resources(agent, task_id: str) -> None:
    """Per-turn VM + browser cleanup for a task. Skips ``cleanup_vm`` for persistent
    terminal envs (``_cleanup_inactive_envs`` reaps them after ``terminal.lifetime_seconds``)
    and ``cleanup_browser`` in headed mode (the inactivity reaper handles idle sessions)."""
    import run_agent as ra
    from agent.tool_operation_lifecycle import preserve_resources
    if preserve_resources(agent, task_id):
        return

    def _headed() -> bool:
        try:
            from tools.browser_tool_cloud import _is_headed_mode
            return _is_headed_mode()
        except Exception:
            return bool(os.environ.get("AGENT_BROWSER_HEADED"))

    for label, skip, skip_what, cleanup in (
        ("VM", is_persistent_env, "cleanup_vm for persistent env", lambda: ra.cleanup_vm(task_id)),
        ("browser", lambda _tid: _headed(), "cleanup_browser for headed session", lambda: ra.cleanup_browser(task_id)),
    ):
        try:
            if skip(task_id):
                if agent.verbose_logging:
                    logging.debug(f"Skipping per-turn {skip_what} {task_id}; idle reaper will handle it.")
            else:
                cleanup()
        except Exception as e:
            if agent.verbose_logging:
                logger.warning("Failed to cleanup %s for task %s: %s", label, task_id, e)
