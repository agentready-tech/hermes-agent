"""Exercise profile-bound Antigravity stores without Google credentials."""

from ctypes import sizeof
from importlib.util import find_spec
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
from types import SimpleNamespace
import warnings

import pytest

from hermes_runtime.standalone import agy_local_authority
from hermes_runtime.standalone.agy_local_authority import LocalAuthority
from hermes_runtime.standalone.agy_slot import request_frame
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND, verify_staged
from providers.agy_policy import AgyError

pytestmark = pytest.mark.platforms("posix")


def test_landlock_path_rule_loads_silently_with_kernel_layout():
    module = find_spec("hermes_runtime.standalone.agy_confine")
    with warnings.catch_warnings(action="error", category=DeprecationWarning):
        structure = runpy.run_path(module.origin)["PathBeneath"]
    assert sizeof(structure) == 12
    assert structure.parent_fd.offset == 8


@pytest.fixture
def provisioned(tmp_path, monkeypatch):
    storage = tmp_path / "store"
    storage.mkdir()
    monkeypatch.setattr(agy_local_authority, "LOCAL_STORE", storage)
    monkeypatch.setattr(agy_local_authority, "SLOT_UID", os.geteuid())
    if os.getegid() != os.geteuid():
        def fixture_owner(fd, fstat=os.fstat):
            info = fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid, st_gid=info.st_uid)
            return info

        monkeypatch.setattr(agy_local_authority.os, "fstat", fixture_owner)

    def profile(letter):
        home = tmp_path / "profiles" / ("env_" + letter * 32)
        home.mkdir(parents=True)
        parent = storage / home.name
        parent.mkdir(mode=0o700)
        for slot in ("a", "b"):
            (parent / slot).mkdir(mode=0o700)
        owner = LocalAuthority(home)

        def control(slot, action, generation):
            path = parent / slot / "generation"
            if action == "prepare":
                path.write_text(generation)
            else:
                assert path.read_text() == generation
                path.unlink()

        monkeypatch.setattr(owner, "control", control)
        return owner, parent

    return profile


def test_staged_cancel_cannot_change_the_active_or_foreign_profile(provisioned, monkeypatch):
    owner, _ = provisioned("a")
    other, _ = provisioned("b")
    home = owner.home
    monkeypatch.setattr(owner, "launch", lambda row, operation, argv: (row["slot"], operation, argv))

    first = owner.stage(home)
    assert owner.spawn_staged(home, first, "login", LOGIN_COMMAND) == ("a", "login", LOGIN_COMMAND)
    assert owner.active(home) is None and owner.promote(home, first)
    next_login = owner.stage(home)
    assert owner.spawn_staged(home, next_login, "login", LOGIN_COMMAND) == ("b", "login", LOGIN_COMMAND)
    with pytest.raises(AgyError):
        owner.spawn_staged(other.home, next_login, "login", LOGIN_COMMAND)
    assert not other.promote(other.home, next_login)
    owner.discard(home, next_login)
    assert owner.active(home) == first.generation
    assert not owner.promote(home, next_login)


def test_legacy_binding_survives_without_statefulset_and_cannot_cross_profiles(provisioned):
    owner, parent = provisioned("a")
    other, _ = provisioned("b")
    ready = other.stage(other.home)
    assert other.promote(other.home, ready)
    generation = "c" * 32
    (parent / "a" / "generation").write_text(generation)
    legacy = {"active": {"slot": "a", "generation": generation, "uid": "removed-statefulset",
                         "image": "fixture.invalid/runtime@sha256:" + "0" * 64}}
    with owner.locked(owner.home):
        owner.save(legacy)
    prior = owner.binding.read_bytes()
    assert owner.active(owner.home) == generation
    assert owner.binding.read_bytes() == prior

    with other.locked(other.home):
        other.save(legacy)
    assert other.active(other.home) is None
    with pytest.raises(AgyError, match="Connect"):
        other.spawn(other.home, generation, "inference", "fixture")
    assert owner.active(owner.home) == generation


def test_local_launch_passes_only_the_bound_profile_slot(provisioned, monkeypatch):
    owner, parent = provisioned("a")
    other, other_parent = provisioned("b")
    (parent / "a" / "owner").write_text(owner.home.name)
    (other_parent / "a" / "owner").write_text(other.home.name)
    seen = []

    def launch(command, *, pass_fds, **_kwargs):
        descriptor, = pass_fds
        assert command[-5:-2] == ["start", "launch", "a" * 32]
        with os.fdopen(os.open("owner", os.O_RDONLY, dir_fd=descriptor), "r") as stream:
            seen.append(stream.read())
        return SimpleNamespace()

    monkeypatch.setattr(agy_local_authority.subprocess, "Popen", launch)
    row = {"slot": "a", "generation": "a" * 32}
    child = owner.launch(row, "models", None)
    assert child.agy_request_frame == request_frame("acp", json.dumps({"type": "models"}))
    assert seen == [owner.home.name]


def test_staged_login_probe_uses_acp_default_model_without_tools(provisioned, monkeypatch):
    owner, _ = provisioned("a")
    home = owner.home
    stage = owner.stage(home)
    monkeypatch.setattr(agy_local_authority, "authority_for", lambda _: owner)

    def launch(row, operation, request):
        assert (row["generation"], operation, request) == (stage.generation, "probe", None)
        script = Path(__file__).parents[1] / "agent" / "agy_acp_fixture.py"
        child = subprocess.Popen([sys.executable, "-I", str(script), str(home), "probe"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        child.agy_request_frame = owner.payload(operation, request)
        return child

    monkeypatch.setattr(owner, "launch", launch)
    verify_staged(owner, home, stage, login_pid=1, cancelled=lambda: False)
    assert owner.active(home) is None
    assert owner.promote(home, stage)


def test_acp_catalog_uses_bound_store_and_refreshes_for_new_login(provisioned, monkeypatch):
    owner, _ = provisioned("a")
    home = owner.home
    first = owner.stage(home)
    assert owner.promote(home, first)
    monkeypatch.setattr(agy_local_authority, "authority_for", lambda home: owner)
    launches = []

    def launch(row, operation, payload):
        assert (operation, payload) == ("models", None)
        launches.append(row["generation"])
        script = Path(__file__).parents[1] / "agent" / "agy_acp_fixture.py"
        child = subprocess.Popen([sys.executable, "-I", str(script), str(home), "models"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        child.agy_request_frame = request_frame("acp", '{"type":"models"}')
        return child

    monkeypatch.setattr(owner, "launch", launch)
    catalog = (("gemini-4-neon", "Gemini 4 Neon"), ("gemini-3.8-flash", "Gemini 3.8 Flash"),
               ("gemini-3.8-flash-low", "Gemini 3.8 Flash · Low"))
    assert owner.models(home, first.generation) == owner.models(home, first.generation) == catalog
    with pytest.raises(AgyError):
        owner.models(home.parent / "foreign", first.generation)
    assert launches == [first.generation]
    second = owner.stage(home)
    assert owner.promote(home, second)
    with pytest.raises(AgyError, match="Connect"):
        owner.models(home, first.generation)
    assert owner.models(home, second.generation) == catalog
    assert launches == [first.generation, second.generation]
