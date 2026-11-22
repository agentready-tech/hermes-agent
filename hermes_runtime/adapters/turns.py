"""Hermes execution ownership, IPC mailbox and group-boundary delivery."""

import fcntl
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from hermes_runtime.events import combine_event_content
from hermes_runtime.sessions import HOME_SESSION_ID
from hermes_runtime.adapters.hermes import managed_home
from hermes_runtime.adapters.state import belongs_to_home_lineage

_LOCK_DEPTH = threading.local()


def lock_path():
    return managed_home() / "state/agentready-home-session.lock"


def home_turn_active():
    """A live writer drains its own inbox; recovery must not queue extra model turns."""
    try:
        with lock_path().open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    return False


def steer_socket_path():
    from gateway.control_socket import _home_hash, _private_directory, _private_runtime_dir
    # A Docker Desktop bind mount can create sockets but rejects chmod on them.
    # Keep this ephemeral IPC endpoint on the OS runtime filesystem; the durable
    # ownership lock and accepted messages remain in the original home.
    base = _private_runtime_dir() or Path("/tmp")  # no-tmp: ok — AF_UNIX needs a local, short path outside shared home mounts
    directory = base / f"hermes-home-{os.getuid()}-{_home_hash(managed_home())}"
    if len(str(directory / "inbox.sock").encode()) > 100:
        directory = Path("/tmp") / directory.name  # no-tmp: ok — native AF_UNIX path length limit
    directory.mkdir(mode=0o700, exist_ok=True)
    if not _private_directory(directory):
        raise PermissionError(f"Managed IPC directory must be private and owned by this user: {directory}")
    return str(directory / "inbox.sock")


def _human_followup(content):
    """Only user chat envelopes can be shown as live user input or supersede a completed reply."""
    if isinstance(content, list):
        content = next((part.get("text") for part in content
                        if isinstance(part, dict) and part.get("type") == "text"), None)
    if not isinstance(content, str) or not content.startswith("[AgentReady event]\n"):
        return None
    try:
        header = json.loads(content.split("\n", 2)[1])
    except (IndexError, ValueError):
        return None
    if not isinstance(header, dict):
        return None
    source, event_id, sender = header.get("source"), header.get("event_id", ""), header.get("metadata")
    if source == "telegram" and isinstance(sender, dict) and sender.get("internal") is False:
        return header, content
    if source == "agentready" and str(event_id).startswith("panel:"):
        return header, content
    return None


def _preserve_completed_reply(agent, result, pending):
    from gateway.response_filters import is_intentional_silence_agent_result
    from hermes_runtime.adapters.goal_turn import goal_inbox_event

    text = result.get("final_response")
    if (not getattr(agent, "_mute_notification_reply", False)
            and isinstance(text, str) and text.strip() and not result.get("failed") and not result.get("interrupted")
            and not is_intentional_silence_agent_result(result, text)
            and any(_human_followup(content) or goal_inbox_event(content) for content in pending)):
        emit = getattr(agent, "_managed_completed_reply_callback", None)
        if emit is not None:
            emit(result)


def _present_human_followups(agent, pending, *, messages=None):
    events = [event for content in pending if (event := _human_followup(content))]
    if not events:
        return True
    resume = getattr(agent, "_resume_notification_presentation", None)
    if resume is not None:
        # Commit the diagnostic prefix while its visibility still applies. Only
        # the newly admitted input and subsequent work belong to the user-facing run.
        if messages is not None and agent._flush_messages_to_session_db(messages) is False:
            agent._incremental_persistence_failed = True
            return False
        resume()
    notify = getattr(agent, "_managed_input_display_callback", None)
    if notify is not None:
        for header, text in events:
            if header["source"] == "telegram":
                notify(header, text)
    return True


def forward_to_active_turn(content, *, wait=False, input_id=None):
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
                (json.dumps({"content": content, "wait": wait, "input_id": input_id}) + "\n").encode()
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
        from agent.memory_provider import spawn_context_thread
        from hermes_runtime.adapters.inbox import HomeInbox
        self.lock = threading.Lock()
        self.inbox = HomeInbox()
        self.inbox.acknowledge_recorded()
        self.pending = self.inbox.pending()
        self.wakeup = threading.Event()
        if self.pending:
            self.wakeup.set()
        self._known_inputs = {identity for identity, _ in self.pending}
        self._taken_inputs = set()
        self._browser_cleanups = {}
        self.accepting = True
        self.done = threading.Event()
        self.outcome = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        Path(steer_socket_path()).unlink(missing_ok=True)
        self.socket.bind(steer_socket_path())
        os.chmod(steer_socket_path(), 0o600)
        self.socket.listen()
        self.socket.settimeout(0.2)
        self.listener = spawn_context_thread(self.listen, name="managed-inbox-listener")
        self.listener.start()

    def listen(self):
        from agent.memory_provider import spawn_context_thread
        while not self.done.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            spawn_context_thread(lambda conn=connection: self.receive(conn), name="managed-inbox-receive").start()

    def receive(self, connection):
        try:
            with connection, connection.makefile("rwb") as stream:
                connection.settimeout(30)
                request = json.loads(stream.readline())
                content = request.get("content")
                with self.lock:
                    accepted = self.accepting and bool(content)
                    if accepted:
                        identity, pending = self.inbox.enqueue(content, input_id=request.get("input_id"))
                        if pending and identity not in self._known_inputs:
                            self._known_inputs.add(identity)
                            self.pending.append((identity, content))
                            self.wakeup.set()
                stream.write((json.dumps({"accepted": accepted}) + "\n").encode())
                stream.flush()
                if accepted and request.get("wait"):
                    self.done.wait()
                    stream.write((json.dumps(self.outcome) + "\n").encode())
                    stream.flush()
        except (OSError, ValueError):
            # A disconnected sender does not cancel an accepted event.
            return

    def take_records(self, *, close_if_empty=False, only_if=None):
        with self.lock:
            if only_if and not all(only_if(content) for _, content in self.pending):
                return []
            pending, self.pending = self.pending, []
            self.wakeup.clear()
            self._taken_inputs.update(identity for identity, _ in pending)
            if close_if_empty and not pending:
                self.accepting = False
            return pending

    def take(self, *, close_if_empty=False):
        return [content for _, content in self.take_records(close_if_empty=close_if_empty)]

    def defer_browser_cleanup(self, task_id, cleanup):
        # A model segment can finish while a queued clarification or tool
        # result is about to continue the same owned turn.
        self._browser_cleanups[task_id] = cleanup

    def finish_browser_cleanup(self, result):
        if not result.get("completed") or result.get("failed") or result.get("interrupted") or result.get("cleanup_errors"):
            return
        errors = []
        for cleanup in self._browser_cleanups.values():
            cleanup(errors)
        if errors:
            result["cleanup_errors"] = errors

    def finish(self, result=None, error=None):
        try:
            self.inbox.acknowledge_recorded(self._taken_inputs)
        except Exception:
            logging.getLogger(__name__).exception("Managed inbox acknowledgement failed; accepted inputs remain saved")
        with self.lock:
            self.accepting = False
            self.outcome = {"result": result, "error": error}
        self.done.set()
        self.socket.close()
        self.listener.join(timeout=1)
        Path(steer_socket_path()).unlink(missing_ok=True)


def run_conversation(self, proceed, *args, **kwargs):
    # Cache-sharing review/side-question forks retain the parent's session ID,
    # but deliberately own neither its transcript nor its incoming messages.
    if getattr(self, "_persist_disabled", False) or not belongs_to_home_lineage(getattr(self, "session_id", None)):
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
            from hermes_runtime.adapters.goal_turn import run_segment

            result = run_segment(self, turn, proceed, *args, **kwargs)
            while True:
                leftover = result.pop("pending_steer", None)
                records = turn.take_records(close_if_empty=not leftover)
                pending = [content for _, content in records]
                if leftover:
                    pending.insert(0, leftover)
                if not pending:
                    turn.finish_browser_cleanup(result)
                    break
                # If the model has just finished, a late user input still needs
                # its own response. Keep the completed reply visible before
                # continuing this same owned conversation.
                _preserve_completed_reply(self, result, pending)
                _present_human_followups(self, pending)
                # A final model response has no tool boundary. Continue
                # in this same owned session instead of losing the event.
                continuation = dict(kwargs)
                continuation["conversation_history"] = result.get("messages", [])
                continuation["task_id"] = self.session_id
                continuation.pop("user_message", None)
                continuation.pop("persist_user_message", None)
                continuation.pop("persist_user_timestamp", None)
                continuation["persist_user_platform_id"] = records[0][0] if records else None
                continuation["persist_user_display_kind"] = None
                continuation["persist_user_display_metadata"] = {"agentready_inbox_ids": [identity for identity, _ in records]}
                result = run_segment(self, turn, proceed, combine_event_content(pending),
                                     goal_records=records, goal_steer=leftover, **continuation)
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
    from hermes_runtime.adapters.goal_turn import ingest_records

    turn = getattr(agent, "_agentready_active_turn", None)
    records = ingest_records(agent, turn.take_records()) if turn is not None else []
    if not records:
        return
    contents = [content for _, content in records]
    if not _present_human_followups(agent, contents, messages=messages):
        return
    # Consecutive inputs share one model message. Persist every receipt on that
    # row so sequence repair cannot leave the later inputs pending for replay.
    messages.append({
        "role": "user", "content": combine_event_content(contents),
        "platform_message_id": records[0][0],
        "display_metadata": {"agentready_inbox_ids": [identity for identity, _ in records]},
    })


def steer(agent, content):
    if not content or (isinstance(content, str) and not content.strip()):
        return False
    return bool(forward_to_active_turn(content))
