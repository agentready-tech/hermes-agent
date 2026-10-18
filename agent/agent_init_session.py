"""Per-agent session state, checkpoint resources and mandatory main-session policy."""

from contextlib import suppress
from datetime import datetime

from hermes_constants import get_hermes_home
from hermes_state_ids import new_session_id


def init_session_state(agent, session_id, session_db, parent_session_id, reasoning_config, max_tokens,
    checkpoints_enabled, checkpoint_max_snapshots, checkpoint_max_total_size_mb, checkpoint_max_file_size_mb):
    from agent.agent_init import _publish_session_id, _set_defaults, _SESSION_STATE

    agent.session_start = datetime.now()
    agent.session_id = session_id or new_session_id(agent.session_start)
    _publish_session_id(agent.session_id)

    # Keep sessions/ unconditionally for request_dump_*.json debug breadcrumbs.
    agent.logs_dir = get_hermes_home() / "sessions"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    _set_defaults(agent, _SESSION_STATE)

    # Filesystem checkpoint manager (transparent — not a tool)
    from tools.checkpoint_manager import CheckpointManager
    agent._checkpoint_mgr = CheckpointManager(
        enabled=checkpoints_enabled, max_snapshots=checkpoint_max_snapshots,
        max_total_size_mb=checkpoint_max_total_size_mb,
        max_file_size_mb=checkpoint_max_file_size_mb,
    )

    agent._session_db = session_db  # optional SQLite store (CLI/gateway-provided)
    agent._parent_session_id = parent_session_id
    agent._session_init_model_config = {
        "max_iterations": agent.max_iterations,
        "reasoning_config": reasoning_config,
        "max_tokens": max_tokens,
    }
    # Process-scoped --yolo is persisted so `hermes --resume` restores the bypass
    # (SessionDB.session_yolo_enabled); session-scoped /yolo toggles persist separately.
    with suppress(Exception):
        from tools.approval import _YOLO_MODE_FROZEN
        if _YOLO_MODE_FROZEN:
            agent._session_init_model_config["yolo_mode"] = True

    # In-memory todo list for task planning (one per agent/session)
    from tools.todo_tool import TodoStore
    agent._todo_store = TodoStore()
    from hermes_runtime.standalone.instructions import install_main_schedule_instructions
    install_main_schedule_instructions(agent)
