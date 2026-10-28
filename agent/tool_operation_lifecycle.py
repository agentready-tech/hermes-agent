"""Managed tool execution receipts; native middleware and authorization remain in charge."""

import copy
import json
import uuid

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
from hermes_runtime.adapters.hermes import enabled, managed_home


class DeferredToolResult(str):
    """A receipt, never a completed tool outcome or guardrail observation."""


def incoming_event(agent):
    if not enabled() or getattr(agent, "_interrupt_requested", False):
        return False
    turn = getattr(agent, "_agentready_active_turn", None)
    return turn is not None and turn.wakeup.is_set()


def resource_key(name):
    if name.startswith(("browser_", "computer_", "desktop_")):
        return "desktop"
    if name in {"terminal", "read_file", "write_file", "patch", "search_files", "list_directory"}:
        return "workspace"
    if name.startswith("mcp_"):
        from tools.mcp_tool_common import _core
        server = _core._mcp_tool_server_names.get(name)
        return "mcp:" + (str(server) if server else "shared")
    return "tool:" + name


def begin_group(agent, messages):
    """Snapshot durable call identities already assigned to the assistant row."""
    from agent.message_metadata import index_tool_call_uids
    agent._managed_tools_deferred = False
    if not enabled() or getattr(agent, "_agentready_active_turn", None) is None:
        return
    agent._managed_tool_group = uuid.uuid4().hex
    index = {}
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            index_tool_call_uids(index, message)
    agent._managed_tool_call_uids = index


def prepare_operation(agent, name, args, task_id, call_id):
    """Inline tools own live agent state; their existing execution path stays synchronous."""
    if not enabled() or getattr(agent, "_agentready_active_turn", None) is None:
        return None
    if name in INLINE_TOOL_EXECUTORS or name in {"delegate_task", "clarify", "manage_connections"}:
        return None
    if name in (getattr(agent, "_context_engine_tool_names", None) or ()):
        return None
    memory = getattr(agent, "_memory_manager", None)
    if memory and memory.has_tool(name):
        return None
    uid = getattr(agent, "_managed_tool_call_uids", {}).get(call_id)
    if not uid:
        # Native persistence assigns occurrence IDs before execution. Legacy/test
        # callers without one must not acquire an unstable replay identity.
        return None
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    store = ToolOperationStore(managed_home())
    row, execute = store.admit(agent.session_id or task_id, uid, call_id, name, args,
        resource_key(name), agent._managed_tool_group)
    return ToolOperation(agent, store, row, execute)


def finish_group(agent):
    if enabled() and getattr(agent, "_agentready_active_turn", None) is not None:
        from hermes_runtime.adapters.tool_operations import ToolOperationStore
        ToolOperationStore(managed_home()).recover(finished_group=getattr(agent, "_managed_tool_group", None))


class ToolOperation:
    def __init__(self, agent, store, row, execute):
        self.agent, self.store, self.row, self.execute = agent, store, row, execute

    def replay(self):
        if self.execute:
            return None
        if "operation_id" not in self.row:
            self.agent._managed_tools_deferred = True
            return DeferredToolResult(json.dumps(self.row))
        if self.row["state"] in {"completed", "failed"}:
            return json.loads(self.row["result_json"])
        return self.defer("running" if self.row["state"] == "running" else "outcome_unknown")

    def receipt(self, status="running"):
        self.agent._managed_tools_deferred = True
        message = ("Execution continues. Its result will arrive as an event in this session. "
            "Do not rerun this call while waiting." if status == "running" else
            "Execution outcome is not confirmed. Check the external outcome before retrying. "
            "Any available completion will be delivered in this session.")
        return DeferredToolResult(json.dumps({"status": status,
            "operation_id": self.row["operation_id"], "tool": self.row["tool_name"],
            "message": message}))

    def complete(self, result):
        # Apply the existing spill policy before a large result enters the inbox.
        from agent.tool_executor import _budget_for_agent, _persist_multimodal_text_parts
        from tools.terminal_tool_lifecycle import get_active_env
        from tools.tool_result_storage import maybe_persist_tool_result
        budget = _budget_for_agent(self.agent)
        env = get_active_env(self.row["session_id"])
        if isinstance(result, dict):
            bounded = _persist_multimodal_text_parts(result, self.row["tool_name"], self.row["operation_id"], env, budget)
        else:
            bounded = maybe_persist_tool_result(content=result, tool_name=self.row["tool_name"],
                tool_use_id=self.row["operation_id"], env=env, config=budget)
        self.store.complete(self.row["operation_id"], bounded)

    def failed(self, error):
        self.store.complete(self.row["operation_id"], {"error": str(error)}, failed=True)

    def defer(self, status="running"):
        self.store.defer(self.row["operation_id"])
        return self.receipt(status)


def execution_agent(agent, operation):
    # Middleware callbacks retain the original turn/model IDs while the owner is
    # free to start the next request. Locks/cancellation handles remain shared.
    return copy.copy(agent) if operation is not None else agent


def append_receipt(agent, messages, name, call_id, result):
    from agent.tool_dispatch_helpers import make_tool_result_message
    from agent.tool_executor import _flush_session_db_after_tool_progress
    agent._current_tool = None
    disposition = "none" if json.loads(result).get("status") == "not_started" else "unknown"
    messages.append(make_tool_result_message(name, result, call_id, effect_disposition=disposition))
    return _flush_session_db_after_tool_progress(agent, messages, stage="deferred tool receipt")


def skip_after_defer(agent, messages, calls):
    if not getattr(agent, "_managed_tools_deferred", False):
        return False
    from agent.message_sanitization import coalesce_tool_call_id
    for call in calls:
        result = DeferredToolResult(json.dumps({"status": "not_started", "tool": call.function.name,
            "message": "An earlier call is still running. Re-evaluate this dependent call after its completion event."}))
        if not append_receipt(agent, messages, call.function.name, coalesce_tool_call_id(call), result):
            break
    return True


def preserve_resources(agent, task_id):
    if not enabled():
        return False
    from hermes_runtime.adapters.tool_operations import ToolOperationStore
    return ToolOperationStore(managed_home()).has_running(task_id)
