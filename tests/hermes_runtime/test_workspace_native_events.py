"""Mailbox collection preserves native durable admission and never fetches attachments."""

import base64

import pytest

from hermes_runtime.standalone.workspace import admit_batches, poll
from hermes_runtime.workspace import sandbox_mail as mail
from hermes_runtime.workspace.service import Workspace
from tests.hermes_runtime.test_workspace_mail_sections import SectionClient
from tests.hermes_runtime.test_workspace_native import ID, SECRET, connection


class IncomingSections(SectionClient):
    def uid(self, command, uid, query=None, *extra):
        if command.upper() == "FETCH" and str(query) == "(UID INTERNALDATE)":
            return "OK", [f'1 (UID {uid} INTERNALDATE "06-Nov-2026 08:00:00 +0000")'.encode()]
        return super().uid(command, uid, query, *extra)


def test_incoming_uses_selected_text_with_attachment_metadata_and_no_full_mime(monkeypatch):
    client = IncomingSections(uids=(42,))
    monkeypatch.setattr(mail, "imap_connect", lambda request: client)
    value = mail.execute({"operation": "incoming", "format": "text-v1", "since": "2026-10-01T00:00:00Z", "cursor": {}, "credential": SECRET, "services": ["mailRead"]})
    assert value["cursor"] == {"validity": "7", "uid": 42}
    message = value["messages"][0]
    assert message["format"] == "text-v1" and message["uid"] == 42
    assert len(message["attachments"]) == 4
    assert client.payload_sections == ["1.1"]
    assert b"fixture-only attachment" not in base64.b64decode(message["raw"])


def test_native_poll_uses_local_workspace_events_and_old_url_never_contacts_panel(monkeypatch, tmp_path):
    workspace = Workspace(tmp_path)
    workspace.store.upsert(connection(proactive=True, senderPolicy="all"), SECRET)
    requests = []
    def run(home, program, request):
        requests.append((program, request))
        return {"messages": [{"uid": 27, "format": "text-v1", "internalDate": "06-Nov-2026 08:00:00 +0000", "attachments": [],
            "raw": base64.b64encode(b"From: lead@example.invalid\r\nSubject: Synthetic\r\n\r\nNative body").decode()}], "cursor": {"validity": "7", "uid": 27}}
    workspace.runner = run
    monkeypatch.setattr("hermes_runtime.workspace.service.Workspace", lambda home: workspace)
    accepted = []
    monkeypatch.setattr("hermes_runtime.standalone.workspace.admit_mail", lambda *args: accepted.append(args))
    cursor = {}
    assert poll(tmp_path, {"url": "https://panel.invalid/workspace"}, cursor) == {}
    assert not requests
    result = poll(tmp_path, {"native": True}, cursor)
    assert result == {ID: {"validity": "7", "uid": 27}}
    assert requests[0][1]["format"] == "text-v1"
    assert accepted[0][1:3] == ("email:" + ID, "7:27")
    assert SECRET["password"] not in str(accepted)


def test_native_failed_admission_keeps_original_cursor_for_replay(tmp_path):
    cursor = {ID: {"validity": "7", "uid": 26}}
    batch = {"batches": [{"connectionId": ID, "cursor": {"validity": "7", "uid": 27}, "events": [{"id": "7:27", "source": "email:" + ID, "prompt": "Synthetic", "metadata": {}}]}]}
    with pytest.raises(OSError):
        admit_batches(tmp_path, batch, cursor, admit=lambda *args: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    assert cursor == {ID: {"validity": "7", "uid": 26}}


def test_native_worker_remains_supervised_and_quiet_until_cutover(monkeypatch, tmp_path):
    from hermes_runtime.standalone import workspace as ingress
    class Stop:
        def __init__(self):
            self.done, self.waits = False, []
        def is_set(self):
            return self.done
        def set(self):
            self.done = True
        def wait(self, seconds):
            self.waits.append(seconds)
            self.set()
    stopped = Stop()
    monkeypatch.setattr(ingress.threading, "Event", lambda: stopped)
    monkeypatch.setattr(ingress.signal, "signal", lambda *_: None)
    monkeypatch.setattr("hermes_runtime.standalone.config.activate_home", lambda *_: None)
    monkeypatch.setattr("hermes_runtime.standalone.config.settings", lambda *_: {"workspace": {"native": False, "url": "https://panel.invalid/legacy", "poll_seconds": 30}})
    monkeypatch.setattr(ingress, "poll", lambda *_: pytest.fail("Paused worker must perform no collection"))
    ingress.worker(tmp_path)
    assert stopped.waits == [30]
