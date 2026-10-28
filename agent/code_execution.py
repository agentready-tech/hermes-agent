"""Bind programmatic calls to the same native policy pipeline as model calls."""

import copy

from tools.code_execution_context import CodeToolScope, bind_scope


def code_execution_scope(agent, task_id, call_id):
    import model_tools
    from agent import tool_executor as executor
    from tools.registry import registry

    owner = copy.copy(agent)
    definitions = model_tools.get_tool_definitions(enabled_toolsets=getattr(owner, "enabled_toolsets", None),
        disabled_toolsets=getattr(owner, "disabled_toolsets", None), quiet_mode=True, skip_tool_search_assembly=True) or []
    definitions = [d for d in definitions if registry.get_entry(d.get("function", {}).get("name", "")) is not None]

    def invoke(name, args, step_id):
        def dispatch(final_args):
            with model_tools.suppress_post_tool_call_hook():
                return model_tools.handle_function_call(name, final_args, task_id=task_id,
                    session_id=owner.session_id, turn_id=getattr(owner, "_current_turn_id", ""),
                    api_request_id=getattr(owner, "_current_api_request_id", ""), tool_call_id=step_id,
                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
                    enabled_toolsets=getattr(owner, "enabled_toolsets", None),
                    disabled_toolsets=getattr(owner, "disabled_toolsets", None))
        with executor._registered_tool_worker(owner):
            managed = executor._run_agent_tool_execution_middleware(owner, function_name=name, function_args=args,
                effective_task_id=task_id, tool_call_id=step_id, execute=dispatch)
        if not managed.blocked:
            executor._ToolCallRef(name, managed.args, task_id, step_id, managed.middleware_trace).emit_post(owner, managed.result)
        return managed.result

    uid = getattr(owner, "_managed_tool_call_uids", {}).get(call_id)
    return bind_scope(CodeToolScope(owner.session_id or task_id, definitions, invoke, uid))
