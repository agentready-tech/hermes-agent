"""Backend SYSTEM, assigned OPTIONAL and OWN resolution in an isolated profile."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import skill_library as library
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

pytestmark = pytest.mark.platforms("posix")


def skill(name="required-example", kind="system", body="Shared procedure."):
    description = "Use the shared procedure."
    return {"name": name, "kind": kind, "description": description,
            "files": [{"path": "SKILL.md", "content":
                       f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"}]}


def packet(skills, revision=1):
    _payload, raw = library.canonical_snapshot(skills)
    return {"schema": 2, "revision": revision, "digest": hashlib.sha256(raw).hexdigest(), "skills": skills}


def publish(home, skills, selected=(), revision=1):
    value = packet(skills, revision)
    _payload, raw = library.canonical_snapshot(skills)
    root = library.custody_root(home)
    root.mkdir(parents=True, exist_ok=True)
    (root / "releases").mkdir(exist_ok=True)
    if not (root / ".read.lock").exists():
        (root / ".read.lock").write_bytes(b"")
        (root / ".read.lock").chmod(0o444)
    target = root / "releases" / value["digest"]
    if not target.exists():
        target.mkdir()
        (target / "release.json").write_bytes(raw)
        for row in skills:
            for item in row["files"]:
                path = target / row["kind"] / row["name"] / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["content"], encoding="utf-8", newline="")
        for current, _dirs, files in os.walk(target, topdown=False):
            for name in files:
                (Path(current) / name).chmod(0o444)
            Path(current).chmod(0o555)
    temporary = root / "next.json"
    temporary.write_text(json.dumps({"schema": 2, "revision": revision, "digest": value["digest"],
                                     "selectedOptional": list(selected)}))
    temporary.chmod(0o444)
    temporary.replace(root / "current.json")
    return value, target


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(library, "_CUSTODIAN_UID", os.getuid())
    token = set_hermes_home_override(home)
    from agent import skill_utils
    bundled = tmp_path / "bundled"
    own_skill(bundled, "image-bootstrap", "Image bootstrap.")
    monkeypatch.setattr(skill_utils, "get_system_skills_dir", lambda: bundled)
    (home / "config.yaml").write_text("skills:\n  disabled: [required-example]\nautonomous:\n  panel:\n    environment_id: env_" + "1" * 32 + "\n")
    try:
        yield home
    finally:
        reset_hermes_home_override(token)
        for current, dirs, files in os.walk(tmp_path):
            for name in dirs:
                if not (path := Path(current) / name).is_symlink():
                    path.chmod(0o755)
            for name in files:
                if not (path := Path(current) / name).is_symlink():
                    path.chmod(0o644)


def own_skill(root, name, body):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(skill(name, body=body)["files"][0]["content"])
    return path


def viewed(name):
    from tools.skills_tool import skill_view
    return json.loads(skill_view(name, preprocess=False))


def test_backend_system_set_replaces_bootstrap_including_empty_and_retirement(home):
    from agent.skill_utils import get_system_skill_names
    assert get_system_skill_names() == {"image-bootstrap"}
    publish(home, [skill()])
    assert get_system_skill_names() == {"required-example"}
    assert "Shared procedure." in viewed("required-example")["content"]
    publish(home, [], revision=2)
    assert get_system_skill_names() == set()
    assert "error" in viewed("image-bootstrap")
    assert "error" in viewed("required-example")


def test_system_cannot_be_shadowed_or_disabled_but_optional_only_when_selected(home):
    own_skill(home / "skills", "required-example", "OWN copy.")
    rows = [skill(), skill("optional-example", "optional", "Optional copy.")]
    publish(home, rows)
    assert "Shared procedure." in viewed("required-example")["content"]
    assert "error" in viewed("optional-example")
    publish(home, rows, ["optional-example"], revision=2)
    assert "Optional copy." in viewed("optional-example")["content"]
    own_skill(home / "skills", "optional-example", "OWN wins.")
    assert "OWN wins." in viewed("optional-example")["content"]
    publish(home, rows, revision=3)
    assert "OWN wins." in viewed("optional-example")["content"]


def test_reclassification_does_not_resurrect_source_mandatory(home):
    from agent.skill_utils import get_system_skill_names
    publish(home, [skill("image-bootstrap", "optional")])
    assert get_system_skill_names() == set()
    assert "error" in viewed("image-bootstrap")
    publish(home, [skill("image-bootstrap", "optional")], ["image-bootstrap"], revision=2)
    assert "Shared procedure." in viewed("image-bootstrap")["content"]
    assert get_system_skill_names() == set()


@pytest.mark.parametrize("selected", [["missing"], ["required-example"], ["optional-example", "optional-example"], [1]])
def test_unknown_system_duplicate_and_nonstring_assignments_refused(selected):
    value = packet([skill(), skill("optional-example", "optional")])
    with pytest.raises(library.SkillLibraryError, match="INVALID_SKILLS_ASSIGNMENT"):
        library.validate_selection(value, selected)


def test_schema_kind_and_canonical_order_are_bound_to_digest():
    rows = [skill("z-optional", "optional"), skill("a-system")]
    first = packet(rows)
    assert first["digest"] == packet(list(reversed(rows)))["digest"]
    rows[0]["kind"] = "system"
    assert packet(rows)["digest"] != first["digest"]
    with pytest.raises(library.SkillLibraryError, match="DIGEST_MISMATCH"):
        library.validate_library(first)


def test_bom_crlf_and_literal_placeholders_preserved():
    row = skill(body="${HERMES_SESSION_ID} !`printf fixture`\r\n")
    row["files"][0]["content"] = "\ufeff" + row["files"][0]["content"]
    value, raw = library.validate_library(packet([row]))
    assert value["skills"][0]["files"][0]["content"] == row["files"][0]["content"]
    assert b"\\r\\n" in raw


def test_published_read_and_preload_never_expand_template_or_shell(home, monkeypatch):
    from agent.skill_preprocessing import preprocess_skill_content
    body = "${HERMES_SESSION_ID} !`printf fixture`"
    _value, target = publish(home, [skill(body=body)])
    import agent.skill_preprocessing as preprocessing
    monkeypatch.setattr(preprocessing, "run_inline_shell", lambda *_a, **_kw: pytest.fail("executed inline shell"))
    content = preprocess_skill_content(body, target / "system" / "required-example", "session-value",
                                       {"template_vars": True, "inline_shell": True})
    assert content == body


def test_own_settings_view_stays_own_only_and_system_reader_is_backend(home):
    from hermes_runtime.skill_inventory import personal_inventory, system_inventory, system_content
    own_skill(home / "skills", "own-example", "Personal procedure.")
    publish(home, [skill(), skill("optional-example", "optional")], ["optional-example"])
    assert [r["name"] for r in personal_inventory(home)["skills"]] == ["own-example"]
    assert [r["name"] for r in system_inventory()["skills"]] == ["required-example"]
    assert system_content("required-example")["skill"]["origin"] == "system"


def test_corrupt_current_never_falls_back_to_image_or_claims_empty(home):
    from agent.skill_utils import get_system_skill_names
    from hermes_runtime.skill_inventory import SkillReadError, system_inventory
    publish(home, [skill()])
    current = library.custody_root(home) / "current.json"
    current.chmod(0o644)
    current.write_text("{}")
    current.chmod(0o444)
    assert get_system_skill_names() == set()
    assert library.load_library(home).status == "invalid"
    with pytest.raises(SkillReadError):
        system_inventory()


def test_ancestor_custody_and_immutable_resource_are_required(home):
    _value, target = publish(home, [skill()])
    assert library.load_library(home).status == "published"
    home.chmod(0o777)
    assert library.load_library(home).reason == "INVALID_SKILLS_UNTRUSTED_CUSTODY"
    home.chmod(0o700)
    (target / "system" / "required-example" / "SKILL.md").chmod(0o644)
    assert library.load_library(home).reason == "INVALID_SKILLS_UNTRUSTED_CUSTODY"


def test_pin_survives_atomic_switch_and_next_scope_reads_new_assignment(home):
    rows = [skill(), skill("optional-example", "optional")]
    publish(home, rows)
    with library.library_scope(home) as pinned:
        publish(home, rows, ["optional-example"], revision=2)
        assert library.current_library(home) is pinned
        assert pinned.selected_optional == ()
    assert library.load_library(home).selected_optional == ("optional-example",)


def test_agent_cannot_edit_create_or_archive_managed_path(home):
    from tools.skill_manager_tool import skill_manage
    from agent.skill_utils import is_protected_system_skill_path
    _value, target = publish(home, [skill()])
    assert is_protected_system_skill_path(target / "system" / "new-skill")
    result = json.loads(skill_manage(action="edit", name="required-example", content=skill(body="Changed.")["files"][0]["content"]))
    assert "read-only" in result["error"]


def test_settings_validator_uses_panel_environment_without_mail_store(home, monkeypatch):
    from hermes_runtime.workspace.control import response
    from hermes_runtime.workspace.service import Workspace
    monkeypatch.setattr(Workspace, "__init__", lambda *_a, **_kw: pytest.fail("mail store initialized"))
    request = {"operation": "skills_validate", "environmentId": "env_" + "1" * 32,
               "library": packet([skill()]), "selectedOptional": []}
    assert response(home, request)["value"]["valid"] is True
    request["environmentId"] = "env_" + "2" * 32
    assert response(home, request) == {"ok": False, "error": "FORBIDDEN"}


def test_content_only_update_changes_live_prompt_delta_without_rewriting_stored_prompt(home):
    from agent.prompt_builder import build_skills_system_prompt
    from agent.skills_index_delta import stage_skills_index_note
    publish(home, [skill(body="Revision one.")])
    before = build_skills_system_prompt()
    assert "required-example" in before
    publish(home, [skill(body="Revision two.")], revision=2)
    after = build_skills_system_prompt()
    assert before != after
    agent = SimpleNamespace(valid_tool_names={"skills_list", "skill_view"}, platform="cli",
                            provider="custom", api_mode="chat_completions", session_id="fixture-session",
                            _session_db=SimpleNamespace(db_path=str(home / "state.db")))
    assert stage_skills_index_note(agent, before, []) is True
    assert "required-example" in agent._skills_index_note
    assert before != after and "Revision two." in viewed("required-example")["content"]


def test_explicit_prompt_home_cannot_reuse_launcher_projection(home, tmp_path):
    from agent.prompt_builder import build_skills_system_prompt
    publish(home, [skill("first-system")])
    other = tmp_path / "other-profile"
    other.mkdir(mode=0o700)
    publish(other, [skill("second-system")])
    prompt = build_skills_system_prompt(skills_dir_override=other / "skills")
    assert "second-system" in prompt and "first-system" not in prompt
    assert library.current_library().packages[0]["name"] == "first-system"


def test_shared_read_lease_prevents_writer_gc_until_reader_finishes(home):
    import fcntl
    publish(home, [skill()])
    with open(library.custody_root(home) / ".read.lock", "rb") as writer:
        with library.library_scope(home):
            with pytest.raises(BlockingIOError):
                fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(writer.fileno(), fcntl.LOCK_UN)


def test_google_main_pointer_follows_active_system_classification_only(home):
    from agent.skill_utils import get_system_skills_dir
    from hermes_runtime.standalone.instructions import (
        MAIN_DELEGATION_INSTRUCTIONS, MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS,
        install_main_delegation_instructions,
    )
    name = "agentsee-google-browser-login"
    own_skill(get_system_skills_dir(), name, "Bootstrap Google procedure.")

    def fresh_main():
        agent = SimpleNamespace(platform="cli", side_agent=False, ephemeral_system_prompt="Attachment hint preserved.")
        install_main_delegation_instructions(agent)
        assert MAIN_DELEGATION_INSTRUCTIONS in agent.ephemeral_system_prompt
        assert "Attachment hint preserved." in agent.ephemeral_system_prompt
        return agent.ephemeral_system_prompt

    bootstrap = fresh_main()
    assert MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS in bootstrap
    publish(home, [skill(name, "optional")], [name])
    assert MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS not in fresh_main()
    assert MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS in bootstrap
    publish(home, [skill(name)], revision=2)
    assert MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS in fresh_main()
    publish(home, [], revision=3)
    assert MAIN_GOOGLE_BROWSER_LOGIN_INSTRUCTIONS not in fresh_main()


def test_shared_golden_packet_is_exact_canonical_contract():
    path = Path(__file__).parent / "fixtures" / "managed_skills_schema2.json"
    golden = json.loads(path.read_text())
    value, raw = library.validate_library(golden["library"])
    assert raw.decode("utf-8") == golden["canonicalUtf8"]
    assert hashlib.sha256(raw).hexdigest() == golden["current"]["digest"]
    assert library.validate_selection(value, golden["selectedOptional"]) == ("shared-selected",)


def test_empty_managed_index_receives_first_publish_and_optional_assignment(home):
    from agent.prompt_builder import build_skills_system_prompt
    from agent.skills_index_delta import stage_skills_index_note
    publish(home, [])
    before = build_skills_system_prompt()
    assert "<available_skills>" in before
    agent = SimpleNamespace(valid_tool_names={"skills_list", "skill_view"}, platform="cli",
                            provider="custom", api_mode="chat_completions", session_id="fixture-session",
                            _session_db=SimpleNamespace(db_path=str(home / "state.db")))
    publish(home, [skill()], revision=2)
    assert stage_skills_index_note(agent, before, []) is True
    assert "required-example" in agent._skills_index_note
    publish(home, [skill("optional-example", "optional")], revision=3)
    empty = build_skills_system_prompt()
    assert "optional-example" not in empty
    publish(home, [skill("optional-example", "optional")], ["optional-example"], revision=4)
    assert stage_skills_index_note(agent, empty, []) is True
    assert "optional-example" in agent._skills_index_note
    assert stage_skills_index_note(SimpleNamespace(valid_tool_names=set()), "No skill baseline.", []) is False


def test_unchanged_seed_hidden_but_customized_and_unknown_copies_remain_own(home):
    from tools.skills_sync import _dir_hash
    from hermes_runtime.skill_inventory import personal_inventory
    seed = own_skill(home / "skills", "seeded-example", "Original bundled procedure.")
    own_skill(home / "skills", "own-example", "Created for the user.")
    origin = _dir_hash(seed.parent)
    (home / "skills" / ".bundled_manifest").write_text("seeded-example:" + origin + "\n")
    assert "Original bundled" in viewed("seeded-example")["content"]
    selected = skill("seeded-example", "optional", "Selected backend procedure.")
    publish(home, [selected], ["seeded-example"])
    assert "Selected backend" in viewed("seeded-example")["content"]
    assert [row["name"] for row in personal_inventory(home)["skills"]] == ["own-example"]
    seed.write_text(skill("seeded-example", body="User customized copy.")["files"][0]["content"])
    assert "User customized" in viewed("seeded-example")["content"]
    assert {row["name"] for row in personal_inventory(home)["skills"]} == {"own-example", "seeded-example"}
    assert (home / "skills" / ".bundled_manifest").read_text().strip().endswith(origin)


def test_unknown_legacy_manifest_is_not_proof_of_bundled_ownership(home):
    own_skill(home / "skills", "legacy-example", "Preserved unknown legacy copy.")
    (home / "skills" / ".bundled_manifest").write_text("legacy-example\n")
    publish(home, [])
    assert "Preserved unknown" in viewed("legacy-example")["content"]
