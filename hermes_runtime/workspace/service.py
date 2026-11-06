"""The agent owns grants, native tool execution and proactive collection."""

import base64
import json
import re
from urllib.parse import quote, urlencode

from hermes_runtime.workspace.mail import gmail_headers, gmail_text, mime_message, parse_text_mime
from hermes_runtime.workspace.store import ConnectionStore, WorkspaceError, now
from hermes_runtime.workspace.transport import sandbox_request


class Workspace:
    def __init__(self, home, *, runner=None):
        self.home, self.store = home, ConnectionStore(home)
        self.runner = runner or sandbox_request

    def mail(self, record, operation, **args):
        connection = record["connection"]
        return self.runner(self.home, "sandbox_mail", {**args, "operation": operation, "services": connection["services"],
            "inbound": connection.get("inbound"), "outbound": connection.get("outbound"), "credential": record["credential"]})

    def google(self, record, service, method, path, body=None):
        return self.runner(self.home, "sandbox_google", {"operation": "request", "services": record["connection"]["services"],
            "credential": record["credential"], "google": self.store.read()["google"], "service": service, "method": method, "path": path, "body": body})

    def search(self, args):
        record = self.store.get(args.get("connectionId"), "mailRead")
        if record["connection"]["provider"] == "imap_smtp":
            return self.mail(record, "search", **{key: args[key] for key in ("query", "since", "before", "cursor", "limit") if key in args})
        query = str(args.get("query", ""))[:500]
        if args.get("since"):
            query += " after:" + str(args["since"])
        if args.get("before"):
            query += " before:" + str(args["before"])
        params = {"maxResults": min(50, max(1, int(args.get("limit", 50)))), "q": query}
        if args.get("cursor"):
            params["pageToken"] = str(args["cursor"])
        listing = self.google(record, "gmail", "GET", "messages?" + urlencode(params))
        messages = []
        for reference in listing.get("messages", []):
            message = self.google(record, "gmail", "GET", "messages/" + quote(reference["id"], safe="") + "?format=metadata&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Cc&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=Message-ID")
            messages.append(gmail_headers(message))
        return {"messages": messages, "nextCursor": listing.get("nextPageToken"), "hasMore": bool(listing.get("nextPageToken")), "totalMatches": listing.get("resultSizeEstimate")}

    def read(self, args):
        record = self.store.get(args.get("connectionId"), "mailRead")
        identity = str(args.get("messageId", ""))
        if record["connection"]["provider"] == "google":
            return gmail_text(self.google(record, "gmail", "GET", "messages/" + quote(identity, safe="") + "?format=full"))
        if not re.fullmatch(r"\d+:\d+", identity):
            raise WorkspaceError("INVALID_MESSAGE_ID")
        message = self.mail(record, "read", messageId=identity, format="text-v1")
        if message.get("format") != "text-v1":
            raise WorkspaceError("INVALID_MAIL_CONTENT")
        return parse_text_mime(message)

    def attachment(self, args):
        record = self.store.get(args.get("connectionId"), "mailRead")
        identity, attachment = str(args.get("messageId", "")), str(args.get("attachmentId", ""))
        if record["connection"]["provider"] == "google":
            value = self.google(record, "gmail", "GET", "messages/" + quote(identity, safe="") + "/attachments/" + quote(attachment, safe=""))
            data = value.get("data", "")
            if not isinstance(data, str) or len(data) > 14_000_000 or len(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))) > 10 * 1024 * 1024:
                raise WorkspaceError("INVALID_MAIL_TOO_LARGE")
            return {"data": data, "encoding": "base64url"}
        if not re.fullmatch(r"\d+:\d+", identity) or not re.fullmatch(r"\d+", attachment):
            raise WorkspaceError("INVALID_MESSAGE_ID")
        return self.mail(record, "read", messageId=identity, attachmentId=attachment, format="attachment-v1")

    def send(self, args):
        record = self.store.get(args.get("connectionId"), "mailSend")
        message, to = mime_message(record["connection"], args)
        raw = message.as_bytes()
        if record["connection"]["provider"] == "google":
            body = {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
            if args.get("threadId"):
                body["threadId"] = str(args["threadId"])
            return self.google(record, "gmail", "POST", "messages/send", body)
        result = self.mail(record, "send", **{"from": record["connection"]["address"], "to": to, "raw": base64.b64encode(raw).decode()})
        return {"messageId": str(message["Message-ID"]), "accepted": result.get("accepted") is True}

    def tool(self, name, args):
        handlers = {"workspace_connections": lambda: self.store.list(), "mail_search": lambda: self.search(args),
            "mail_read": lambda: self.read(args), "mail_attachment": lambda: self.attachment(args), "mail_send": lambda: self.send(args),
            "google_workspace": lambda: self.google(self.store.get(args.get("connectionId"), args.get("service")), args.get("service"), args.get("method"), args.get("path"), args.get("body")),
            "google_schema": lambda: self.runner(self.home, "sandbox_google", {**args, "operation": "schema"}),
            "google_gws": lambda: self.gws(args)}
        if name not in handlers:
            raise WorkspaceError("INVALID_WORKSPACE_TOOL")
        return handlers[name]()

    def gws(self, args):
        record = self.store.get(args.get("connectionId"), args.get("service"))
        if record["connection"]["provider"] != "google":
            raise WorkspaceError("FORBIDDEN")
        return self.runner(self.home, "sandbox_google", {**args, "operation": "gws", "google": self.store.read()["google"],
            "credential": record["credential"], "services": record["connection"]["services"]})

    def verify(self, identity):
        record = self.store.get(identity)
        if record["connection"]["provider"] == "imap_smtp":
            self.mail(record, "verify")
        else:
            # Userinfo is verified during OAuth. Token refresh checks existing grants without reading mailbox contents.
            self.runner(self.home, "sandbox_google", {"operation": "verify", "credential": record["credential"], "google": self.store.read()["google"]})
        return {"verified": True, "connection": self.store.update(identity, {"status": "connected", "verifiedAt": now(), "lastError": None})}

    def events(self, cursors):
        from hermes_runtime.workspace.events import incoming
        batches = []
        for record in self.store.read()["connections"].values():
            connection = record["connection"]
            if not connection.get("enabled", True) or not connection.get("proactive"):
                continue
            try:
                value = incoming(self, record, cursors.get(connection["id"], {}))
                batches.append({"connectionId": connection["id"], **value})
                update = {"lastCheckedAt": now(), "lastError": None}
                if connection.get("verifiedAt"):
                    update["status"] = "connected"
            except (WorkspaceError, ValueError, TypeError, KeyError):
                batches.append({"connectionId": connection["id"], "error": "Mailbox could not be read; cursor was not advanced."})
                update = {"lastCheckedAt": now(), "lastError": "INVALID_MAIL_COLLECTION_FAILED"}
                if connection.get("verifiedAt"):
                    update["status"] = "needs_attention"
            self.store.update(connection["id"], update)
        return {"batches": batches}
