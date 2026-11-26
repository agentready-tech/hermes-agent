"""Exercise the two provisioned credential mounts without Google credentials."""

from ctypes import sizeof
from importlib.util import find_spec
from pathlib import Path
import runpy
import subprocess
import sys
import warnings

import pytest

from hermes_runtime.standalone import agy_pod_authority
from hermes_runtime.standalone.agy_pod_authority import PodAuthority
from hermes_runtime.standalone.agy_pod_process import request_frame
from hermes_runtime.standalone.agy_runtime import LOGIN_COMMAND
from providers.agy_policy import AgyError

pytestmark = pytest.mark.platforms("posix")


def test_landlock_path_rule_loads_silently_with_kernel_layout():
    module = find_spec("hermes_runtime.standalone.agy_confine")
    with warnings.catch_warnings(action="error", category=DeprecationWarning):
        structure = runpy.run_path(module.origin)["PathBeneath"]
    assert sizeof(structure) == 12
    assert structure.parent_fd.offset == 8


def test_staged_cancel_cannot_change_the_active_or_foreign_profile(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / ("env_" + "a" * 32)
    home.mkdir(parents=True)
    image = "fixture.invalid/runtime@sha256:" + "0" * 64
    owner = PodAuthority(home, "agentsee-a-" + "a" * 32, "fixture", (("uid-a", image), ("uid-b", image)))
    events = []
    monkeypatch.setattr(owner, "control", lambda *args: events.append(args))
    monkeypatch.setattr(owner, "launch", lambda row, operation, argv: (row["slot"], operation, argv))

    first = owner.stage(home)
    assert owner.spawn_staged(home, first, "login", LOGIN_COMMAND) == ("a", "login", LOGIN_COMMAND)
    assert owner.active(home) is None and owner.promote(home, first)
    next_login = owner.stage(home)
    assert owner.spawn_staged(home, next_login, "login", LOGIN_COMMAND) == ("b", "login", LOGIN_COMMAND)
    with pytest.raises(AgyError):
        owner.spawn_staged(Path("/foreign"), next_login, "login", LOGIN_COMMAND)

    owner.discard(home, next_login)
    assert owner.active(home) == first.generation
    assert not owner.promote(home, next_login)
    assert events == [("a", "prepare", first.generation), ("b", "prepare", next_login.generation),
                      ("b", "clear", next_login.generation)]


def test_approved_image_update_keeps_the_same_profile_login(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / ("env_" + "a" * 32)
    home.mkdir(parents=True)
    first_image = "fixture.invalid/runtime@sha256:" + "0" * 64
    next_image = "fixture.invalid/runtime@sha256:" + "1" * 64
    namespace = "agentsee-a-" + "a" * 32
    before = PodAuthority(home, namespace, "fixture", (("uid-a", first_image), ("uid-b", first_image)))
    monkeypatch.setattr(before, "control", lambda *_: None)
    store = before.stage(home)
    assert before.promote(home, store)
    persisted = before.binding.read_bytes()

    updated = PodAuthority(home, namespace, "fixture", (("uid-a", next_image), ("uid-b", next_image)))
    monkeypatch.setattr(updated, "launch", lambda row, op, payload: (row["slot"], row["generation"], op, payload))
    assert updated.active(home) == before.active(home) == store.generation
    assert updated.spawn(home, store.generation, "inference", "fixture") == ("a", store.generation, "inference", "fixture")
    assert updated.binding.read_bytes() == persisted

    replaced = PodAuthority(home, namespace, "fixture", (("new-uid-a", next_image), ("uid-b", next_image)))
    assert replaced.active(home) is None
    with pytest.raises(AgyError, match="Connect"):
        replaced.spawn(home, store.generation, "inference", "fixture")
    with pytest.raises(AgyError):
        updated.spawn(home.parent / "foreign", store.generation, "inference", "fixture")


def test_acp_catalog_uses_bound_store_and_refreshes_for_new_login(tmp_path, monkeypatch):
    image = "fixture.invalid/runtime@sha256:" + "0" * 64
    owner = PodAuthority(tmp_path, "fixture", "fixture", (("uid-a", image), ("uid-b", image)), acp=True)
    generation = "first-generation"
    monkeypatch.setattr(owner, "active", lambda home: generation if home == tmp_path else None)
    monkeypatch.setattr(agy_pod_authority, "authority_for", lambda home: owner if home == tmp_path else None)
    launches = []

    def spawn(home, expected, operation, payload):
        assert (home, expected, operation, payload) == (tmp_path, generation, "models", None)
        launches.append(expected)
        script = Path(__file__).parents[1] / "agent" / "agy_acp_fixture.py"
        child = subprocess.Popen([sys.executable, "-I", str(script), str(tmp_path), "models"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
        child.agy_request_frame = request_frame("acp", '{"type":"models"}')
        return child

    monkeypatch.setattr(owner, "spawn", spawn)
    catalog = (("gemini-4-neon", "Gemini 4 Neon"), ("gemini-3.8-flash", "Gemini 3.8 Flash"),
               ("gemini-3.8-flash-low", "Gemini 3.8 Flash · Low"))
    assert owner.models(tmp_path, generation) == owner.models(tmp_path, generation) == catalog
    with pytest.raises(AgyError, match="Connect"):
        owner.models(tmp_path / "foreign", generation)
    assert launches == ["first-generation"]
    generation = "second-generation"
    with pytest.raises(AgyError, match="Connect"):
        owner.models(tmp_path, "first-generation")
    assert owner.models(tmp_path, generation) == catalog
    assert launches == ["first-generation", "second-generation"]
