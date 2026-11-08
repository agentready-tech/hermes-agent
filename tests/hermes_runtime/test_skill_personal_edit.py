"""OWN text edits: trusted authority, owning profile, atomic CAS and native validation."""

import asyncio
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from urllib.error import HTTPError

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_runtime.skill_edit import EDIT_CAPABILITY, personal_update
from hermes_runtime.skill_inventory import SkillReadError, personal_content
from hermes_runtime.standalone.panel_skills import SkillsRead

pytestmark = pytest.mark.platforms("posix")


def document(name="own-example", body="Own procedure.", description="Use the own workflow."):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "own-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    path = home / "skills" / "own-example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(document())
    path.chmod(0o640)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)
        for current, dirs, files in os.walk(home):
            for name in dirs:
                if not (path := Path(current) / name).is_symlink():
                    path.chmod(0o755)
            for name in files:
                if not (path := Path(current) / name).is_symlink():
                    path.chmod(0o644)


def request(home, content=None):
    value = personal_content(home, "own-example")
    return {"name": "own-example", "content": content or document(body="Updated own procedure."),
            "expectedSha256": value["sha256"]}


def test_update_preserves_mode_raw_bom_crlf_and_read_contract(home):
    raw = "\ufeff" + document(body="Literal ${HERMES_SESSION_ID} !`printf fixture`\r\n")
    path = home / "skills" / "own-example" / "SKILL.md"
    result = personal_update(home, request(home, raw))
    assert result["capability"] == EDIT_CAPABILITY
    assert result["content"] == raw and result["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert path.read_bytes() == raw.encode() and stat.S_IMODE(path.stat().st_mode) == 0o640
    assert result["bytes"] == len(raw.encode())
    assert personal_content(home, "own-example")["capability"] == "skills-library-read-v1"


def test_cas_rejects_stale_editor_without_overwriting_agent_change(home):
    pending = request(home)
    path = home / "skills" / "own-example" / "SKILL.md"
    path.write_text(document(body="Agent changed this first."))
    before = path.read_bytes()
    with pytest.raises(SkillReadError, match="CONFLICT_SKILL_CHANGED"):
        personal_update(home, pending)
    assert path.read_bytes() == before


def test_same_lock_window_detects_agent_update_before_commit(home, monkeypatch):
    from hermes_runtime import skill_edit
    from tools.skill_manager_tool import _skill_mutation_lock
    pending = request(home)
    path = home / "skills" / "own-example" / "SKILL.md"
    original = skill_edit._replace_own

    def agent_changed(path, owning_home, content, expected):
        with _skill_mutation_lock("own-example"):
            path.write_text(document(body="Serialized native agent edit."))
        return original(path, owning_home, content, expected)

    monkeypatch.setattr(skill_edit, "_replace_own", agent_changed)
    with pytest.raises(SkillReadError, match="CONFLICT_SKILL_CHANGED"):
        personal_update(home, pending)
    assert "Serialized native agent edit." in path.read_text()


@pytest.mark.parametrize("change,code", [
    ({"name": "../foreign"}, "INVALID_SKILLS_NAME"),
    ({"expectedSha256": "stale"}, "INVALID_SKILLS_EXPECTED_HASH"),
    ({"home": "/foreign"}, "INVALID_SKILLS_REQUEST"),
    ({"filePath": "scripts/tool.py"}, "INVALID_SKILLS_REQUEST"),
    ({"content": "\x00"}, "INVALID_SKILLS_TEXT"),
    ({"content": "x" * (65536 + 1)}, "INVALID_SKILLS_RESOURCE_TOO_LARGE"),
    ({"content": document("renamed-example")}, "INVALID_SKILLS_NAME_RENAME"),
    ({"content": "No native frontmatter."}, "INVALID_SKILLS_FRONTMATTER"),
])
def test_closed_request_and_authoring_refusals_do_not_change_file(home, change, code):
    path = home / "skills" / "own-example" / "SKILL.md"
    before = path.read_bytes()
    with pytest.raises(SkillReadError, match=code):
        personal_update(home, {**request(home), **change})
    assert path.read_bytes() == before


def test_legacy_long_description_remains_editable_without_central_60_rule(home):
    result = personal_update(home, request(home, document(description="A" * 100)))
    assert result["skill"]["description"] == "A" * 100


def test_prospective_escaped_response_limit_is_checked_before_write(home):
    path = home / "skills" / "own-example" / "SKILL.md"
    before = path.read_bytes()
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_RESPONSE_TOO_LARGE"):
        personal_update(home, request(home, document(body="\x01" * 50000)))
    assert path.read_bytes() == before


def test_ambiguous_own_name_refused(home):
    duplicate = home / "skills" / "other-dir" / "SKILL.md"
    duplicate.parent.mkdir()
    duplicate.write_text(document(body="Ambiguous duplicate."))
    value = {"name": "own-example", "content": document(), "expectedSha256": "0" * 64}
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_AMBIGUOUS_NAME"):
        personal_update(home, value)


def test_symlink_or_hardlink_cannot_redirect_edit(home, tmp_path):
    target = tmp_path / "foreign.md"
    target.write_text(document(body="Foreign unchanged."))
    path = home / "skills" / "own-example" / "SKILL.md"
    pending = request(home)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(SkillReadError):
        personal_update(home, pending)
    assert "Foreign unchanged." in target.read_text()
    path.unlink()
    os.link(target, path)
    with pytest.raises(SkillReadError):
        personal_update(home, pending)
    assert "Foreign unchanged." in target.read_text()


def test_truncated_inventory_cannot_be_used_as_write_authority(home, monkeypatch):
    from hermes_runtime import skill_edit
    monkeypatch.setattr(skill_edit, "_catalog", lambda *_a: ([], True))
    value = {"name": "own-example", "content": document(), "expectedSha256": "0" * 64}
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_INVENTORY_TRUNCATED"):
        personal_update(home, value)


class Auth:
    gateway_owned = True

    def __init__(self, home):
        self.home = home.resolve()
        self.calls = []

    def authenticated_request(self, path, payload=None, *, method=None):
        self.calls.append((path, payload, method))
        assert path == "/api/skills/library/personal/content" and method == "POST"
        return personal_update(self.home, payload)


def test_trusted_permission_required_and_request_flag_never_grants(home):
    auth = Auth(home)
    reader = SkillsRead(home, auth)
    for flag in (False, None, 1, "true"):
        with pytest.raises(SkillReadError, match="FORBIDDEN"):
            asyncio.run(reader.handle("skills.library.update", request(home), can_edit_personal=flag))
    assert auth.calls == []
    with pytest.raises(SkillReadError, match="INVALID_SKILLS_REQUEST"):
        asyncio.run(reader.handle("skills.library.update", {**request(home), "canEditPersonalSkills": True}, can_edit_personal=True))
    assert auth.calls == []


def test_update_uses_fixed_own_gateway_even_when_ambient_profile_is_foreign(home, tmp_path):
    foreign = tmp_path / "foreign-home"
    foreign.mkdir()
    path = foreign / "skills" / "own-example" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(document(body="Foreign remains untouched."))
    pending = request(home)
    token = set_hermes_home_override(foreign)
    try:
        auth = Auth(home)
        result = asyncio.run(SkillsRead(home, auth).handle("skills.library.update", pending, can_edit_personal=True))
        assert result["capability"] == EDIT_CAPABILITY and len(auth.calls) == 1
        assert "Foreign remains untouched." in path.read_text()
    finally:
        reset_hermes_home_override(token)


def test_system_catalog_error_does_not_make_own_capability_unavailable(home):
    class UnavailableSystem(Auth):
        def endpoint(self):
            return None

        def authenticated_request(self, path, payload=None, *, method=None):
            self.calls.append((path, payload, method))
            raise HTTPError("local", 400, "typed", {}, BytesIO(b'{"detail":"INVALID_SKILLS_LIBRARY_UNAVAILABLE"}'))

    auth = UnavailableSystem(home)
    assert SkillsRead(home, auth).available() is True
    assert auth.calls == [("/api/skills/library/system", None, "GET")]


def test_actual_fixed_http_route_requires_own_principal_and_rejects_selectors(home):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli.web_routers.skills import router
    app = FastAPI()
    app.include_router(router)
    grant = {"purpose": "native-http", "capabilities": frozenset({"http:owner"}), "profile_id": str(home)}

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.native_http_principal = grant
        token = set_hermes_home_override(home)
        try:
            return await call_next(request)
        finally:
            reset_hermes_home_override(token)

    with TestClient(app) as client:
        pending = request(home)
        result = client.post("/api/skills/library/personal/content", json=pending)
        assert result.status_code == 200 and result.json()["capability"] == EDIT_CAPABILITY
        stale = client.post("/api/skills/library/personal/content", json=pending)
        assert stale.status_code == 409 and stale.json()["detail"] == "CONFLICT_SKILL_CHANGED"
        denied = client.post("/api/skills/library/personal/content?profile=foreign", json=request(home))
        assert denied.status_code == 400
        oversized = client.post("/api/skills/library/personal/content", content=b"x" * (512 * 1024 + 1))
        assert oversized.status_code == 413
        grant["profile_id"] = str(home.parent / "foreign")
        denied = client.post("/api/skills/library/personal/content", json=pending)
        assert denied.status_code == 403


def test_managed_system_name_is_not_editable_through_own_shadow(home, monkeypatch):
    from agent import skill_library as library
    monkeypatch.setattr(library, "_CUSTODIAN_UID", os.getuid())
    row = {"name": "own-example", "description": "Use mandatory procedure.", "kind": "system",
           "files": [{"path": "SKILL.md", "content": document(description="Use mandatory procedure.")}]}
    _payload, raw = library.canonical_snapshot([row])
    sha = hashlib.sha256(raw).hexdigest()
    root = library.custody_root(home)
    target = root / "releases" / sha
    directory = target / "system" / "own-example"
    directory.mkdir(parents=True)
    (root / ".read.lock").write_bytes(b"")
    (root / ".read.lock").chmod(0o444)
    (target / "release.json").write_bytes(raw)
    (directory / "SKILL.md").write_text(row["files"][0]["content"])
    for current, _dirs, files in os.walk(target, topdown=False):
        for name in files:
            (Path(current) / name).chmod(0o444)
        Path(current).chmod(0o555)
    (root / "current.json").write_text(json.dumps({"schema": 2, "revision": 1, "digest": sha, "selectedOptional": []}))
    (root / "current.json").chmod(0o444)
    path = home / "skills" / "own-example" / "SKILL.md"
    before = path.read_bytes()
    value = {"name": "own-example", "content": document(body="Cannot replace mandatory."),
             "expectedSha256": hashlib.sha256(before).hexdigest()}
    with pytest.raises(SkillReadError, match="FORBIDDEN"):
        personal_update(home, value)
    assert path.read_bytes() == before


def test_unsupported_update_is_per_request_and_does_not_break_existing_reads(home, monkeypatch):
    from websockets.asyncio import client
    from hermes_runtime.skill_edit import UPDATE_METHOD

    class LegacyAuth(Auth):
        def authenticated_request(self, path, payload=None, *, method=None):
            if method == "POST":
                raise HTTPError("local", 405, "unsupported", {}, BytesIO(b'{}'))
            return personal_content(self.home, "own-example")

    frames = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": UPDATE_METHOD, "params": request(home)}),
              json.dumps({"jsonrpc": "2.0", "id": 2, "method": "skills.library.read", "params": {"name": "own-example"}})]
    replies = []

    class Remote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not frames:
                raise StopAsyncIteration
            return frames.pop(0)

        async def send(self, value):
            replies.append(json.loads(value))

    monkeypatch.setattr(client, "connect", lambda *_a, **_kw: Remote())
    asyncio.run(SkillsRead(home, LegacyAuth(home)).bridge("ws://fixture", "synthetic", can_edit_personal=True))
    assert replies[0]["id"] == 1 and replies[0]["error"]["message"] == "INVALID_SKILLS_UNAVAILABLE"
    assert replies[1]["id"] == 2 and replies[1]["result"]["capability"] == "skills-library-read-v1"


def test_server_open_permission_is_exact_bool_and_does_not_use_params(home):
    from hermes_runtime.standalone.panel import bridge
    calls = []

    class Channel:
        def available(self):
            return True

        async def bridge(self, _remote, _credential, **permissions):
            calls.append(permissions)

    config = {"url": "https://fixture.invalid", "environment_id": "env_" + "1" * 32}
    for flag in (True, 1, "true", False):
        command = {"kind": "skills", "id": "fixture", "canEditPersonalSkills": flag}
        asyncio.run(bridge(command, config, 0, "synthetic", None, skills=Channel()))
    assert [call["can_edit_personal"] for call in calls] == [True, False, False, False]


def test_legacy_directory_alias_joins_native_agent_lock(home, monkeypatch):
    import fcntl
    from hermes_runtime import skill_edit
    from tools.skill_manager_tool import _skill_lock_path
    (home / "skills" / "own-example").rename(home / "skills" / "directory-alias")
    original = skill_edit._replace_own
    observed = []

    def replace_while_both_aliases_are_locked(path, owning_home, content, expected):
        for alias in ("own-example", "directory-alias"):
            with _skill_lock_path(alias).open("rb") as independent_writer:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(independent_writer.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            observed.append(alias)
        return original(path, owning_home, content, expected)

    monkeypatch.setattr(skill_edit, "_replace_own", replace_while_both_aliases_are_locked)
    assert personal_update(home, request(home))["capability"] == EDIT_CAPABILITY
    assert observed == ["own-example", "directory-alias"]
