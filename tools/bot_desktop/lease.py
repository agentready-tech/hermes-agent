"""Who may drive a profile's Bot Desktop screen: the agent (default) or exactly one human viewer.

The lease is the single truth shared by the RFB bridge (drops human input from non-holders), the
``computer_use`` tool (refuses to act while a human holds control — the person may be typing a
credential, so even screenshots are refused; fail closed rather than trusting the agent to pause
itself) and the Desktop UI (Watch / Take over / Hand back).

Scope: the lease is a TOOL-LEVEL fence, not a property of the X server. ``computer_use`` and the browser
tools consult it; a process the agent starts by hand against the published ``DISPLAY``/``XAUTHORITY``
(the ``terminal`` tool, a script) is inside the documented same-user boundary and is not stopped by it
(bot-screen.md, "Threat model"; #110040).

Authority lives ON DISK, ``<HERMES_HOME>/bot-desktop/lease.json`` under an fcntl lock, because the
processes that must agree do not share memory: ``hermes serve`` (viewer bridge), the messaging
gateway, a CLI turn and isolated workers all drive the same display. Every read goes to the file;
the in-process Condition only wakes local waiters early. ``epoch`` increments on every transition so
an action admitted under one lease can tell that control changed underneath it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from hermes_constants import get_hermes_home, hermes_home_key, secure_parent_dir

try:
    import fcntl
except ImportError:  # Windows/macOS without fcntl: computer_use imports this module on every call, and no
    fcntl = None     # multi-process Bot Desktop exists there, so the cross-process lock degrades to a no-op.

logger = logging.getLogger(__name__)

AGENT = "agent"
HUMAN = "human"
RECONNECT_GRACE_S = 30.0
RENEW_INTERVAL_S = 10.0


class HumanHasControl(RuntimeError):
    """Raised by screen-driving tools while a human holds the lease."""


@dataclass
class Lease:
    holder: str = AGENT
    viewer_id: Optional[str] = None
    since: float = field(default_factory=time.time)
    reason: str = ""
    epoch: int = 0
    expires_at: Optional[float] = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def public_view(lease: Lease) -> dict[str, object]:
    """The lease as anything outside the gateway may see it (RPC results, the ``display.lease`` broadcast,
    the CLI): the holder's viewer id is a capability — whoever presents it co-drives or releases the lease —
    so it is replaced by a short hash the holder can match against its own id to know it is in control."""
    import hashlib
    d = lease.as_dict()
    d.pop("expires_at", None)  # Persistence/liveness detail; keep the established public RPC shape.
    d["viewer_id"] = None
    d["viewer_hash"] = hashlib.sha256(lease.viewer_id.encode()).hexdigest()[:12] if lease.viewer_id else None
    return d


_lock = threading.Condition()
_listeners: list[Callable[[str, Lease], None]] = []


def _path(profile_key: Optional[str]) -> Path:
    """``profile_key`` is the HERMES_HOME path of the profile whose lease is meant (the RFB bridge
    serves several profiles from one process); ``None`` means the current profile."""
    home = Path(profile_key) if profile_key else get_hermes_home()
    return home / "bot-desktop" / "lease.json"


def _read(path: Path) -> Lease:
    """No file = fresh profile, agent holds. A file that exists but cannot be parsed is a torn write
    or tampering: fail CLOSED (human holds) — an unreadable lease must never let the agent act on a
    screen a human may be using; the next successful write repairs it."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return Lease()
    except OSError:
        return Lease(holder=HUMAN, viewer_id="unreadable-lease", reason="lease file unreadable")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("holder") not in (AGENT, HUMAN):
        return Lease(holder=HUMAN, viewer_id="unreadable-lease", reason="lease file corrupt")
    try:
        return Lease(**{k: v for k, v in data.items() if k in Lease.__dataclass_fields__})
    except TypeError:
        return Lease(holder=HUMAN, viewer_id="unreadable-lease", reason="lease file corrupt")


def _private_dir(path: Path) -> None:
    """``bot-desktop/`` owner-only even when the lease is the first thing written there (a takeover can be
    recorded before start() ever ran, and the umask would otherwise leave it 0755)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)


def _open_private(path: str | bytes | os.PathLike, flags: int) -> int:
    """``open(..., opener=_open_private)``: the file is created 0600 regardless of the umask."""
    return os.open(path, flags, 0o600)


def _write(path: Path, lease: Lease) -> None:
    _private_dir(path)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", opener=_open_private) as fh:
        fh.write(json.dumps(lease.as_dict()))
    os.replace(tmp, path)


class _locked:
    """Cross-process critical section over the lease file (fcntl on a sibling lock file)."""

    def __init__(self, path: Path):
        self._lockfile = path.with_suffix(".lock")
        self._fh = None

    def __enter__(self):
        if fcntl is None:
            return self
        _private_dir(self._lockfile)
        self._fh = open(self._lockfile, "a+", encoding="utf-8", opener=_open_private)
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()


def _deadline(lease: Lease) -> Optional[float]:
    """Legacy valid records use their original acquisition time; corrupt state stays closed."""
    if (lease.holder != HUMAN or not isinstance(lease.viewer_id, str)
            or not lease.viewer_id or lease.viewer_id == "unreadable-lease"
            or type(lease.epoch) is not int or lease.epoch < 0
            or type(lease.since) not in (int, float) or not math.isfinite(lease.since)):
        return None
    value = lease.expires_at
    if value is None:
        value = lease.since + RECONNECT_GRACE_S
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def expire(profile_key: Optional[str] = None, *, viewer_id: Optional[str] = None,
           expected_epoch: Optional[int] = None) -> Lease:
    """Expire only a stale valid human grant, optionally fenced to one disconnected viewer."""
    def _m(lease: Lease) -> bool:
        if ((viewer_id is not None and lease.viewer_id != viewer_id)
                or (expected_epoch is not None and lease.epoch != expected_epoch)):
            return False
        deadline = _deadline(lease)
        if deadline is None or time.time() < deadline:
            return False
        lease.holder, lease.viewer_id, lease.since, lease.reason = AGENT, None, time.time(), ""
        lease.expires_at = None
        return True
    return _transition(profile_key, _m)


def get(profile_key: Optional[str] = None) -> Lease:
    current = _read(_path(profile_key))
    deadline = _deadline(current)
    # Reads never extend liveness. Recheck under the existing cross-process lock before expiry.
    return expire(profile_key) if deadline is not None and time.time() >= deadline else current


def renew(viewer_id: str, *, expected_epoch: int,
          profile_key: Optional[str] = None) -> bool:
    """Only the existing live RFB holder can extend its current grant, never revive an expired one."""
    path = _path(profile_key)
    with _locked(path):
        current = _read(path)
        deadline = _deadline(current)
        now = time.time()
        if (current.holder != HUMAN or current.viewer_id != viewer_id
                or current.epoch != expected_epoch or deadline is None or now >= deadline):
            return False
        current.expires_at = now + RECONNECT_GRACE_S
        _write(path, current)
    # A heartbeat is not a control transition: preserve epoch/since and avoid event spam.
    return True


def on_change(listener: Callable[[str, Lease], None]) -> Callable[[], None]:
    """Subscribe to lease transitions made IN THIS PROCESS (the gateway broadcasts them to Desktop
    clients). Transitions made by another process are observed by reading, not by callback."""
    with _lock:
        _listeners.append(listener)

    def _off() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)
    return _off


def _notify(key: str, lease: Lease) -> None:
    # Snapshot under the lock: a viewer disconnecting on another thread runs unsubscribe()
    # concurrently with this loop; removing an entry from the live list mid-iteration skips
    # the next listener, and a still-connected viewer misses the lease transition.
    with _lock:
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb(key, lease)
        except Exception:  # a broken subscriber must not wedge the handoff
            pass


def _transition(profile_key: Optional[str], mutate: Callable[[Lease], bool]) -> Lease:
    key, path = hermes_home_key(profile_key) if profile_key else hermes_home_key(), _path(profile_key)
    with _locked(path):
        lease = _read(path)
        if not mutate(lease):
            return lease
        lease.epoch += 1
        _write(path, lease)
    with _lock:
        _lock.notify_all()
    _notify(key, lease)
    return lease


def acquire(viewer_id: str, *, profile_key: Optional[str] = None, reason: str = "",
            expected_epoch: Optional[int] = None, unless_human: bool = False) -> Lease:
    """Human ``viewer_id`` takes control. Last writer wins: a second viewer evicts the first, and the
    RFB bridge closes the evicted socket so its UI drops to view-only."""
    def _m(lease: Lease) -> bool:
        if expected_epoch is not None and lease.epoch != expected_epoch:
            raise HumanHasControl("Screen control changed; observe it again before continuing")
        if unless_human and lease.holder == HUMAN and lease.viewer_id != viewer_id:
            raise HumanHasControl("Another person controls this screen")
        if (lease.holder == HUMAN and lease.viewer_id == viewer_id
                and (deadline := _deadline(lease)) is not None and time.time() < deadline):
            return False  # already theirs: no epoch bump, `since` and the reason on screen stay put
        # The agent's ask ("please log in to X") stays as the takeover reason: the human needs it
        # on screen WHILE they act, not only before they clicked Take over.
        lease.holder, lease.viewer_id, lease.since = HUMAN, viewer_id, time.time()
        lease.reason = reason or ""
        lease.expires_at = time.time() + RECONNECT_GRACE_S
        return True
    return _transition(profile_key, _m)


def release(viewer_id: Optional[str] = None, *, profile_key: Optional[str] = None,
            unless_human: bool = False, expected_epoch: Optional[int] = None) -> Lease:
    """Return control to the agent. With ``viewer_id`` only that holder may release (a stale viewer
    closing its window must not yank control from the one who took over after it). ``unless_human``
    makes a bare release a no-op while any human holds: the decision is taken under the file lock,
    so a takeover racing a check-then-release cannot be silently revoked. Callers read the returned
    lease's holder to learn whether anything happened."""
    def _m(lease: Lease) -> bool:
        if expected_epoch is not None and lease.epoch != expected_epoch:
            return False
        if unless_human and lease.holder == HUMAN:
            logger.info("bot-desktop lease: bare release ignored, a human holds")
            return False
        if viewer_id is not None and lease.holder == HUMAN and lease.viewer_id != viewer_id:
            # Ignored, not an error: the returned lease still shows the real holder. Logged so a
            # caller that never inspects the return value leaves a trace.
            logger.info("bot-desktop lease: release by %r ignored, another viewer holds", viewer_id)
            return False
        if lease.holder == AGENT:
            # Already the agent's. Bumping the epoch here would make a legitimately admitted in-flight
            # agent action (a double-clicked Hand back, a stray CLI stop) look overtaken and get voided.
            return False
        lease.holder, lease.viewer_id, lease.since, lease.reason = AGENT, None, time.time(), ""
        lease.expires_at = None
        return True
    return _transition(profile_key, _m)


def human_holds(profile_key: Optional[str] = None) -> bool:
    return get(profile_key).holder == HUMAN


def viewer_may_send_input(viewer_id: str, *, profile_key: Optional[str] = None) -> bool:
    lease = get(profile_key)
    return lease.holder == HUMAN and lease.viewer_id == viewer_id


def assert_agent_may_act(profile_key: Optional[str] = None) -> Lease:
    """The lease as of now, or ``HumanHasControl``. Callers keep the returned ``epoch`` and compare it
    with ``get().epoch`` after an admitted action: a change means a human took over mid-flight."""
    lease = get(profile_key)
    if lease.holder == HUMAN:
        raise HumanHasControl(
            "A human has taken over this desktop (they may be entering a credential). Screen actions and "
            "captures are refused until they hand control back. Tell the user what you need in your reply.")
    return lease


def _reset_for_tests() -> None:
    with _lock:
        _listeners.clear()
    p = get_hermes_home() / "bot-desktop" / "lease.json"
    for f in (p, p.with_suffix(".lock"), p.with_suffix(".json.tmp")):
        try:
            f.unlink()
        except OSError:
            pass
