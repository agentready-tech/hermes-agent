"""Read-pinned backend SYSTEM and assigned OPTIONAL skills in the owning profile."""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

try:
    import fcntl
except ImportError:
    fcntl = None

MAX_FILE_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SKILLS = 50
MAX_FILES = 16
_CUSTODIAN_UID = 0
RESOURCE_DIRECTORY = ".agentsee-library-resources"
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PIN = ContextVar("hermes_common_skills_release", default=None)


class SkillLibraryError(ValueError):
    def __init__(self, code, field=None):
        super().__init__(code)
        self.field = field


def custody_root(home=None):
    from hermes_constants import get_hermes_home
    return Path(home or get_hermes_home()).resolve() / ".agentsee" / "skills-library"


def resource_projection_root(home=None):
    """Root-owned resource mirror inside the already shared, read-only personal skills mount."""
    from hermes_constants import get_hermes_home
    return Path(home or get_hermes_home()).resolve() / "skills" / RESOURCE_DIRECTORY


def resource_path(value):
    if (not isinstance(value, str) or len(value) > 180 or "\\" in value
            or not value or len(value.split("/")) > 6 or value.startswith("/") or any(not _SEGMENT.fullmatch(p) for p in value.split("/"))
            or any(p in {".", ".."} for p in value.split("/"))):
        raise SkillLibraryError("INVALID_SKILLS_RESOURCE", "path")
    if not re.fullmatch(r"(?:SKILL\.md|(?:references|scripts|templates|assets)/[A-Za-z0-9][A-Za-z0-9._/-]*\.(?:md|txt|json|yaml|yml|py|js|ts|sh|bash|sql|csv))", value) or (value != "SKILL.md" and value.endswith("/SKILL.md")):
        raise SkillLibraryError("INVALID_SKILLS_RESOURCE", "path")
    return PurePosixPath(value)


def _utf8(text, field):
    if not isinstance(text, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise SkillLibraryError("INVALID_SKILLS_TEXT", field)
    try:
        return text.encode("utf-8")
    except UnicodeError:
        raise SkillLibraryError("INVALID_SKILLS_TEXT", field) from None


def validate_skill(name, content):
    """Use native authoring invariants, with strict scalar identity and no YAML aliases."""
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.tokens import AliasToken, AnchorToken
    from tools.skill_manager_tool import _FRONTMATTER_END_RE, _validate_frontmatter, _validate_name
    from agent.skill_utils import SKILL_PROMPT_DESC_LIMIT, parse_frontmatter

    if not isinstance(name, str) or _validate_name(name):
        raise SkillLibraryError("INVALID_SKILLS_NAME", "name")
    _utf8(content, f"{name}/SKILL.md")
    parsed_content = content.removeprefix("\ufeff")
    end = _FRONTMATTER_END_RE.search(parsed_content[3:]) if parsed_content.startswith("---") else None
    if end is None:
        raise SkillLibraryError("INVALID_SKILLS_FRONTMATTER", f"{name}/SKILL.md")
    try:
        front = parsed_content[3:end.start() + 3]
        if any(isinstance(t, (AnchorToken, AliasToken)) for t in YAML(typ="safe").scan(front)):
            raise SkillLibraryError("INVALID_SKILLS_YAML_ALIASES_FORBIDDEN", f"{name}/SKILL.md")
        error = _validate_frontmatter(content, new_skill=True)
        if error:
            code = "INVALID_SKILLS_DESCRIPTION_TOO_LONG" if error.startswith("Description") else "INVALID_SKILLS_FRONTMATTER"
            raise SkillLibraryError(code, f"{name}/SKILL.md")
        metadata, _ = parse_frontmatter(content)
    except SkillLibraryError:
        raise
    except (TypeError, ValueError, YAMLError):
        raise SkillLibraryError("INVALID_SKILLS_FRONTMATTER", f"{name}/SKILL.md") from None
    if metadata.get("name") != name or not isinstance(metadata.get("description"), str):
        raise SkillLibraryError("INVALID_SKILLS_FRONTMATTER", f"{name}/SKILL.md")
    if len(metadata["description"]) > SKILL_PROMPT_DESC_LIMIT:
        raise SkillLibraryError("INVALID_SKILLS_DESCRIPTION_TOO_LONG", f"{name}/SKILL.md")
    return metadata


def canonical_snapshot(skills):
    if not isinstance(skills, list) or len(skills) > MAX_SKILLS:
        raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT", "skills")
    result, seen, total = [], set(), 0
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != {"name", "description", "kind", "files"}:
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT", "skills")
        name, files = skill["name"], skill["files"]
        if not isinstance(skill["kind"], str) or skill["kind"] not in {"system", "optional"}:
            raise SkillLibraryError("INVALID_SKILLS_KIND", "kind")
        if not isinstance(name, str) or name in seen or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise SkillLibraryError("INVALID_SKILLS_NAME", "name")
        seen.add(name)
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT", name)
        paths, normalized, package = set(), [], 0
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
                raise SkillLibraryError("INVALID_SKILLS_RESOURCE", name)
            path = resource_path(entry["path"]).as_posix()
            if path.casefold() in paths:
                raise SkillLibraryError("INVALID_SKILLS_DUPLICATE_RESOURCE", name)
            paths.add(path.casefold())
            raw = _utf8(entry["content"], f"{name}/{path}")
            if len(raw) > MAX_FILE_BYTES:
                raise SkillLibraryError("INVALID_SKILLS_RESOURCE_TOO_LARGE", f"{name}/{path}")
            package += len(raw)
            total += len(raw)
            normalized.append({"path": path, "content": entry["content"]})
        if package > MAX_PACKAGE_BYTES or total > MAX_SNAPSHOT_BYTES:
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT_TOO_LARGE", name)
        main = next((f["content"] for f in normalized if f["path"] == "SKILL.md"), None)
        if main is None:
            raise SkillLibraryError("INVALID_SKILLS_MISSING_SKILL_MD", name)
        metadata = validate_skill(name, main)
        if (skill["description"] != metadata["description"] or not metadata["description"]
                or "\n" in metadata["description"] or "\r" in metadata["description"]):
            raise SkillLibraryError("INVALID_SKILLS_DESCRIPTION_MISMATCH", name)
        row = {"name": name, "kind": skill["kind"], "description": metadata["description"],
               "files": sorted(normalized, key=lambda f: f["path"])}
        if len(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_PACKAGE_BYTES:
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT_TOO_LARGE", name)
        result.append(row)
    payload = {"schema": 2, "skills": sorted(result, key=lambda s: s["name"])}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT_TOO_LARGE", "library")
    return payload, raw


def validate_library(library):
    if (not isinstance(library, dict) or set(library) != {"schema", "revision", "digest", "skills"}
            or type(library["schema"]) is not int or library["schema"] != 2
            or type(library["revision"]) is not int or not 1 <= library["revision"] <= 2**53 - 1
            or not isinstance(library["digest"], str) or not _DIGEST.fullmatch(library["digest"])):
        raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT", "library")
    payload, raw = canonical_snapshot(library["skills"])
    if hashlib.sha256(raw).hexdigest() != library["digest"]:
        raise SkillLibraryError("INVALID_SKILLS_DIGEST_MISMATCH", "digest")
    return {"revision": library["revision"], "digest": library["digest"], **payload}, raw


def _custodied(path, *, directory=False, parent=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or (not directory and info.st_nlink != 1) or info.st_uid != _CUSTODIAN_UID
            or info.st_mode & (0o022 if parent else 0o222)):
        raise SkillLibraryError("INVALID_SKILLS_UNTRUSTED_CUSTODY")
    return info


def _read(path, root, limit):
    relative = path.relative_to(root)
    _custodied(root, directory=True, parent=True)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        _custodied(current, directory=True, parent=current == root / "releases")
    _custodied(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != _CUSTODIAN_UID or info.st_mode & 0o222):
            raise SkillLibraryError("INVALID_SKILLS_UNTRUSTED_CUSTODY")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise SkillLibraryError("INVALID_SKILLS_RESOURCE_TOO_LARGE")
    return raw


@contextmanager
def _read_lease(root):
    if not root.exists() and not root.is_symlink():
        yield
        return
    if fcntl is None:
        raise SkillLibraryError("INVALID_SKILLS_LIBRARY_LOCK_UNAVAILABLE")
    path = root / ".read.lock"
    _custodied(root, directory=True, parent=True)
    info = _custodied(path)
    if info.st_nlink != 1:
        raise SkillLibraryError("INVALID_SKILLS_UNTRUSTED_CUSTODY")
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1
                or opened.st_uid != _CUSTODIAN_UID or opened.st_mode & 0o222):
            raise SkillLibraryError("INVALID_SKILLS_UNTRUSTED_CUSTODY")
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT")
        result[key] = value
    return result


def _verify_tree(release, skills):
    expected = {"release.json"} | {s["kind"] + "/" + s["name"] + "/" + f["path"] for s in skills for f in s["files"]}
    directories = {str(parent) for path in expected for parent in PurePosixPath(path).parents if str(parent) != "."}
    found = set()
    for current, dirs, files in os.walk(release, followlinks=False):
        for name in dirs:
            path = Path(current) / name
            if path.relative_to(release).as_posix() not in directories:
                raise SkillLibraryError("INVALID_SKILLS_UNDECLARED_RESOURCE")
            _custodied(path, directory=True)
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(release).as_posix()
            if relative not in expected:
                raise SkillLibraryError("INVALID_SKILLS_UNDECLARED_RESOURCE")
            _custodied(path)
            found.add(relative)
    if found != expected:
        raise SkillLibraryError("INVALID_SKILLS_MISSING_RESOURCE")


@dataclass(frozen=True)
class LibraryState:
    status: str
    revision: int | None = None
    digest: str | None = None
    root: Path | None = None
    packages: tuple = ()
    selected_optional: tuple = ()
    reason: str | None = None

    def public(self):
        return {"status": self.status, "revision": self.revision, "digest": self.digest,
                **({"reason": self.reason} if self.reason else {})}

    def system_roots(self):
        return [self.root / "system"] if self.root and any(s["kind"] == "system" for s in self.packages) else []

    def optional_roots(self):
        return [self.root / "optional" / name for name in self.selected_optional] if self.root else []


def validate_selection(library, selected):
    if (not isinstance(selected, list) or any(not isinstance(n, str) for n in selected)
            or selected != sorted(set(selected))):
        raise SkillLibraryError("INVALID_SKILLS_ASSIGNMENT", "selectedOptional")
    optional = {s["name"] for s in library["skills"] if s["kind"] == "optional"}
    if not set(selected).issubset(optional):
        raise SkillLibraryError("INVALID_SKILLS_ASSIGNMENT", "selectedOptional")
    return tuple(selected)


def _load_current(root):
    if not root.exists() and not root.is_symlink():
        return LibraryState("bootstrap")
    try:
        # A sandbox user must not be able to rename a root-custodied tree's writable ancestor.
        _custodied(root.parent.parent, directory=True, parent=True)
        _custodied(root.parent, directory=True, parent=True)
        current = json.loads(_read(root / "current.json", root, 16 * 1024), object_pairs_hook=_object_pairs)
        if (not isinstance(current, dict) or set(current) != {"schema", "revision", "digest", "selectedOptional"}
                or type(current.get("schema")) is not int or current["schema"] != 2):
            raise SkillLibraryError("INVALID_SKILLS_CURRENT")
        digest = current["digest"]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise SkillLibraryError("INVALID_SKILLS_CURRENT")
        release = root / "releases" / digest
        raw = _read(release / "release.json", root, MAX_SNAPSHOT_BYTES)
        snapshot = json.loads(raw, object_pairs_hook=_object_pairs)
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema", "skills"}
                or type(snapshot["schema"]) is not int or snapshot["schema"] != 2):
            raise SkillLibraryError("INVALID_SKILLS_SNAPSHOT")
        library, canonical = validate_library({"schema": 2, "revision": current["revision"],
                                              "digest": digest, "skills": snapshot["skills"]})
        selected = validate_selection(library, current["selectedOptional"])
        if raw != canonical:
            raise SkillLibraryError("INVALID_SKILLS_CURRENT")
        _verify_tree(release, library["skills"])
        for skill in library["skills"]:
            for entry in skill["files"]:
                path = release / skill["kind"] / skill["name"] / entry["path"]
                if _read(path, root, MAX_FILE_BYTES) != entry["content"].encode("utf-8"):
                    raise SkillLibraryError("INVALID_SKILLS_RESOURCE_HASH_MISMATCH")
        return LibraryState("published", current["revision"], digest, release,
                            tuple(library["skills"]), selected)
    except SkillLibraryError as error:
        return LibraryState("invalid", reason=str(error))
    except (ValueError, UnicodeError, TypeError, KeyError):
        return LibraryState("invalid", reason="INVALID_SKILLS_SNAPSHOT")
    except OSError:
        return LibraryState("unavailable", reason="INVALID_SKILLS_LIBRARY_UNAVAILABLE")


def is_library_path(path, home=None):
    """Custody namespace, including inactive releases: never native-agent writable or preprocessed."""
    try:
        target = Path(path)
        return any(target.is_relative_to(root) or target.resolve().is_relative_to(root)
                   for root in (custody_root(home), resource_projection_root(home)))
    except (OSError, ValueError, RuntimeError):
        return True


def package_metadata(path, state=None):
    """Public origin and content stamp for a currently exposed managed package, never a path."""
    state = state or current_library()
    if state.root is None:
        return None
    target = Path(path).resolve()
    for row in state.packages:
        root = state.root / row["kind"] / row["name"]
        if target.is_relative_to(root) and (row["kind"] == "system" or row["name"] in state.selected_optional):
            raw = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return {"name": row["name"], "origin": row["kind"], "sha256": hashlib.sha256(raw).hexdigest()}
    return None


def resource_directory(path, state=None):
    """Return the physical sandbox-visible directory for one currently owned managed package."""
    state = state or current_library()
    metadata = package_metadata(path, state)
    if metadata is None:
        return Path(path)
    packages = [row for row in state.packages if row["kind"] == "system" or row["name"] in state.selected_optional]
    raw = json.dumps({"schema": 2, "skills": packages}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    root = resource_projection_root()
    release = root / "releases" / digest
    try:
        _custodied(root.parent, directory=True, parent=True)
        marker = _read(root / "projection.json", root, 1024)
        if marker != b'{"purpose":"agentsee-managed-skill-resources","schema":1}':
            raise SkillLibraryError("INVALID_SKILLS_RESOURCE_PROJECTION")
        if _read(release / "release.json", root, MAX_SNAPSHOT_BYTES) != raw:
            raise SkillLibraryError("INVALID_SKILLS_RESOURCE_PROJECTION")
        _verify_tree(release, packages)
        for row in packages:
            for entry in row["files"]:
                if _read(release / row["kind"] / row["name"] / entry["path"], root, MAX_FILE_BYTES) != entry["content"].encode("utf-8"):
                    raise SkillLibraryError("INVALID_SKILLS_RESOURCE_PROJECTION")
    except OSError:
        raise SkillLibraryError("INVALID_SKILLS_RESOURCE_PROJECTION_UNAVAILABLE") from None
    return release / metadata["origin"] / metadata["name"]


def load_library(home=None):
    with library_scope(home) as state:
        return state


def current_library(home=None):
    root = custody_root(home)
    pinned = _PIN.get()
    return pinned[1] if pinned and pinned[0] == root else load_library(home)


@contextmanager
def library_scope(home=None):
    root = custody_root(home)
    if (pinned := _PIN.get()) and pinned[0] == root:
        yield pinned[1]
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(_read_lease(root))
            state = _load_current(root)
        except SkillLibraryError as error:
            state = LibraryState("invalid", reason=str(error))
        except OSError:
            state = LibraryState("unavailable", reason="INVALID_SKILLS_LIBRARY_UNAVAILABLE")
        token = _PIN.set((root, state))
        try:
            yield state
        finally:
            _PIN.reset(token)


def pin_library(function):
    @wraps(function)
    def pinned(*args, **kwargs):
        with library_scope():
            return function(*args, **kwargs)
    return pinned
