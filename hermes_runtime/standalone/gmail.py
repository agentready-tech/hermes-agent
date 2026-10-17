"""Optional Gmail OAuth ingress; native IMAP/SMTP remains available in the gateway."""

import base64
import json
import logging
import os
import signal
import threading
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from hermes_runtime.standalone.events import enqueue

logger = logging.getLogger(__name__)


class Gmail:
    def __init__(self, credentials, *, api="https://gmail.googleapis.com/gmail/v1/users/me",
                 token_url="https://oauth2.googleapis.com/token"):
        self.credentials, self.api, self.token_url = credentials, api.rstrip("/"), token_url

    def token(self):
        body = urlencode({"client_id": self.credentials["GMAIL_CLIENT_ID"],
                          "client_secret": self.credentials["GMAIL_CLIENT_SECRET"],
                          "refresh_token": self.credentials["GMAIL_REFRESH_TOKEN"],
                          "grant_type": "refresh_token"}).encode()
        with urlopen(Request(self.token_url, data=body), timeout=30) as response:
            return json.load(response)["access_token"]

    def get(self, path, token, **params):
        url = self.api + "/" + path + ("?" + urlencode(params) if params else "")
        with urlopen(Request(url, headers={"Authorization": "Bearer " + token}), timeout=30) as response:
            return json.load(response)

    def poll(self, home, config):
        from hermes_runtime.adapters.inbox import HomeInbox
        allowed = {address.strip().lower() for address in config.get("allowed_senders", [])}
        if not allowed:
            raise ValueError("autonomous.gmail.allowed_senders must list permitted senders (or '*')")
        token = self.token()
        account = self.get("profile", token)["emailAddress"].lower()
        source = "gmail:" + account
        with HomeInbox().transaction() as conn:
            seen = {json.loads(row[0])[1] for row in conn.execute(
                "SELECT event_key FROM agentready_inbox WHERE json_extract(event_key,'$[0]')=?", (source,))}
        page, accepted = None, 0
        while True:
            params = {"q": config.get("query", "in:inbox"), "maxResults": 100}
            if page:
                params["pageToken"] = page
            listing = self.get("messages", token, **params)
            for reference in listing.get("messages", []):
                identity = reference["id"]
                if identity in seen:
                    continue
                message = self.get("messages/" + identity, token, format="full")
                headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
                sender = parseaddr(headers.get("from", ""))[1].lower()
                if sender not in allowed and "*" not in allowed:
                    continue
                body, attachments = self.content(message, token, Path(home))
                prompt = "\n".join([f"Email from: {headers.get('from', '')}", f"Subject: {headers.get('subject', '')}",
                                     f"Date: {headers.get('date', '')}", "", body])
                enqueue(prompt, source, identity, {"thread_id": message.get("threadId"),
                    "message_id": headers.get("message-id"), "attachments": attachments})
                seen.add(identity)
                accepted += 1
            page = listing.get("nextPageToken")
            if not page:
                return accepted

    def content(self, message, token, home):
        from utils import atomic_write_bytes
        nodes, texts, attachments = [message["payload"]], [], []
        total = 0
        while nodes:
            node = nodes.pop()
            nodes.extend(reversed(node.get("parts", [])))
            body = node.get("body", {})
            if body.get("size", 0) > 20 * 1024 * 1024:
                raise ValueError("Gmail part exceeds 20 MiB; message remains unacknowledged")
            raw = body.get("data")
            if body.get("attachmentId"):
                raw = self.get(f"messages/{message['id']}/attachments/{body['attachmentId']}", token).get("data")
            if not raw:
                continue
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            total += len(data)
            if total > 25 * 1024 * 1024:
                raise ValueError("Gmail message exceeds 25 MiB; message remains unacknowledged")
            if node.get("filename"):
                name = Path(node["filename"].replace("\\", "/")).name
                if name in (".", "..", ""):
                    name = "attachment"
                path = home / "workspace" / "mail" / message["id"] / f"{len(attachments)}-{name}"
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, data, mode=0o600)
                attachments.append(str(path))
            elif node.get("mimeType") in ("text/plain", "text/html"):
                texts.append((node["mimeType"], data.decode("utf-8", errors="replace")))
        plain = [text for kind, text in texts if kind == "text/plain"]
        return "\n".join(plain or [text for _, text in texts]), attachments


def worker(home):
    from hermes_runtime.standalone.config import activate_home, settings
    from hermes_cli.env_loader import load_hermes_dotenv
    activate_home(home)
    load_hermes_dotenv(hermes_home=home)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    while not stopped.is_set():
        config = settings(home).get("gmail", {})
        if not config.get("enabled"):
            return
        try:
            Gmail(os.environ).poll(home, config)
        except Exception as error:  # health: allow BLE001 -- OAuth exceptions may include credentials
            logger.warning("Gmail poll deferred (%s); unaccepted messages remain in Gmail", type(error).__name__)
        stopped.wait(max(5, float(config.get("poll_seconds", 60))))
