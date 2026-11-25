"""Fence Panel's selected route at native apply and request admission, not during inference."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path

from hermes_runtime.sessions import HOME_SESSION_ID
from providers.agy_policy import AgyError, MODEL, PROVIDER, is_agy


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def selection_config(config, generation):
    """Set while holding the profile config transaction; no account data or credentials."""
    return {"generation": generation, "route": _identity(config["model"])}


@dataclass(frozen=True)
class Selection:
    home: Path
    revision: str
    generation: str
    fingerprint: str
    provider: str
    model: str
    endpoint: str

    def require_saved(self):
        from hermes_cli.config import require_readable_config_before_write
        from hermes_runtime.standalone import agy_runtime

        saved = require_readable_config_before_write(self.home / "config.yaml")
        if read_selection(self.home, saved) != self:
            raise AgyError("unavailable")
        if self.provider == PROVIDER:
            authority = agy_runtime.admit(self.home, "inference")
            if authority.active(self.home) != self.generation:
                raise AgyError("auth_required")

    def _receipt(self):
        return {"owner": _identity(str(self.home)), "session": HOME_SESSION_ID, "revision": self.revision,
                "generation": self.generation, "route": self.fingerprint, "provider": self.provider, "model": self.model}

    def _receipt_path(self, *, create=False):
        parent = self.home
        for component in ("subscriptions", "agy"):
            parent = parent / component
            if parent.is_symlink():
                raise AgyError("unavailable")
            if create:
                parent.mkdir(mode=0o700, exist_ok=True)
        return parent / "native-applied.json"

    def was_applied(self):
        path = self._receipt_path()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2048:
            return False
        try:
            return json.loads(path.read_text()) == self._receipt()
        except (OSError, ValueError):
            return False

    def require_runtime(self, agent):
        """Called after the real native model swap/creation, before the next request."""
        from hermes_cli.route_identity import normalize_route_base_url
        from hermes_constants import get_hermes_home

        if (get_hermes_home() != self.home or agent.session_id != HOME_SESSION_ID
                or (agent.provider, agent.model) != (self.provider, self.model)):
            raise AgyError("unavailable")
        if self.endpoint and normalize_route_base_url(str(agent.base_url)) != normalize_route_base_url(self.endpoint):
            raise AgyError("unavailable")
        if self.provider == PROVIDER and (agent.client.home != self.home or agent.client.generation != self.generation):
            raise AgyError("auth_required")

    def attach(self, agent):
        """Fence this and the next request attempt; never publishes an applied receipt."""
        self.require_saved()
        agent._panel_selection = self
        if self.provider == PROVIDER:
            agent.client.selection = self

    def publish_applied(self, agent):
        """Request boundary: recheck the persisted intent and the route actually being requested."""
        from hermes_cli.config import config_write_lock
        from utils import atomic_json_write

        with config_write_lock(self.home / "config.yaml"):
            self.require_saved()
            self.require_runtime(agent)
            if self.provider != PROVIDER and not self.was_applied():
                atomic_json_write(self._receipt_path(create=True), self._receipt(), mode=0o600, fsync_dir=True)

    def clear_stamp(self):
        """A successful disconnect ends the transition: no fence and no applied receipt survives."""
        from hermes_cli.config import atomic_config_replace, config_write_lock, require_readable_config_before_write

        path = self.home / "config.yaml"
        with config_write_lock(path):
            config = require_readable_config_before_write(path)
            panel = (config.get("autonomous") or {}).get("panel") or {}
            panel.pop("agy_selection", None)
            atomic_config_replace(path, config)
            receipt = self._receipt_path()
            if receipt.is_file() and not receipt.is_symlink():
                receipt.unlink()


def read_selection(home, config):
    """Read only the exact intent that Panel committed, never infer a receipt from a revision."""
    panel = (config.get("autonomous") or {}).get("panel") or {}
    stamp = panel.get("agy_selection")
    if stamp is None:
        return None
    revision, model = panel.get("model_selection_revision"), config.get("model")
    if not isinstance(model, dict) or not isinstance(stamp, dict) or not isinstance(revision, str):
        return None
    generation = stamp.get("generation")
    provider, target = model.get("provider"), model.get("default")
    if (not re.fullmatch(r"[a-f0-9]{32}", revision) or not isinstance(generation, str) or not generation
            or stamp.get("route") != _identity(model) or not isinstance(provider, str) or not isinstance(target, str)):
        return None
    if is_agy(provider) and (provider != PROVIDER or target != MODEL):
        raise AgyError("unavailable")
    return Selection(home.resolve(), revision, generation, stamp['route'], provider, target, model.get('base_url', ''))


def standalone_selection(stamp, session_key, model, provider):
    from hermes_cli.config import require_readable_config_before_write
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    ticket = read_selection(home, require_readable_config_before_write(home / "config.yaml"))
    if (session_key != HOME_SESSION_ID or ticket is None or (ticket.model, ticket.provider) != (model, provider)
            or stamp != {"revision": ticket.revision, "generation": ticket.generation}):
        raise AgyError("unavailable")
    ticket.require_saved()
    return ticket


@contextmanager
def commit_selection(ticket, session_key, result):
    """Lock only the local mutation, never provider resolution or inference."""
    if ticket is None:
        yield
        return
    from hermes_cli.config import config_write_lock
    from hermes_cli.route_identity import normalize_route_base_url

    with config_write_lock(ticket.home / "config.yaml"):
        ticket.require_saved()
        if (session_key != HOME_SESSION_ID or (result.target_provider, result.new_model) != (ticket.provider, ticket.model)
                or (ticket.endpoint and normalize_route_base_url(result.base_url) != normalize_route_base_url(ticket.endpoint))):
            raise AgyError("unavailable")
        yield


def capture_native_selection(session_key, target):
    """Guard the original standalone home against a stale Panel selection at turn start."""
    if session_key != HOME_SESSION_ID:
        return None
    from hermes_cli.config import read_user_config_raw
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    config = read_user_config_raw(home / "config.yaml")
    ticket = read_selection(home, config)
    if ticket is None:
        # A stale edited AGY config may not turn a reserved selection into legacy routing.
        if is_agy(target[1]) and ((config.get("autonomous") or {}).get("panel") or {}).get("agy_selection") is not None:
            raise AgyError("unavailable")
        return None
    if (ticket.model, ticket.provider) != target:
        raise AgyError("unavailable")
    ticket.require_saved()
    return ticket
