"""Required policy and direct tools reach the first real request, without model I/O."""
import copy
from contextlib import nullcontext

import pytest


def _schema(name):
    return {"name": name, "description": "Synthetic permitted capability " + name,
            "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # Never derive a write target from ambient get_hermes_home(). Collection
    # also runs under canonical tests/conftest.py and a disposable OS HOME.
    home = (tmp_path / "profile").resolve()
    assert home.is_relative_to(tmp_path.resolve())
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_constants import get_hermes_home
    assert get_hermes_home().resolve() == home
    (home / "config.yaml").write_text(
        "compression:\n  enabled: false\nauxiliary:\n  title_generation:\n    enabled: false\n",
        encoding="utf-8")
    from openai.types.chat import ChatCompletion
    from run_agent import AIAgent
    from hermes_state import SessionDB
    import model_tools
    from tools import registry as registry_module
    from tools.registry import ToolRegistry
    from tools.tool_search import ToolSearchConfig
    from toolsets import _HERMES_CORE_TOOLS

    registry = ToolRegistry()
    essential = {n for n in _HERMES_CORE_TOOLS if n.startswith("browser_")} | {"computer_use", "delegate_task"}
    for name in sorted(essential):
        toolset = "browser" if name.startswith("browser_") else "delegation" if name == "delegate_task" else "computer_use"
        registry.register(name=name, toolset=toolset, schema=_schema(name), handler=lambda *_a, **_kw: "{}")
    registry.register(name="mcp_policy_probe", toolset="mcp-policy-probe", schema=_schema("mcp_policy_probe"),
                      handler=lambda *_a, **_kw: "{}")
    monkeypatch.setattr(registry_module, "registry", registry)
    monkeypatch.setattr(model_tools, "registry", registry)
    # Schema rewriting and capability probes are not under test; the real
    # selection, registry filtering, deferral and agent request path still run.
    monkeypatch.setattr(model_tools, "_apply_dynamic_schemas", lambda defs: defs)
    monkeypatch.setattr("tools.tool_search.load_config", lambda: ToolSearchConfig.from_raw({"enabled": "on", "defer": sorted(essential)}))
    monkeypatch.setattr(model_tools, "_resolve_active_context_length", lambda: 200_000)
    seen = []

    def model(self, payload, *_args, **_kwargs):
        seen.append(copy.deepcopy(payload))
        return ChatCompletion(id="policy-only", object="chat.completion", created=1, model=self.model,
            choices=[{"index": 0, "message": {"role": "assistant", "content": "Synthetic request captured"}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105})

    monkeypatch.setattr(AIAgent, "_interruptible_api_call", model)
    monkeypatch.setattr(AIAgent, "_interruptible_streaming_api_call", model)
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", home / "state.db")
    with SessionDB(db_path=home / "state.db") as db:
        yield db, seen, essential


@pytest.mark.parametrize("role,model", [("main", "claude-test"), ("legacy-owner", "gemini-test"), ("child", "qwen-test")])
@pytest.mark.parametrize("permitted", [True, False])
def test_first_request_policy_and_direct_tools(runtime, role, model, permitted):
    from agent.delegation_context import delegated_child_context
    from agent.prompt_builder import DESKTOP_APPLICATION_UI_POLICY
    from run_agent import AIAgent
    db, seen, essential = runtime
    legacy = "Preserved old system prompt and identity."
    db.create_session(role, source="cli", system_prompt=legacy if role != "main" else None)
    if role != "main":
        db.append_message(role, "user", "Original work")
        db.append_message(role, "assistant", "Original result")
    before = db.get_messages_as_conversation(role)
    scope = delegated_child_context("policy-child") if role == "child" else nullcontext()
    enabled = ["browser", "computer_use", "delegation", "mcp-policy-probe"] if permitted else ["terminal"]
    with scope:
        agent = AIAgent(model=model, provider="custom", base_url="http://127.0.0.1:9/v1", api_key="test-only",
            api_mode="chat_completions", session_id=role, session_db=db, enabled_toolsets=enabled,
            platform="subagent" if role == "child" else "cli", side_agent=role == "child",
            skip_context_files=True, load_soul_identity=False, skip_memory=True, quiet_mode=True,
            ephemeral_system_prompt="Existing host suffix")
    try:
        agent.run_conversation("Inspect the desktop application", conversation_history=before)
        first = seen[0]
        system = first["messages"][0]["content"]
        assert system.count(DESKTOP_APPLICATION_UI_POLICY) == 1
        assert "even when computer_use is absent" in system
        assert "empty or incomplete accessibility tree" in system
        assert "human_has_control/HUMAN_CONTROL" in system
        assert "code, files and non-UI data processing" in system
        assert "proper browser tools for website automation" in system
        names = {t["function"]["name"] for t in first.get("tools", [])}
        if permitted:
            assert essential <= names
            assert "mcp_policy_probe" not in names
            assert {"tool_search", "tool_describe", "tool_call"} <= names
        else:
            assert not (names & essential)
        if role != "main":
            assert agent._cached_system_prompt == legacy
            assert db.get_session(role)["system_prompt"] == legacy
        assert db.get_messages_as_conversation(role)[:len(before)] == before
        agent.run_conversation("Continue", conversation_history=db.get_messages_as_conversation(role))
        assert seen[-1]["messages"][0]["content"] == system
    finally:
        agent.close()


@pytest.mark.parametrize("explicit", [False, True])
def test_long_catalog_keeps_only_permitted_essential_tools_direct(monkeypatch, explicit):
    from tools import registry as registry_module
    from tools.registry import ToolRegistry
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs
    from toolsets import _HERMES_CORE_TOOLS
    registry = ToolRegistry()
    essential = {n for n in _HERMES_CORE_TOOLS if n.startswith("browser_")} | {"computer_use", "delegate_task"}
    names = essential | {"read_window_below"} | {f"mcp_bulk_{n}" for n in range(100)}
    for name in names:
        registry.register(name=name, toolset="mcp-probe" if name.startswith("mcp_") else "computer_use",
            schema={**_schema(name), "description": "Bounded catalog " * 100}, handler=lambda *_a, **_kw: "{}",
            check_fn=(lambda: False) if name == "computer_use" else None)
    monkeypatch.setattr(registry_module, "registry", registry)
    raw = {"enabled": "on", "listing_max_tokens": 200}
    if explicit:
        raw["defer"] = sorted(names)
    incoming = registry.get_definitions(names, quiet=True)
    result = assemble_tool_defs(incoming, context_length=1000, config=ToolSearchConfig.from_raw(raw))
    visible = {t["function"]["name"] for t in result.tool_defs}
    assert essential - {"computer_use"} <= visible
    assert "computer_use" not in visible  # A failed capability check never grants a tool.
    assert "read_window_below" not in visible
    assert not any(n.startswith("mcp_bulk_") for n in visible)


def test_wire_policy_is_idempotent_and_preserves_input():
    from agent.prompt_builder import DESKTOP_APPLICATION_UI_POLICY, enforce_desktop_ui_policy
    for role in ("system", "developer"):
        messages = [{"role": role, "content": "Saved prefix"}, {"role": "user", "content": "Work"}]
        original = copy.deepcopy(messages)
        once, prompt = enforce_desktop_ui_policy(messages)
        twice, second = enforce_desktop_ui_policy(once)
        assert twice == once and second == prompt
        assert prompt.count(DESKTOP_APPLICATION_UI_POLICY) == 1
        assert messages == original
