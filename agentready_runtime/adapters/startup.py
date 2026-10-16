"""Resume the managed home after bind, without requiring a human to reopen chat."""

import asyncio
import hashlib
import json
import logging
from urllib.parse import quote

from agentready_runtime.adapters.hermes import enabled, managed_home
from agentready_runtime.adapters.inbox import HomeInbox
from agentready_runtime.adapters.state import current_home_session_id

logger = logging.getLogger(__name__)


def _work_pending():
    from tui_gateway.turn_marker import read_turn_marker
    from tools.delegate_project_store import _transaction
    sid = current_home_session_id()
    marker = read_turn_marker(managed_home(), sid)
    if HomeInbox().pending() or (marker and marker.get("auto_continue", True)):
        return True
    with _transaction() as conn:
        return bool(conn.execute("SELECT 1 FROM delegated_project_messages m JOIN delegated_projects p USING(subagent_id) "
            "WHERE p.parent_session_id IN (?,?) AND p.archived=0 AND p.paused=0 AND m.state IN ('pending','running') LIMIT 1",
            (sid, "agentready_home")).fetchone() or conn.execute("SELECT 1 FROM async_delegations WHERE parent_session_id IN (?,?) AND delivery_state='pending' LIMIT 1", (sid, "agentready_home")).fetchone())


async def resume_after_bind(port, token):
    from agentready_runtime.session_event import rpc
    from tools.async_delegation import restore_undelivered_completions
    from tools.process_registry import process_registry
    from tools.delegate_project_recovery import publish_wakeup
    import websockets

    inbox = HomeInbox()
    inbox.acknowledge_recorded()
    if pending := inbox.pending():
        identity = "home-recovery-" + hashlib.sha256(json.dumps([i for i, _ in pending]).encode()).hexdigest()
        publish_wakeup(identity, current_home_session_id(), {"session_key": "", "origin_ui_session_id": ""},
            "Accepted messages remain in the managed inbox after restart. Continue the home conversation; "
            "the saved messages will be delivered at a normal turn boundary. Check existing outcomes before repeating actions.")
    restore_undelivered_completions(process_registry.completion_queue)
    if not _work_pending():
        return
    async with websockets.connect(f"ws://127.0.0.1:{port}/api/ws?token={quote(token, safe='')}",
                                  open_timeout=10, max_size=8 * 1024 * 1024) as socket:
        opened = await rpc(socket, "session.resume", {"session_id": current_home_session_id(),
            "source": "agentready", "close_on_disconnect": False}, "managed-startup-resume")
        # Keep a live transport while native notification delivery wakes the parent
        # and its project children. No model turn is manufactured for an idle home.
        while True:
            if not _work_pending():
                active = await rpc(socket, "session.active_list", {}, "managed-recovery-status")
                owner = next((s for s in active.get("sessions", []) if s["id"] == opened["session_id"]), None)
                if owner and owner["status"] == "idle" and not _work_pending():
                    return
            try:
                await asyncio.wait_for(socket.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue


def start_recovery(port, token):
    if not enabled():
        return None
    from agent.memory_provider import spawn_context_thread

    def recover():
        try:
            asyncio.run(resume_after_bind(port, token))
        except Exception as error:
            # Connection exception strings may contain the session-token URL.
            logger.warning("Managed startup recovery deferred (%s); accepted work remains saved", type(error).__name__)
    thread = spawn_context_thread(recover, name="managed-startup-recovery")
    thread.start()
    return thread
