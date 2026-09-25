"""Hermes execution ownership, IPC mailbox and group-boundary delivery."""

import fcntl
import json
import os
import socket
import threading
import time
from pathlib import Path
from agentready_runtime.events import combine_event_content
from agentready_runtime.sessions import HOME_SESSION_ID
from agentready_runtime.adapters.hermes import managed_home
from agentready_runtime.adapters.state import belongs_to_home_lineage

_LOCK_DEPTH = threading.local()


def lock_path():
    return managed_home() / "state/agentready-home-session.lock"


def steer_socket_path():
    return str(lock_path().with_suffix(".sock"))


def forward_to_active_turn(content, *, wait=False):
    """Return None only when no owner accepted the event. Never replay an
    accepted event after a disconnect: its effects may already have happened.
    The socket is private to this Agent's container and runtime UID.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        try:
            connection.connect(steer_socket_path())
        except (FileNotFoundError, ConnectionRefusedError):
            return None
        connection.settimeout(None)
        with connection.makefile("rwb") as stream:
            stream.write(
                (json.dumps({"content": content, "wait": wait}) + "\n").encode()
            )
            stream.flush()
            accepted = stream.readline()
            if not accepted:
                raise RuntimeError(
                    "Active Hermes disconnected before acknowledging the event; delivery is uncertain"
                )
            if not json.loads(accepted).get("accepted"):
                return None
            if not wait:
                return True
            response = stream.readline()
            if not response:
                raise RuntimeError(
                    "Active Hermes stopped after accepting the event; it will not be replayed automatically"
                )
            outcome = json.loads(response)
            if outcome.get("error"):
                raise RuntimeError(outcome["error"])
            return outcome["result"]


class ActiveHomeTurn:
    """One run owns the model; all other ingress processes feed its mailbox."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = []
        self.accepting = True
        self.done = threading.Event()
        self.outcome = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        Path(steer_socket_path()).unlink(missing_ok=True)
        self.socket.bind(steer_socket_path())
        os.chmod(steer_socket_path(), 0o600)
        self.socket.listen()
        self.socket.settimeout(0.2)
        self.listener = threading.Thread(target=self.listen, daemon=True)
        self.listener.start()

    def listen(self):
        while not self.done.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self.receive, args=(connection,), daemon=True
            ).start()

    def receive(self, connection):
        try:
            with connection, connection.makefile("rwb") as stream:
                connection.settimeout(30)
                request = json.loads(stream.readline())
                content = request.get("content")
                with self.lock:
                    accepted = self.accepting and bool(content)
                    if accepted:
                        self.pending.append(content)
                stream.write((json.dumps({"accepted": accepted}) + "\n").encode())
                stream.flush()
                if accepted and request.get("wait"):
                    self.done.wait()
                    stream.write((json.dumps(self.outcome) + "\n").encode())
                    stream.flush()
        except (OSError, ValueError):
            # A disconnected sender does not cancel an accepted event.
            return

    def take(self, *, close_if_empty=False):
        with self.lock:
            pending, self.pending = self.pending, []
            if close_if_empty and not pending:
                self.accepting = False
            return pending

    def finish(self, result=None, error=None):
        with self.lock:
            self.accepting = False
            self.outcome = {"result": result, "error": error}
        self.done.set()
        self.socket.close()
        self.listener.join(timeout=1)
        Path(steer_socket_path()).unlink(missing_ok=True)


def run_conversation(self, proceed, *args, **kwargs):
    if not belongs_to_home_lineage(getattr(self, "session_id", None)):
        return proceed(*args, **kwargs)
    depth = int(getattr(_LOCK_DEPTH, "value", 0))
    if depth:
        _LOCK_DEPTH.value = depth + 1
        try:
            return proceed(*args, **kwargs)
        finally:
            _LOCK_DEPTH.value = depth
    lock_path().parent.mkdir(parents=True, exist_ok=True)
    with lock_path().open("a+") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                content = args[0] if args else kwargs.get("user_message", "")
                joined = forward_to_active_turn(content, wait=True)
                if joined is not None:
                    self.session_id = joined.get("session_id", self.session_id)
                    # The owner already persisted the complete transcript.
                    # Gateway must refresh its JSONL view after joining.
                    self._last_compaction_in_place = True
                    return joined
                time.sleep(0.02)
        _LOCK_DEPTH.value = 1
        turn = None
        try:
            turn = ActiveHomeTurn()
            self._agentready_active_turn = turn
            # hermes serve and gateway run are separate processes with
            # separate in-memory histories. Re-read the continuation tip
            # only after acquiring the shared lock so neither process can
            # start from a stale snapshot and overwrite the other turn.
            from hermes_state import SessionDB

            db = SessionDB()
            try:
                current_session_id = getattr(self, "session_id", HOME_SESSION_ID)
                target_session_id = (
                    db.resolve_resume_session_id(current_session_id)
                    or current_session_id
                )
                self.session_id = target_session_id
                latest_history = db.get_messages_as_conversation(
                    target_session_id,
                    repair_alternation=True,
                )
                supplied_history = kwargs.get("conversation_history")
                if isinstance(supplied_history, list):
                    # Mutate in place: Gateway retains this list to compute
                    # its history_offset after the agent returns.
                    supplied_history[:] = latest_history
                else:
                    kwargs["conversation_history"] = latest_history
                kwargs["task_id"] = target_session_id
            finally:
                close = getattr(db, "close", None)
                if callable(close):
                    close()
            result = proceed(*args, **kwargs)
            while True:
                leftover = result.pop("pending_steer", None)
                pending = turn.take(close_if_empty=not leftover)
                if leftover:
                    pending.insert(0, leftover)
                if not pending:
                    break
                # A final model response has no tool boundary. Continue
                # in this same owned session instead of losing the event.
                continuation = dict(kwargs)
                continuation["conversation_history"] = result.get("messages", [])
                continuation["task_id"] = self.session_id
                continuation.pop("user_message", None)
                continuation.pop("persist_user_message", None)
                continuation.pop("persist_user_timestamp", None)
                result = proceed(combine_event_content(pending), **continuation)
            turn.finish(result=result)
            return result
        except BaseException as error:
            if turn is not None:
                turn.finish(error="Active Hermes failed: " + str(error))
            raise
        finally:
            self._agentready_active_turn = None
            _LOCK_DEPTH.value = 0
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def after_tool_group(agent, messages):
    turn = getattr(agent, "_agentready_active_turn", None)
    if turn is not None:
        for content in turn.take():
            messages.append({"role": "user", "content": content})


def steer(agent, content):
    if not content or (isinstance(content, str) and not content.strip()):
        return False
    return bool(forward_to_active_turn(content))
