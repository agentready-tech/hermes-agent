"""Google JSON command and OAuth contracts, without vendor or credentials."""

import pytest

from hermes_runtime.workspace import sandbox_google as google
from hermes_runtime.workspace.sandbox_mail import MailError


@pytest.mark.parametrize(("path", "method", "code"), [
    ("spreadsheets/fixture/values/A1:C3", "GET", "INVALID_GOOGLE_REQUEST_FAILED"),
    ("spreadsheets/fixture/values:batchGetByDataFilter", "POST", "INVALID_GOOGLE_REQUEST_FAILED"),
    ("spreadsheets/fixture/values:batchUpdateByDataFilter", "POST", "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN"),
])
def test_http_error_reports_status_without_provider_body(monkeypatch, path, method, code):
    class Response:
        status = 503

        def read(self, _size):
            pytest.fail("A failing Google response body must stay private")

    class Connection:
        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(google, "PublicHTTPS", lambda *_args, **_kwargs: Connection())
    with pytest.raises(MailError, match=f"^{code}$") as caught:
        google.json_http(google.APIS["sheets"] + path, method)
    assert caught.value.diagnostic == "api: HTTP 503"


def document():
    return {"rootUrl": "https://sheets.googleapis.com/", "servicePath": "v4/", "parameters": {"fields": {"location": "query"}},
        "resources": {"spreadsheets": {"resources": {"values": {"methods": {"update": {"httpMethod": "PUT",
            "path": "spreadsheets/{spreadsheetId}/values/{range}", "parameters": {"spreadsheetId": {"location": "path", "required": True}, "range": {"location": "path", "required": True}, "valueInputOption": {"location": "query"}}, "request": {"$ref": "ValueRange"}}}}}}},
        "schemas": {"ValueRange": {"type": "object"}}}


def test_schema_and_json_method_keep_resource_params_body_contract(monkeypatch):
    calls = []
    def http(url, method="GET", body=None, **kwargs):
        calls.append((url, method, body, kwargs))
        return document() if "/discovery/" in url else {"updatedCells": 2}
    monkeypatch.setattr(google, "json_http", http)
    monkeypatch.setattr(google, "access_token", lambda request: "synthetic-only")
    command = {"operation": "schema", "service": "sheets", "resource": ["spreadsheets", "values"], "method": "update"}
    schema = google.execute_google(command)
    assert schema["method"]["request"] == {"$ref": "ValueRange"}
    assert "ValueRange" in schema["schemas"]
    result = google.execute_google({**command, "operation": "gws", "services": ["sheets"], "params": {"spreadsheetId": "fixture", "range": "Sheet1!A1:B1", "valueInputOption": "RAW"}, "body": {"values": [[1, 2]]}})
    assert result == {"updatedCells": 2}
    assert calls[-1][0] == "https://sheets.googleapis.com/v4/spreadsheets/fixture/values/Sheet1%21A1%3AB1?valueInputOption=RAW"
    assert calls[-1][1:3] == ("PUT", {"values": [[1, 2]]})


@pytest.mark.parametrize("change", [{"params": {"rootUrl": "https://private.invalid/"}}, {"params": {"access_token": "forbidden"}}, {"resource": ["--help"]}, {"params": {"unknown": "x"}}])
def test_gws_rejects_endpoint_auth_or_shell_injection(monkeypatch, change):
    monkeypatch.setattr(google, "json_http", lambda *args, **kwargs: document())
    with pytest.raises(MailError):
        google.execute_google({"operation": "gws", "service": "sheets", "resource": ["spreadsheets", "values"], "method": "update", "services": ["sheets"], **change})


def test_oauth_exchange_stays_inside_sandbox_and_checks_scopes(monkeypatch):
    calls = []
    def http(url, method="GET", body=None, **kwargs):
        calls.append((url, method, body))
        if url.endswith("/token"):
            return {"refresh_token": "synthetic-refresh", "access_token": "synthetic-access", "scope": google.SCOPES["mailRead"]}
        if url.endswith("/userinfo"):
            return {"email": "fixture@example.invalid", "email_verified": True}
        return {"historyId": "42"}
    monkeypatch.setattr(google, "json_http", http)
    result = google.execute_google({"operation": "exchange", "services": ["mailRead"], "google": {"clientId": "fixture", "clientSecret": "synthetic"}, "code": "synthetic", "verifier": "synthetic", "redirectUri": "https://example.invalid/app/api/workspace/google/callback"})
    assert result["credential"] == {"refreshToken": "synthetic-refresh"} and result["gmailHistoryId"] == "42"
    assert calls[0][2]["grant_type"] == "authorization_code"
    assert len(calls) == 3
