"""Per-cell scoped tool SDK, shared by local and remote RPC transports."""

import contextlib
import contextvars
import json

from tools.registry import tool_error

BRIDGE_TOOLS = frozenset({"__code_search__", "__code_describe__", "__code_call__"})
_scope = contextvars.ContextVar("code_execution_scope", default=None)


def current_scope():
    return _scope.get()


@contextlib.contextmanager
def bind_scope(scope):
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


class CodeToolScope:
    def __init__(self, session_id, definitions, invoke, uid=None):
        self.session_id, self.invoke, self.uid = session_id, invoke, uid
        from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS
        excluded = set(INLINE_TOOL_EXECUTORS) | {"execute_code", "delegate_task", "clarify", "manage_connections",
            "tool_search", "tool_describe", "tool_call"}
        self.definitions = {d["function"]["name"]: d for d in definitions
            if d.get("function", {}).get("name") not in excluded}
        self.run = None

    def dispatch(self, name, args, *, request_id=None):
        if name == "__code_search__":
            from tools.tool_search_catalog import build_catalog, search_catalog
            hits = search_catalog(build_catalog(list(self.definitions.values())), str(args.get("query", "")),
                limit=max(1, min(int(args.get("limit", 5)), 25)))
            return json.dumps([{"name": h.name, "description": h.description} for h in hits])
        if name == "__code_describe__":
            names = args.get("names", [])
            if not isinstance(names, list) or len(names) > 25:
                return tool_error("names must be a list of at most 25 scoped tool names")
            return json.dumps({n: self.definitions[n]["function"] for n in names if n in self.definitions})
        step = None
        if name == "__code_call__":
            name, args, step = args.get("name"), args.get("arguments", {}), args.get("step")
        if name not in self.definitions:
            return tool_error("Tool is not available to this code execution session")
        if not isinstance(args, dict) or (step is not None and (not isinstance(step, str) or not step)):
            return tool_error("arguments must be an object and step a nonempty string")
        from jsonschema import Draft7Validator
        schema = self.definitions[name]["function"].get("parameters", {})
        error = next(Draft7Validator(schema).iter_errors(args), None)
        if error:
            return tool_error("Invalid tool arguments: " + error.message)
        if self.run is None:
            return tool_error("No active code run")
        if name == "terminal":
            from tools.code_execution_rpc import _TERMINAL_BLOCKED_PARAMS
            args = {k: v for k, v in args.items() if k not in _TERMINAL_BLOCKED_PARAMS}
        return self.run.call(name, args, self.invoke, step=step, request_id=request_id)
