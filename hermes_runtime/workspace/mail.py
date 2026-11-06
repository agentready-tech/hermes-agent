"""Mail text and metadata formatting stays inside the owning Hermes Pod."""

import base64
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, make_msgid
from html.parser import HTMLParser

from hermes_runtime.workspace.store import WorkspaceError, address


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(value):
    parser = PlainHTML()
    parser.feed(value)
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()


def addresses(value):
    return [{"name": name, "address": value.lower()} for name, value in getaddresses([str(value or "")])]


def parse_text_mime(message):
    try:
        source = base64.b64decode(message["raw"], validate=True)
    except (ValueError, KeyError):
        raise WorkspaceError("INVALID_MAIL_CONTENT") from None
    if len(source) > 10 * 1024 * 1024:
        raise WorkspaceError("INVALID_MAIL_TOO_LARGE")
    parsed = BytesParser(policy=policy.default).parsebytes(source)
    texts, htmls = [], []
    for part in parsed.walk():
        if part.is_multipart() or part.get_filename() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() in {"text/plain", "text/html"}:
            value = part.get_content()
            (texts if part.get_content_type() == "text/plain" else htmls).append(value)
    text = "\n".join(texts) if texts else "\n".join(plain_html(value) for value in htmls)
    senders = addresses(parsed.get("from"))
    attachments = message.get("attachments", [])
    if not isinstance(attachments, list) or len(attachments) > 128 or any(not isinstance(item, dict) or set(item) != {"id", "name", "size", "contentType"} for item in attachments):
        raise WorkspaceError("INVALID_MAIL_CONTENT")
    return {"from": senders[0]["address"] if senders else "", "to": addresses(parsed.get("to")), "cc": addresses(parsed.get("cc")),
            "subject": str(parsed.get("subject", "")), "date": str(parsed.get("date", "")), "messageId": str(parsed.get("message-id", "")),
            "text": text[:100_000], "attachments": attachments}


def mime_message(connection, args):
    to = address(args.get("to"))
    subject, text = args.get("subject", ""), args.get("text", "")
    reply = args.get("inReplyTo")
    if not isinstance(subject, str) or len(subject) > 500 or re.search(r"[\r\n]", subject) or not isinstance(text, str) or len(text) > 100_000:
        raise WorkspaceError("INVALID_MAIL_CONTENT")
    if reply is not None and (not isinstance(reply, str) or len(reply) > 1000 or re.search(r"[\r\n]", reply)):
        raise WorkspaceError("INVALID_MAIL_CONTENT")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"], message["Subject"] = connection["address"], to, subject
    message["Message-ID"] = make_msgid()
    if reply:
        message["In-Reply-To"] = reply
    message.set_content(text)
    return message, to


def gmail_text(message):
    headers = {row["name"].lower(): row["value"] for row in message.get("payload", {}).get("headers", [])}
    texts, htmls, attachments = [], [], []
    def visit(part):
        body = part.get("body", {})
        if part.get("filename"):
            attachments.append({"id": body.get("attachmentId", ""), "name": part["filename"], "size": body.get("size", 0), "contentType": part.get("mimeType", "application/octet-stream")})
        elif part.get("mimeType") in {"text/plain", "text/html"} and body.get("data"):
            raw = base64.urlsafe_b64decode(body["data"] + "=" * (-len(body["data"]) % 4)).decode("utf-8", errors="replace")
            (texts if part["mimeType"] == "text/plain" else htmls).append(raw)
        for child in part.get("parts", []):
            visit(child)
    visit(message.get("payload", {}))
    senders = addresses(headers.get("from"))
    return {"id": message["id"], "threadId": message.get("threadId"), "from": senders[0]["address"] if senders else "",
            "to": addresses(headers.get("to")), "cc": addresses(headers.get("cc")), "subject": headers.get("subject", ""),
            "date": headers.get("date"), "messageId": headers.get("message-id"),
            "text": ("\n".join(texts) if texts else "\n".join(plain_html(value) for value in htmls))[:100_000], "attachments": attachments}


def gmail_headers(message):
    headers = {row["name"].lower(): row["value"] for row in message.get("payload", {}).get("headers", [])}
    labels = message.get("labelIds", [])
    return {"id": message["id"], "threadId": message.get("threadId"), "from": addresses(headers.get("from")),
            "to": addresses(headers.get("to")), "cc": addresses(headers.get("cc")), "subject": headers.get("subject", ""),
            "date": headers.get("date"), "messageId": headers.get("message-id"), "flags": labels, "read": "UNREAD" not in labels}
