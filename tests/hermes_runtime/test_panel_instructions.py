import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from hermes_runtime.standalone.panel import bridge
from hermes_runtime.standalone.panel_instructions import CONFLICT, instructions_request

pytestmark = pytest.mark.platforms("posix")


def test_instructions_save_is_scoped_and_rejects_stale_revision(tmp_path):
    assert instructions_request(tmp_path, "instructions.read", {}) == {"content": "", "revision": None}
    initial = instructions_request(tmp_path, "instructions.save", {"content": "Правила", "expectedRevision": None})
    assert instructions_request(tmp_path, "instructions.read", {}) == initial
    assert instructions_request(tmp_path, "instructions.save", {"content": "Правила", "expectedRevision": None}) == initial

    def edit(content):
        try:
            return instructions_request(tmp_path, "instructions.save", {
                "content": content, "expectedRevision": initial["revision"],
            })["content"]
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["Первый редактор", "Второй редактор"]))
    assert results.count(CONFLICT) == 1
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") in set(results) - {CONFLICT}

    with pytest.raises(ValueError, match="INVALID_AGENT_INSTRUCTIONS"):
        instructions_request(tmp_path, "instructions.read", {"path": "config.yaml"})


def test_instructions_do_not_follow_links_or_expose_target(tmp_path):
    from hermes_runtime.standalone.panel_instructions import _reply

    other = tmp_path / "private.md"
    other.write_text("other file", encoding="utf-8")
    (tmp_path / "SOUL.md").symlink_to(other)
    for method, params in (("instructions.read", {}), ("instructions.save", {"content": "new", "expectedRevision": None})):
        result = asyncio.run(_reply(tmp_path, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})))
        assert result["error"]["message"] == "AGENT_INSTRUCTIONS_UNAVAILABLE"
        assert other.read_text(encoding="utf-8") == "other file"


def test_connector_refuses_instructions_channel_without_configuration_access():
    with pytest.raises(ValueError, match="require configuration access"):
        asyncio.run(bridge({"kind": "instructions", "id": "test", "canConfigure": False}, {}, 0, "", None))


def test_file_tool_denies_edits_to_managed_soul(tmp_path, monkeypatch):
    from hermes_runtime.adapters import hermes
    from tools.file_tools_write_guards import _check_sensitive_path

    home = tmp_path.resolve()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes, "_home", home)
    assert hermes.enabled()
    assert "managed in Panel" in _check_sensitive_path(str(home / "SOUL.md"))
    assert _check_sensitive_path(str(home / "notes.md")) is None
