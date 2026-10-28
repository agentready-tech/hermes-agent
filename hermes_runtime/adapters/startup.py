"""Resume the managed home after bind, without requiring a human to reopen chat."""

import asyncio
import hashlib
import json
import logging
from urllib.parse import quote

from hermes_runtime.adapters.hermes import enabled, managed_home
from hermes_runtime.adapters.inbox import HomeInbox
from hermes_runtime.adapters.state import current_home_session_id

logger = logging.getLogger(__name__)


def _wake_inbox(inbox, *, reconcile=True):
    from tools.delegate_project_recovery import publish_wakeup
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    from hermes_runtime.adapters.turns import home_turn_active
    if reconcile:
        ToolOperationStore(inbox.home).recover()
    inbox.acknowledge_recorded()
    if home_turn_active():
        return False
    if pending := inbox.pending():
        identity = "home-recovery-" + hashlib.sha256(json.dumps([i for i, _ in pending]).encode()).hexdigest()
        origin = {"session_key": "", "origin_ui_session_id": ""}
        for _, content in pending:
            if isinstance(content, str) and content.startswith("[AgentReady event]\n"):
                try:
                    header = json.loads(content.split("\n", 1)[1].split("\n\n", 1)[0])
                except (ValueError, TypeError):
                    continue
                if header.get("source") == "tool_operation" and (route := header.get("metadata", {}).get("origin")):
                    origin = route
                    break
        return publish_wakeup(identity, current_home_session_id(), origin,
            "Accepted messages remain in the managed inbox. Continue the home conversation; "
            "the saved messages will be delivered at a normal turn boundary. Check existing outcomes before repeating actions.")
    return False


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
    from hermes_runtime.session_event import rpc
    from tools.async_delegation import restore_undelivered_completions
    from tools.process_registry import process_registry
    import websockets

    inbox = HomeInbox()
    _wake_inbox(inbox)
    restore_undelivered_completions(process_registry.completion_queue)
    if not _work_pending():
        return
    from hermes_cli.web_server import app
    if getattr(app.state, "auth_required", False):
        # This client lives inside the bound server. Use its existing private
        # credential, exactly like the native embedded TUI, in gated bind mode.
        from hermes_cli.dashboard_auth.ws_tickets import internal_ws_credential
        auth = "internal=" + quote(internal_ws_credential(), safe="")
    else:
        auth = "token=" + quote(token, safe="")
    async with websockets.connect(f"ws://127.0.0.1:{port}/api/ws?{auth}",
                                  open_timeout=10, max_size=8 * 1024 * 1024) as socket:
        opened = await rpc(socket, "session.resume", {"session_id": current_home_session_id(),
            "source": "agentready", "close_on_disconnect": False}, "managed-startup-resume")
        # Keep a live transport while native notification delivery wakes the parent
        # and its project children. No model turn is manufactured for an idle home.
        loop = asyncio.get_running_loop()
        next_scan = loop.time() + 2
        while True:
            # New admissions can arrive after the active owner stopped accepting
            # IPC, while this transport still waits for that owner's idle event.
            # Refresh the deterministic wakeup so this window cannot strand them.
            if loop.time() >= next_scan:
                if _wake_inbox(inbox):
                    restore_undelivered_completions(process_registry.completion_queue)
                next_scan = loop.time() + 2
            if not _work_pending():
                active = await rpc(socket, "session.active_list", {}, "managed-recovery-status")
                owner = next((s for s in active.get("sessions", []) if s["id"] == opened["session_id"]), None)
                if owner and owner["status"] == "idle" and not _work_pending():
                    return
            try:
                await asyncio.wait_for(socket.recv(), timeout=2)
            except TimeoutError:
                continue


def start_recovery(port, token):
    if not enabled():
        return None
    from agent.memory_provider import spawn_context_thread

    def recover():
        try:
            from hermes_runtime.standalone.config import settings
            from hermes_runtime.standalone.events import watch_home
            coroutine = watch_home(port, token) if settings(managed_home()).get("enabled") else resume_after_bind(port, token)
            asyncio.run(coroutine)
        except Exception as error:  # health: allow BLE001 -- exception traces can contain the WebSocket session token
            # Connection exception strings may contain the session-token URL.
            logger.warning("Managed startup recovery deferred (%s); accepted work remains saved", type(error).__name__)
    thread = spawn_context_thread(recover, name="managed-startup-recovery")
    thread.start()
    return thread
