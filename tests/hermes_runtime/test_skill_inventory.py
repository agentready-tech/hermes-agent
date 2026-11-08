"""Own-profile raw text reads and closed reverse channel, with no agent turn or install path."""

import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_runtime.skill_inventory import SkillReadError
from hermes_runtime.skill_inventory import CAPABILITY, personal_content, personal_inventory, system_content, system_inventory


def personal(home, name="own-example", *, folder=None, body="Own instructions."):
    path = home / "skills" / (folder or name) / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Own workflow.\n---\n{body}\n")
    return path


@pytest.mark.parametrize("bom", [False, True])
def test_personal_inventory_and_detail_keep_raw_text_and_honest_provenance(tmp_path, monkeypatch, bom):
    home, foreign = tmp_path / "own", tmp_path / "foreign"
    path = personal(home, body="Literal ${PRIVATE_ENV} must stay literal.")
    if bom:
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    personal(foreign, body="Foreign body.")
    monkeypatch.setenv("HERMES_HOME", str(foreign))
    monkeypatch.setenv("PRIVATE_ENV", "NOT_FOR_UI")
    result = personal_inventory(home)
    assert result["skills"][0]["origin"] == "unknown"
    result = personal_content(home, "own-example")
    assert "${PRIVATE_ENV}" in result["content"] and "NOT_FOR_UI" not in json.dumps(result)
    assert result["bytes"] == len(path.read_bytes())
    assert result["content"].encode("utf-8") == path.read_bytes()
    assert result["sha256"] == hashlib.sha256(result["content"].encode("utf-8")).hexdigest()
    assert str(home) not in json.dumps(result) and "Foreign body" not in json.dumps(result)
    lock = home / "skills" / ".hub" / "lock.json"
    lock.parent.mkdir()
    lock.write_text(json.dumps({"version": 1, "installed": {"own-example": {"install_path": "own-example"}}}))
    assert personal_inventory(home)["skills"][0]["origin"] == "installed"


def test_canonical_name_ambiguity_refuses_instead_of_picking_a_path(tmp_path):
    personal(tmp_path, folder="first")
    personal(tmp_path, folder="second")
    rows = personal_inventory(tmp_path)["skills"]
    assert len(rows) == 2 and all(row["ambiguous"] for row in rows)
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_AMBIGUOUS_NAME"):
        personal_content(tmp_path, "own-example")


def test_foreign_symlink_and_oversized_resource_are_refused(tmp_path):
    home, other = tmp_path / "own", tmp_path / "other"
    source = personal(other)
    alias = home / "skills" / "own-example"
    alias.mkdir(parents=True)
    (alias / "SKILL.md").symlink_to(source)
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESOURCE"):
        personal_content(home, "own-example")
    (alias / "SKILL.md").unlink()
    (alias / "SKILL.md").write_text("x" * (64 * 1024 + 1))
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESOURCE_TOO_LARGE"):
        personal_inventory(home)


def test_inventory_limit_is_explicit(tmp_path):
    for index in range(251):
        personal(tmp_path, f"example-{index}")
    result = personal_inventory(tmp_path)
    assert len(result["skills"]) == 250 and result["truncated"]
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_INVENTORY_TRUNCATED"):
        personal_content(tmp_path, "example-0")


@pytest.mark.platforms("posix")
def test_nonregular_skill_resource_is_refused_before_open(tmp_path):
    target = tmp_path / "skills" / "pipe-skill" / "SKILL.md"
    target.parent.mkdir(parents=True)
    os.mkfifo(target)
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESOURCE"):
        personal_inventory(tmp_path)


@pytest.mark.asyncio
async def test_closed_bridge_rejects_mutations_and_selectors_before_any_http(tmp_path):
    from hermes_runtime.standalone.panel_skills import SkillsRead
    native = Mock(return_value={"capability": CAPABILITY, "skills": [], "truncated": False})
    auth = SimpleNamespace(home=tmp_path.resolve(), gateway_owned=True, endpoint=Mock(), authenticated_request=native)
    reader = SkillsRead(tmp_path, auth)
    for method, params in [
        ("skills.manage", {"action": "install"}), ("display.start", {}),
        ("skills.library.list", {"profile": "other"}), ("skills.library.list", {"url": "https://invalid.invalid"}),
        ("skills.library.read", {"name": "../other"}), ("skills.library.read", {"name": "~/key"}),
        ("skills.library.read", {"name": "C:\\private"}), ("skills.library.read", {"name": "own", "path": "file"}),
    ]:
        with pytest.raises(SkillReadError):
            await reader.handle(method, params)
    native.assert_not_called()
    assert (await reader.handle("skills.library.list", {}))["capability"] == CAPABILITY
    native.assert_called_once_with("/api/skills/library/personal", method="GET")


@pytest.mark.asyncio
async def test_reverse_reader_rejects_unknown_native_response_fields_and_unready_gateway(tmp_path):
    from hermes_runtime.standalone.panel_skills import SkillsRead
    auth = SimpleNamespace(home=tmp_path.resolve(), gateway_owned=True,
        endpoint=Mock(side_effect=ValueError("fixture gateway unavailable")),
        authenticated_request=Mock(return_value={"capability": CAPABILITY, "skills": [], "truncated": False,
                                                "foreign_path": "must never be relayed"}))
    reader = SkillsRead(tmp_path, auth)
    assert reader.available() is False
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_UNAVAILABLE"):
        await reader.handle("skills.library.list", {})


@pytest.fixture
def system_source(tmp_path, monkeypatch):
    root = tmp_path / "installed" / "skills" / "agentsee"
    path = root / "common-example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: common-example\ndescription: Platform workflow.\n---\nLiteral ${PRIVATE_ENV} and !`do not execute`.\n")
    monkeypatch.setattr("agent.skill_utils.get_system_skills_dir", lambda: root)
    return root, path


def test_system_reads_are_source_owned_raw_bounded_and_keep_private_shadow(tmp_path, monkeypatch, system_source):
    root, path = system_source
    home = tmp_path / "own"
    private = personal(home, "common-example", body="PRIVATE_SHADOW")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(home / "skills"))
    monkeypatch.setenv("PRIVATE_ENV", "MUST_NOT_EXPAND")
    listed = system_inventory()
    assert listed["skills"][0]["origin"] == "system"
    assert listed["skills"][0]["readOnly"] is True
    assert not listed["skills"][0]["shadowed"]
    result = system_content("common-example")
    assert result["content"].encode("utf-8") == path.read_bytes()
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["bytes"] == len(path.read_bytes())
    assert "PRIVATE_SHADOW" not in json.dumps(result) and "MUST_NOT_EXPAND" not in json.dumps(result)
    assert str(root) not in json.dumps(result)
    assert personal_content(home, "common-example")["content"] == private.read_text()
    assert personal_inventory(home)["skills"][0]["shadowed"]


def test_system_detail_refuses_foreign_symlink_and_oversize(tmp_path, system_source):
    _root, path = system_source
    source = personal(tmp_path / "foreign", "common-example")
    path.unlink()
    path.symlink_to(source)
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESOURCE"):
        system_content("common-example")
    path.unlink()
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESOURCE_TOO_LARGE"):
        system_inventory()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [False, None, 1, "true"])
async def test_system_permission_is_trusted_exact_bool_not_request_data(tmp_path, permission):
    from hermes_runtime.standalone.panel_skills import SkillsRead
    auth = SimpleNamespace(home=tmp_path.resolve(), gateway_owned=True, authenticated_request=Mock())
    reader = SkillsRead(tmp_path, auth)
    with pytest.raises(SkillReadError, match="FORBIDDEN"):
        await reader.handle("skills.system.list", {}, can_read_system=permission)
    auth.authenticated_request.assert_not_called()


@pytest.mark.asyncio
async def test_closed_system_bridge_uses_fixed_routes_and_exact_source_schema(tmp_path, system_source):
    from hermes_runtime.standalone.panel_skills import SkillsRead
    _, path = system_source
    expected = {"/api/skills/library/system": system_inventory(),
                "/api/skills/library/system/content?name=common-example": system_content("common-example")}
    auth = SimpleNamespace(home=tmp_path.resolve(), gateway_owned=True,
                           authenticated_request=Mock(side_effect=lambda route, **kw: expected[route]))
    reader = SkillsRead(tmp_path, auth)
    assert await reader.handle("skills.system.list", {}, can_read_system=True) == expected["/api/skills/library/system"]
    result = await reader.handle("skills.system.read", {"name": "common-example"}, can_read_system=True)
    assert result["content"] == path.read_text() and result["skill"]["origin"] == "system"
    for method, params in [("skills.system.list", {"home": "/foreign"}),
                           ("skills.system.read", {"name": "common-example", "canReadSystemSkills": True}),
                           ("skills.system.read", {"name": "../outside"}),
                           ("skills.system.write", {"name": "common-example"})]:
        with pytest.raises(SkillReadError, match="INVALID_SKILLS_"):
            await reader.handle(method, params, can_read_system=True)
    assert auth.authenticated_request.call_count == 2
    auth.authenticated_request.return_value = {"capability": CAPABILITY, "skills": [], "truncated": False, "path": "/private"}
    auth.authenticated_request.side_effect = None
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_UNAVAILABLE"):
        await reader.handle("skills.system.list", {}, can_read_system=True)


@pytest.mark.asyncio
async def test_capability_probe_reads_only_system_even_when_personal_is_invalid(tmp_path, system_source):
    from hermes_runtime.standalone.panel_skills import SkillsRead
    calls = []

    def native(route, **kwargs):
        calls.append(route)
        if route == "/api/skills/library/system":
            return system_inventory()
        raise ValueError("Personal inventory refused")

    auth = SimpleNamespace(home=tmp_path.resolve(), gateway_owned=True, endpoint=lambda: None,
                           authenticated_request=native)
    reader = SkillsRead(tmp_path, auth)
    assert reader.available()
    assert calls == ["/api/skills/library/system"]
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_UNAVAILABLE"):
        await reader.handle("skills.library.list", {})
    assert calls == ["/api/skills/library/system", "/api/skills/library/personal"]


def test_system_http_detail_also_bounds_json_escaping(system_source):
    _, path = system_source
    path.write_text("---\nname: common-example\ndescription: Platform workflow.\n---\n" + "\x01" * (48 * 1024))
    assert len(path.read_bytes()) < 64 * 1024
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESPONSE_TOO_LARGE"):
        system_content("common-example")
