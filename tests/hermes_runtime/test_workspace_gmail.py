"""Gmail tools and proactive history use the same local grants and durable identities."""

import base64

from hermes_runtime.workspace import control
from hermes_runtime.workspace.service import Workspace
from tests.hermes_runtime.test_workspace_native import ENV, ID, connection


def workspace(tmp_path, runner):
    value = Workspace(tmp_path, runner=runner)
    metadata = connection(provider="google", proactive=True, senderPolicy="all", services=["mailRead", "mailSend"], gmailHistoryId="41")
    metadata.pop("inbound"); metadata.pop("outbound")
    value.store.upsert(metadata, {"refreshToken": "synthetic-refresh"})
    value.store.settings({"clientId": "synthetic-client", "clientSecret": "synthetic-secret"})
    return value


def message():
    return {"id": "gmail-one", "threadId": "thread-one", "internalDate": "1793952000000", "labelIds": ["INBOX", "UNREAD"],
        "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": "Lead <lead@example.invalid>"}, {"name": "Subject", "value": "Synthetic"}],
            "parts": [{"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"Synthetic text").decode()}},
                {"mimeType": "application/pdf", "filename": "huge.pdf", "body": {"attachmentId": "payload-one", "size": 300_000_000}}]}}


def test_gmail_search_returns_headers_and_native_server_cursor_without_reading_bodies(tmp_path):
    calls = []
    def run(home, program, request):
        calls.append(request)
        if request["path"].startswith("messages?"):
            return {"messages": [{"id": "gmail-one"}], "nextPageToken": "older", "resultSizeEstimate": 123}
        return message()
    service = workspace(tmp_path, run)
    value = service.search({"connectionId": ID, "query": "after:2026/10/27", "cursor": "prior"})
    assert value["messages"][0]["subject"] == "Synthetic"
    assert value["nextCursor"] == "older" and value["hasMore"] is True
    assert "pageToken=prior" in calls[0]["path"]
    assert "format=metadata" in calls[1]["path"]
    assert "text" not in value["messages"][0] and all("attachments/" not in row["path"] for row in calls)


def test_gmail_read_returns_text_and_large_attachment_metadata_without_payload_fetch(tmp_path):
    calls = []
    service = workspace(tmp_path, lambda home, program, request: calls.append(request) or message())
    value = service.read({"connectionId": ID, "messageId": "gmail-one"})
    assert value["text"] == "Synthetic text"
    assert value["attachments"][0]["size"] == 300_000_000
    assert len(calls) == 1 and calls[0]["path"].endswith("?format=full")


def test_gmail_proactive_history_preserves_event_source_and_cursor_without_panel(tmp_path):
    calls = []
    def run(home, program, request):
        assert program == "sandbox_google"
        calls.append(request)
        if request["path"].startswith("history?"):
            return {"historyId": "43", "history": [{"messagesAdded": [{"message": {"id": "gmail-one"}}]}]}
        return message()
    service = workspace(tmp_path, run)
    value = service.events({ID: {"historyId": "42"}})["batches"][0]
    assert value["cursor"] == {"historyId": "43"}
    assert value["events"][0]["source"] == "gmail:" + ID
    assert value["events"][0]["id"] == "gmail-one"
    assert value["events"][0]["metadata"] == {"thread_id": "thread-one", "connection_id": ID}
    assert "startHistoryId=42" in calls[0]["path"]
    assert "synthetic-refresh" not in str(value) and "Synthetic text" in value["events"][0]["prompt"]


def test_existing_google_client_cannot_be_rotated_under_saved_grants(tmp_path):
    service = workspace(tmp_path, lambda *args: None)
    from hermes_runtime.workspace.store import WorkspaceError
    import pytest
    with pytest.raises(WorkspaceError, match="CLIENT_IN_USE"):
        service.store.settings({"clientId": "different", "clientSecret": "new"})
    assert service.store.read()["google"]["clientId"] == "synthetic-client"


def test_missing_legacy_sender_policy_stays_omitted_during_idempotent_import(tmp_path):
    (tmp_path / "config.yaml").write_text(f"autonomous:\n  workspace:\n    environment_id: {ENV}\n")
    service = Workspace(tmp_path)
    metadata = connection()
    metadata.pop("senderPolicy")
    result = control.dispatch(tmp_path, {"operation": "import", "environmentId": ENV, "connections": [{"connection": metadata, "credential": {"username": "synthetic", "password": "synthetic"}}]}, workspace=service)
    public = result["connections"][0]
    public.pop("credentialConfigured")
    assert public == metadata
