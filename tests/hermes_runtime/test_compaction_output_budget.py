"""Managed compaction must finish inside the shared provider's output budget."""

from copy import deepcopy
import re
from types import SimpleNamespace

from agent.context_compressor import ContextCompressor
from hermes_constants import get_hermes_home
from hermes_runtime.adapters import hermes


def response(text, reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text, reasoning_content=None), finish_reason=reason,
    )])


def compressor(monkeypatch):
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hermes, "_home", home.resolve())
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *a, **kw: 1_048_576)
    value = ContextCompressor(model="~deepseek/deepseek-flash-latest", provider="custom", quiet_mode=True,
                              protect_first_n=2, protect_last_n=2)
    value._session_id = "agentready_home"
    value.max_summary_tokens = 10_000
    return value


def turns():
    result = [{"role": "user", "content": "Finish invoice INV-42 without sending it twice."}]
    for i in range(30):
        result.extend([
            {"role": "assistant", "content": "Check the saved report.", "tool_calls": [{
                "id": f"report-{i}", "type": "function", "function": {"name": "terminal", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": f"report-{i}", "content": "Recorded invoice facts. " * 800},
        ])
    result.append({"role": "user", "content": "Continue checking INV-42."})
    return result


def requested_budget(prompt):
    return int(re.search(r"Target ~(\d+) tokens", prompt).group(1))


def test_total_summary_budget_fits_gateway_cap_including_detailed_log(monkeypatch):
    value = compressor(monkeypatch)
    requests = []

    def capped_provider(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        requests.append(prompt)
        if requested_budget(prompt) > 8192:
            return response("PARTIAL MUST NOT BECOME A CHECKPOINT", "length")
        return response("## Goal\nFinish invoice INV-42.\n## Active State\nAwaiting confirmation.")

    monkeypatch.setattr("agent.context_compressor.call_llm", capped_provider)
    result = value._generate_summary(turns())
    assert result and "Finish invoice INV-42" in result
    assert len(requests) == 1
    total = requested_budget(requests[0])
    detail = int(re.search(r"Spend up to ~(\d+) tokens here", requests[0]).group(1))
    assert 0 < detail < total < 8192
    assert "PARTIAL MUST NOT" not in result


def test_length_stop_retries_once_with_smaller_complete_summary(monkeypatch):
    value = compressor(monkeypatch)
    budgets = []
    original = turns()
    before = deepcopy(original)

    def provider(**kwargs):
        budgets.append(requested_budget(kwargs["messages"][0]["content"]))
        return response("PARTIAL SECRET OF THE UNFINISHED CHECKPOINT", "length") if len(budgets) == 1 else response(
            "## Goal\nFinish invoice INV-42.\n## Active State\nNothing has been sent.")

    monkeypatch.setattr("agent.context_compressor.call_llm", provider)
    result = value._generate_summary(original)
    assert result and "Nothing has been sent" in result
    assert len(budgets) == 2 and budgets[1] < budgets[0]
    assert original == before
    assert not value._last_summary_truncated_failure
    assert "PARTIAL SECRET" not in value._previous_summary
    for path in (get_hermes_home() / "workspace/.agentready/checkpoints").rglob("*.md"):
        assert "PARTIAL SECRET" not in path.read_text()


def test_repeated_truncation_is_bounded_and_preserves_history(monkeypatch):
    value = compressor(monkeypatch)
    attempts = []
    original = turns()
    before = deepcopy(original)

    def provider(**kwargs):
        attempts.append(kwargs)
        return response("PARTIAL NEVER PERSISTED", "length")

    monkeypatch.setattr("agent.context_compressor.call_llm", provider)
    result = value.compress(original, current_tokens=999_999, force=True)
    assert result == original == before
    assert len(attempts) == 2
    assert value._last_compress_aborted
    assert value._previous_summary is None
    assert not list((get_hermes_home() / "workspace/.agentready/checkpoints").rglob("*.md"))
