"""Google JSON operations on the selected workspace network."""

import http.client
from urllib.parse import quote, urlencode, urlsplit

if __name__ != "__main__":
    from hermes_runtime.workspace.sandbox_mail import MailError, MAX_MAIL, TIMEOUT, json, main, public_socket, re, ssl

APIS = {"gmail": "https://gmail.googleapis.com/gmail/v1/users/me/", "drive": "https://www.googleapis.com/drive/v3/",
        "sheets": "https://sheets.googleapis.com/v4/", "docs": "https://docs.googleapis.com/v1/",
        "slides": "https://slides.googleapis.com/v1/", "calendar": "https://www.googleapis.com/calendar/v3/"}
DISCOVERY = {"drive": "drive/v3", "sheets": "sheets/v4", "docs": "docs/v1", "slides": "slides/v1", "calendar": "calendar/v3"}
SCOPES = {"mailRead": "https://www.googleapis.com/auth/gmail.readonly", "mailSend": "https://www.googleapis.com/auth/gmail.send",
          "drive": "https://www.googleapis.com/auth/drive", "sheets": "https://www.googleapis.com/auth/spreadsheets",
          "docs": "https://www.googleapis.com/auth/documents", "slides": "https://www.googleapis.com/auth/presentations",
          "calendar": "https://www.googleapis.com/auth/calendar"}


class PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(public_socket(self.host, self.port), server_hostname=self.host)


def json_http(url, method="GET", body=None, *, token=None, form=False):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"oauth2.googleapis.com", "openidconnect.googleapis.com", "www.googleapis.com", "gmail.googleapis.com", "sheets.googleapis.com", "docs.googleapis.com", "slides.googleapis.com", "calendar.googleapis.com"} or parsed.port not in {None, 443} or parsed.username or parsed.password:
        raise MailError("INVALID_GOOGLE_OPERATION")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        body = urlencode(body).encode() if form else json.dumps(body).encode()
        if len(body) > 1024 * 1024:
            raise MailError("INVALID_GOOGLE_TOO_LARGE")
    client = PublicHTTPS(parsed.hostname, timeout=TIMEOUT, context=ssl.create_default_context())
    try:
        client.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body=body, headers=headers)
        response = client.getresponse()
        if response.status == 404:
            raise MailError("INVALID_GOOGLE_NOT_FOUND")
        if response.status in {401, 403}:
            raise MailError("INVALID_GOOGLE_RECONNECT_OR_PERMISSIONS")
        if not 200 <= response.status < 300:
            raise MailError("INVALID_GOOGLE_REQUEST_FAILED" if method == "GET" or form else "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN")
        data = response.read(MAX_MAIL + 1)
        if len(data) > MAX_MAIL:
            raise MailError("INVALID_GOOGLE_TOO_LARGE")
        return json.loads(data) if data else {}
    except MailError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise MailError("INVALID_GOOGLE_REQUEST_FAILED" if method == "GET" or form else "INVALID_GOOGLE_WRITE_OUTCOME_UNKNOWN") from None
    finally:
        client.close()


def access_token(request):
    app = request.get("google", {})
    secret = request.get("credential", {})
    if not all(isinstance(v, str) and v for v in (app.get("clientId"), app.get("clientSecret"), secret.get("refreshToken"))):
        raise MailError("INVALID_GOOGLE_RECONNECT_REQUIRED")
    tokens = json_http("https://oauth2.googleapis.com/token", "POST", {"client_id": app["clientId"], "client_secret": app["clientSecret"], "refresh_token": secret["refreshToken"], "grant_type": "refresh_token"}, form=True)
    if not isinstance(tokens.get("access_token"), str):
        raise MailError("INVALID_GOOGLE_RECONNECT_REQUIRED")
    return tokens["access_token"]


def google_operation(service, method, path):
    if service not in APIS or method not in {"GET", "POST", "PATCH", "PUT", "DELETE"} or not isinstance(path, str) or not 0 < len(path) <= 3000 or path.startswith("/") or re.search(r"[^a-zA-Z0-9_!$&'()*+,;=:@%/?.\[\]-]|%2f|%5c|(?:^|/)(?:\.|%2e){1,2}(?:/|$)", path, re.I):
        raise MailError("INVALID_GOOGLE_OPERATION")
    prefixes = {"drive": r"^(files|about|changes)([/?]|$)", "sheets": r"^spreadsheets([/:?]|$)", "docs": r"^documents([/:?]|$)", "slides": r"^presentations([/:?]|$)", "calendar": r"^(calendars|users/me/calendarList)([/?]|$)"}
    if service == "gmail":
        if not ((method == "GET" and re.match(r"^(messages|threads|labels|profile|history)([/?]|$)", path)) or (method == "POST" and path == "messages/send")):
            raise MailError("INVALID_GMAIL_OPERATION")
    elif not re.match(prefixes[service], path):
        raise MailError("INVALID_GOOGLE_OPERATION")
    return "mailRead" if service == "gmail" and method == "GET" else "mailSend" if service == "gmail" else service


def command(request):
    service, resources, method = request.get("service"), request.get("resource"), request.get("method")
    word = lambda value: isinstance(value, str) and re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{0,63}", value)
    if service not in DISCOVERY or not isinstance(resources, list) or not 1 <= len(resources) <= 5 or not all(word(part) for part in resources) or not word(method):
        raise MailError("INVALID_GOOGLE_OPERATION")
    document = json_http("https://www.googleapis.com/discovery/v1/apis/" + DISCOVERY[service] + "/rest")
    node = document
    for part in resources:
        node = node.get("resources", {}).get(part, {})
    definition = node.get("methods", {}).get(method)
    if not definition:
        raise MailError("INVALID_GOOGLE_OPERATION")
    return service, document, definition


def gws(request):
    service, document, definition = command(request)
    if request["operation"] == "schema":
        return {"method": definition, "schemas": document.get("schemas", {}), "service": service, "source": "google-discovery"}
    params, body = request.get("params", {}), request.get("body")
    if not isinstance(params, dict) or (body is not None and not isinstance(body, dict)) or any(re.fullmatch(r"access_token|oauth_token|key|rootUrl|servicePath", key, re.I) for key in params):
        raise MailError("INVALID_GOOGLE_OPERATION")
    if params.get("alt", "json") != "json" or (service == "drive" and request["resource"][0] == "files" and request["method"] in {"export", "download"}):
        raise MailError("INVALID_GOOGLE_OPERATION")
    path, query = definition["path"], {}
    known = {**document.get("parameters", {}), **definition.get("parameters", {})}
    if set(params) - set(known):
        raise MailError("INVALID_GOOGLE_PARAMETERS")
    for name, spec in known.items():
        value = params.get(name)
        if value is None:
            if spec.get("required"):
                raise MailError("INVALID_GOOGLE_PARAMETERS")
            continue
        if isinstance(value, (dict, list)) and not (isinstance(value, list) and spec.get("repeated")):
            raise MailError("INVALID_GOOGLE_PARAMETERS")
        if spec.get("location") == "path":
            path = path.replace("{" + name + "}", quote(str(value), safe="")).replace("{+" + name + "}", quote(str(value), safe="/"))
        else:
            query[name] = value if isinstance(value, list) else str(value).lower() if isinstance(value, bool) else value
    if "{" in path:
        raise MailError("INVALID_GOOGLE_PARAMETERS")
    url = document.get("rootUrl", "") + document.get("servicePath", "") + path
    if query:
        url += "?" + urlencode(query, doseq=True)
    if not url.startswith(APIS[service]):
        raise MailError("INVALID_GOOGLE_OPERATION")
    if service not in request.get("services", []):
        raise MailError("FORBIDDEN")
    return json_http(url, definition["httpMethod"], body, token=access_token(request))


def exchange(request):
    services = request.get("services", [])
    if not services or any(value not in SCOPES for value in services):
        raise MailError("INVALID_WORKSPACE_PERMISSIONS")
    redirect = urlsplit(str(request.get("redirectUri", "")))
    if redirect.scheme != "https" or redirect.path != "/app/api/workspace/google/callback" or redirect.query or redirect.fragment:
        raise MailError("INVALID_GOOGLE_AUTHORIZATION")
    app = request["google"]
    tokens = json_http("https://oauth2.googleapis.com/token", "POST", {"code": request["code"], "code_verifier": request["verifier"], "client_id": app["clientId"], "client_secret": app["clientSecret"], "redirect_uri": request["redirectUri"], "grant_type": "authorization_code"}, form=True)
    granted = set(str(tokens.get("scope", "")).split())
    if not isinstance(tokens.get("refresh_token"), str) or any(SCOPES[value] not in granted for value in services):
        raise MailError("INVALID_GOOGLE_PERMISSIONS_NOT_GRANTED")
    profile = json_http("https://openidconnect.googleapis.com/v1/userinfo", token=tokens["access_token"])
    if not isinstance(profile.get("email"), str) or profile.get("email_verified") is not True:
        raise MailError("INVALID_GOOGLE_IDENTITY")
    history = json_http(APIS["gmail"] + "profile", token=tokens["access_token"]).get("historyId") if "mailRead" in services else None
    return {"address": profile["email"].lower(), "credential": {"refreshToken": tokens["refresh_token"]}, "gmailHistoryId": str(history) if history else None}


def execute_google(request):
    operation = request.get("operation")
    if operation == "verify":
        access_token(request)
        return {"verified": True}
    if operation in {"schema", "gws"}:
        return gws(request)
    if operation == "exchange":
        return exchange(request)
    service, method, path = request.get("service"), request.get("method"), request.get("path")
    permission = google_operation(service, method, path)
    if permission not in request.get("services", []):
        raise MailError("FORBIDDEN")
    return json_http(APIS[service] + path, method, request.get("body"), token=access_token(request))


if __name__ == "__main__":
    execute = execute_google
    main()
