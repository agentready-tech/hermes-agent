"""Bounded IMAP/SMTP client, executed inside the employee sandbox, never on the worker.

Only the fixed program is passed on argv. Account credentials and message content
arrive on stdin and are neither persisted nor included in diagnostics.
"""
import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import decode_rfc2231
from urllib.parse import unquote_to_bytes
import secrets
import imaplib
import hashlib
import unicodedata
import ipaddress
import json
import os
import re
import resource
import signal
import smtplib
import socket
import ssl
import sys

MAX_MAIL = 10 * 1024 * 1024
MAX_INPUT = 2 * 1024 * 1024
TIMEOUT = 15


class MailError(Exception):
    pass


def endpoint(value):
    if not isinstance(value, dict):
        raise MailError("INVALID_MAIL_SERVER")
    host, port, security = value.get("host"), value.get("port"), value.get("security")
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)
            or type(port) is not int or not 1 <= port <= 65535 or security not in {"tls", "starttls"}):
        raise MailError("INVALID_MAIL_SERVER")
    return host, port, security


def public_socket(host, port):
    # Resolve here, inside the sandbox's network namespace, and connect to the
    # validated literal. TLS still checks the original DNS name (including SNI).
    addresses = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise MailError("INVALID_PUBLIC_MAIL_SERVER_REQUIRED")
    for row in addresses:
        try:
            return socket.create_connection(row[4], timeout=TIMEOUT)
        except OSError:
            continue
    raise TimeoutError()


class BoundedImapLiteral:
    def read(self, size):
        # RFC822.SIZE may be an Exchange MIME-conversion estimate. Bound the
        # actual literal before imaplib allocates it, independently of that hint.
        if size > getattr(self, "literal_limit", MAX_MAIL):
            with contextlib.suppress(OSError):
                self.shutdown()
            raise MailError("INVALID_MAIL_TOO_LARGE")
        return super().read(size)


class Imap(BoundedImapLiteral, imaplib.IMAP4):
    def _create_socket(self, timeout):
        return public_socket(self.host, self.port)


class ImapTLS(BoundedImapLiteral, imaplib.IMAP4_SSL):
    def _create_socket(self, timeout):
        return self.ssl_context.wrap_socket(public_socket(self.host, self.port), server_hostname=self.host)


class Smtp(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return public_socket(host, port)


class SmtpTLS(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        return self.context.wrap_socket(public_socket(host, port), server_hostname=host)


def error_code(protocol, phase, error):
    if isinstance(error, MailError):
        return str(error)
    prefix = "INVALID_MAIL_" + protocol.upper() + "_"
    if isinstance(error, ssl.SSLError) or phase == "tls":
        return prefix + "TLS_FAILED"
    if isinstance(error, (TimeoutError, socket.timeout, OSError)):
        return prefix + "UNREACHABLE"
    if isinstance(error, smtplib.SMTPAuthenticationError) or (isinstance(error, imaplib.IMAP4.error) and phase == "auth"):
        return prefix + "AUTH_FAILED"
    return prefix + "PROTOCOL_FAILED"


def folder_name(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("INVALID_MAIL_FOLDER")
    try:
        if len(value.encode("utf8")) > 1024:
            raise MailError("INVALID_MAIL_FOLDER")
    except UnicodeError:
        raise MailError("INVALID_MAIL_FOLDER") from None
    return "INBOX" if value.upper() == "INBOX" else value


def mailbox_utf7(value):
    """IMAP4rev1 uses modified UTF-7, independently of SEARCH's charset."""
    def encoded(match):
        return "&" + base64.b64encode(match[0].encode("utf-16-be")).decode().rstrip("=").replace("/", ",") + "-"
    return re.sub(r"[^ -~]+", encoded, folder_name(value).replace("&", "&-")).encode("ascii")


def mailbox_unicode(value):
    try:
        source = value.decode("ascii")
        def decoded(match):
            token = match[1].replace(",", "/")
            return base64.b64decode(token + "=" * (-len(token) % 4), validate=True).decode("utf-16-be") if token else "&"
        if "&" in re.sub(r"&[A-Za-z0-9+,]*-", "", source):
            raise ValueError()
        return folder_name(re.sub(r"&([A-Za-z0-9+,]*)-", decoded, source))
    except (ValueError, UnicodeError):
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED") from None


def list_mailbox(row):
    literal = None
    if isinstance(row, tuple) and len(row) == 2:
        row, literal = row
    if not isinstance(row, bytes) or len(row) > 8192:
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    parsed = re.fullmatch(rb'\(([^)]*)\)\s+(NIL|"(?:[^"\\]|\\.)*")\s+(.+)', row)
    if not parsed:
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    flags, delimiter, name = parsed.groups()
    if literal is not None:
        if not isinstance(literal, bytes) or name != b"{" + str(len(literal)).encode() + b"}":
            raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
        name = literal
    elif name.startswith(b'"'):
        if not re.fullmatch(rb'"(?:[^"\\]|\\.)*"', name):
            raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
        name = re.sub(rb"\\(.)", rb"\1", name[1:-1])
    separator = "" if delimiter == b"NIL" else mailbox_unicode(re.sub(rb"\\(.)", rb"\1", delimiter[1:-1]))
    if delimiter != b"NIL" and len(separator) != 1:
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    return {"name": mailbox_unicode(name), "flags": flags.lower().split(), "delimiter": separator}


def resolve_folder(client, requested):
    requested = folder_name(requested)
    if requested == "INBOX":
        return requested
    status, values = client.list('""', '*')
    if status != "OK" or not isinstance(values, list) or len(values) > 10000:
        raise MailError("INVALID_MAIL_FOLDER_LIST_FAILED")
    # imaplib follows a LIST literal tuple with its empty framing trailer.
    folders = [list_mailbox(row) for row in values if row not in (None, b"")]
    folders = [row for row in folders if b"\\noselect" not in row["flags"]]
    aliases = {"sent": (b"\\sent", {"sent", "sent items", "sent messages"}),
               "drafts": (b"\\drafts", {"drafts", "draft"}), "archive": (b"\\archive", {"archive", "archives"})}
    alias = aliases.get(requested.lower())
    if alias:
        candidates = {row["name"] for row in folders if alias[0] in row["flags"]}
        if not candidates:
            candidates = {row["name"] for row in folders
                          if (row["name"].rsplit(row["delimiter"], 1)[-1] if row["delimiter"] else row["name"]).lower() in alias[1]}
    else:
        candidates = {row["name"] for row in folders if row["name"] == requested}
    if len(candidates) != 1:
        raise MailError("INVALID_MAIL_FOLDER_AMBIGUOUS" if candidates else "INVALID_MAIL_FOLDER_NOT_FOUND")
    return candidates.pop()


def imap_identity(value):
    if not isinstance(value, str):
        raise MailError("INVALID_MESSAGE_ID")
    if re.fullmatch(r"\d{1,10}:\d{1,10}", value):
        current, uid = value.split(":")
        folder = "INBOX"
    else:
        match = re.fullmatch(r"imap-v1:([A-Za-z0-9_-]{1,1400}):(\d{1,10}):(\d{1,10})", value)
        if not match:
            raise MailError("INVALID_MESSAGE_ID")
        token, current, uid = match.groups()
        try:
            folder = folder_name(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True).decode("utf8"))
        except (ValueError, UnicodeError):
            raise MailError("INVALID_MESSAGE_ID") from None
    if not all(0 < int(number) <= 4294967295 for number in (current, uid)):
        raise MailError("INVALID_MESSAGE_ID")
    return folder, current, int(uid)


def scoped_message_id(folder, current, uid):
    if folder == "INBOX":
        return f"{current}:{uid}"
    token = base64.urlsafe_b64encode(folder.encode("utf8")).decode().rstrip("=")
    return f"imap-v1:{token}:{current}:{uid}"


def imap_connect(request):
    client = None
    phase = "connect"
    try:
        host, port, security = endpoint(request.get("inbound"))
        context = ssl.create_default_context()
        client = (ImapTLS(host, port, ssl_context=context, timeout=TIMEOUT) if security == "tls"
                  else Imap(host, port, timeout=TIMEOUT))
        if security == "starttls":
            phase = "tls"
            client.starttls(ssl_context=context)
        phase = "auth"
        client.login(client._quote(request["credential"]["username"]), request["credential"]["password"])
        phase = "select"
        operation = request.get("operation")
        if operation == "read":
            selected = imap_identity(request.get("messageId"))[0]
        else:
            selected = resolve_folder(client, request.get("folder", "INBOX")) if operation == "search" else "INBOX"
        wire = mailbox_utf7(selected).replace(b"\\", b"\\\\").replace(b'"', b'\\"')
        status, _ = client.select(b'"' + wire + b'"', readonly=True)
        if status != "OK":
            raise MailError("INVALID_MAIL_FOLDER_NOT_FOUND")
        client.workspace_folder = selected
        return client
    except (OSError, imaplib.IMAP4.error, MailError) as error:
        if client:
            with contextlib.suppress(OSError):
                client.shutdown()
        raise MailError(error_code("imap", phase, error)) from None


def smtp_connect(request):
    client = None
    phase = "connect"
    try:
        host, port, security = endpoint(request.get("outbound"))
        context = ssl.create_default_context()
        options = {"local_hostname": "localhost", "timeout": TIMEOUT}
        client = (SmtpTLS(host, port, context=context, **options) if security == "tls"
                  else Smtp(host, port, **options))
        client.ehlo_or_helo_if_needed()
        if security == "starttls":
            phase = "tls"
            client.starttls(context=context)
            client.ehlo()
        phase = "auth"
        client.login(request["credential"]["username"], request["credential"]["password"])
        return client
    except (OSError, smtplib.SMTPException, MailError) as error:
        if client:
            client.close()
        raise MailError(error_code("smtp", phase, error)) from None


def validity(client):
    value = client.response("UIDVALIDITY")[1]
    if not value or not value[0] or not value[0].isdigit():
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    return value[0].decode("ascii")


def uid_search(client, *criteria):
    status, value = client.uid("SEARCH", None, *criteria)
    if status != "OK":
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    return [int(uid) for uid in (value[0] or b"").split()]


def fetch_mail(client, uid):
    status, values = client.uid("FETCH", str(uid), "(RFC822.SIZE INTERNALDATE)")
    metadata = b" ".join(value for value in values if isinstance(value, bytes))
    size = re.search(rb"RFC822.SIZE (\d+)", metadata)
    date = re.search(rb'INTERNALDATE "([^"]+)"', metadata)
    if status != "OK" or not size:
        raise MailError("NOT_FOUND")
    if int(size[1]) > MAX_MAIL:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    # Fetch the whole MIME without marking it seen. The complete IMAP literal
    # framing is authoritative; RFC822.SIZE can differ from generated MIME bytes.
    status, values = client.uid("FETCH", str(uid), "(BODY.PEEK[])")
    bodies = [(row[0], row[1]) for row in values if isinstance(row, tuple) and len(row) == 2]
    if status != "OK" or len(bodies) != 1:
        raise MailError("INVALID_MAIL_INCOMPLETE")
    prefix, raw = bodies[0]
    literal = re.search(rb"BODY\[\]\s+\{(\d+)\}$", prefix, re.IGNORECASE) if isinstance(prefix, bytes) else None
    if not isinstance(raw, bytes) or not raw or not literal or len(raw) != int(literal[1]):
        raise MailError("INVALID_MAIL_INCOMPLETE")
    if len(raw) > MAX_MAIL:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    received = datetime.strptime(date[1].decode("ascii"), "%d-%b-%Y %H:%M:%S %z").isoformat() if date else None
    return {"uid": uid, "internalDate": received, "raw": base64.b64encode(raw).decode("ascii")}


# Header lookup and MIME section retrieval deliberately avoid BODY[]/BODY[TEXT]:
# multipart TEXT includes attachments. Only explicitly selected text leaves or
# the requested attachment section ever cross the mail connection.
MAX_HEADER = 64 * 1024
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID REPLY-TO REFERENCES IN-REPLY-TO"


def parse_atom(atom):
    if not atom or atom == b")":
        raise MailError("INVALID_MAIL_INCOMPLETE")
    return None if atom.upper() == b"NIL" else int(atom) if atom.isdigit() else atom


def parse_fetch(values):
    """Parse bounded IMAP FETCH data, including quoted strings and literals."""
    chunks = []
    for row in values:
        if isinstance(row, tuple) and len(row) == 2 and all(isinstance(x, bytes) for x in row):
            declared = re.search(rb"\{(\d+)\}$", row[0])
            if not declared or int(declared[1]) != len(row[1]):
                raise MailError("INVALID_MAIL_INCOMPLETE")
            chunks.append(row[0] + b"\r\n" + row[1])
        elif isinstance(row, bytes):
            chunks.append(row)
        elif row is not None:
            raise MailError("INVALID_MAIL_INCOMPLETE")
    raw = b" ".join(chunks)
    if len(raw) > MAX_MAIL + 1024 * 1024:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    position, tokens = 0, 0

    def value(depth=0):
        nonlocal position, tokens
        tokens += 1
        if depth > 32 or tokens > 16384:
            raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
        while position < len(raw) and raw[position] in b" \r\n\t":
            position += 1
        if position >= len(raw):
            raise MailError("INVALID_MAIL_INCOMPLETE")
        start = position
        character = raw[position]
        position += 1
        if character == 40:
            result = []
            while True:
                while position < len(raw) and raw[position] in b" \r\n\t":
                    position += 1
                if position >= len(raw):
                    raise MailError("INVALID_MAIL_INCOMPLETE")
                if raw[position] == 41:
                    position += 1
                    return result
                result.append(value(depth + 1))
        if character == 34:
            result = bytearray()
            while position < len(raw):
                character = raw[position]; position += 1
                if character == 34:
                    return bytes(result)
                if character == 92:
                    if position >= len(raw):
                        break
                    character = raw[position]; position += 1
                result.append(character)
            raise MailError("INVALID_MAIL_INCOMPLETE")
        if character == 123:
            match = re.match(rb"(\d+)\}\r\n", raw[position:])
            if not match:
                raise MailError("INVALID_MAIL_INCOMPLETE")
            position += match.end()
            end = position + int(match[1])
            if end > len(raw):
                raise MailError("INVALID_MAIL_INCOMPLETE")
            result = raw[position:end]; position = end
            return result
        # Section names contain spaces and parenthesized HEADER.FIELDS lists.
        if raw[start:start + 5].upper() == b"BODY[":
            end = raw.find(b"]", start)
            if end < 0:
                raise MailError("INVALID_MAIL_INCOMPLETE")
            position = end + 1
        while position < len(raw) and raw[position] not in b" ()\r\n\t":
            position += 1
        return parse_atom(raw[start:position])

    records = []
    while position < len(raw):
        if not raw[position:].strip():
            break
        sequence, fields = value(), value()
        if type(sequence) is not int or not isinstance(fields, list) or len(fields) % 2:
            raise MailError("INVALID_MAIL_INCOMPLETE")
        record = {}
        for key, item in zip(fields[::2], fields[1::2]):
            if not isinstance(key, bytes):
                raise MailError("INVALID_MAIL_INCOMPLETE")
            record[key.decode("ascii").lower()] = item
        records.append(record)
    return records


def fetch_records(client, uids, fields, literal_limit=MAX_HEADER):
    client.literal_limit = literal_limit
    status, values = client.uid("FETCH", ",".join(str(uid) for uid in uids), fields)
    if status != "OK":
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    # IMAP permits unsolicited FLAGS updates interleaved with FETCH results.
    # They carry no requested content and must not masquerade as message data.
    return [record for record in parse_fetch(values) if set(record) - {"uid", "flags", "modseq"}]


def header_bytes(record):
    values = [value for key, value in record.items() if key.startswith("body[header.fields (")]
    if len(values) != 1 or not isinstance(values[0], bytes) or len(values[0]) > MAX_HEADER:
        raise MailError("INVALID_MAIL_INCOMPLETE")
    return values[0]


def fetch_summary(client, uids, current, folder="INBOX", has_attachment=None):
    if not uids:
        return []
    structure = " BODYSTRUCTURE" if has_attachment is not None else ""
    records = fetch_records(client, uids, f"(UID FLAGS{structure} BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
    wanted = set(uids); messages = {}
    for record in records:
        uid = record.get("uid")
        if uid not in wanted or uid in messages:
            raise MailError("INVALID_MAIL_INCOMPLETE")
        headers = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes(record))
        flags = [v.decode("ascii") for v in record.get("flags", []) if isinstance(v, bytes)]
        present = bool(message_parts(record.get("bodystructure"))[1]) if has_attachment is not None else None
        if has_attachment is not None and present != has_attachment:
            continue
        messages[uid] = {"id": scoped_message_id(folder, current, uid), "folder": folder,
                         **{name: str(headers.get(name, ""))[:8192] for name in ("from", "to", "cc", "subject", "date")},
                         **({"hasAttachment": present} if has_attachment is not None else {}),
                         "flags": flags, "read": "\\seen" in [v.lower() for v in flags], "replied": "\\answered" in [v.lower() for v in flags]}
    # A concurrent delete may remove a selected UID; do not manufacture a row.
    return [messages[uid] for uid in uids if uid in messages]


def message_parts(structure):
    """Select text leaves and stable attachment indices from BODYSTRUCTURE."""
    attachments, count = [], 0

    def decoded(value):
        return value.decode("utf8", "replace") if isinstance(value, bytes) else ""

    def params(value):
        if not isinstance(value, list) or len(value) % 2:
            return {}
        fields = {decoded(k).lower(): decoded(v) for k, v in zip(value[::2], value[1::2])}
        # MIME RFC2231 extended filenames are common on Exchange/Outlook.
        for key in list(fields):
            if key.endswith("*") and not re.search(r"\*\d+\*?$", key):
                charset, _language, encoded = decode_rfc2231(fields[key])
                try:
                    fields[key[:-1]] = unquote_to_bytes(encoded).decode(charset or "utf-8", "replace")
                except LookupError:
                    fields[key[:-1]] = unquote_to_bytes(encoded).decode("utf-8", "replace")
        return fields

    def visit(part, section="", depth=0):
        nonlocal count
        count += 1
        if depth > 20 or count > 128 or not isinstance(part, list) or not part:
            raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
        if isinstance(part[0], list):
            boundary = next((i for i, item in enumerate(part) if not isinstance(item, list)), len(part))
            subtype = decoded(part[boundary]).lower() if boundary < len(part) else ""
            parameters = params(part[boundary + 1]) if len(part) > boundary + 1 else {}
            disposition = part[boundary + 2] if len(part) > boundary + 2 else None
            disposition_kind = decoded(disposition[0]).lower() if isinstance(disposition, list) and disposition else ""
            disposition_params = params(disposition[1]) if isinstance(disposition, list) and len(disposition) > 1 else {}
            name = disposition_params.get("filename", parameters.get("name", ""))
            if name or disposition_kind == "attachment":
                # An attached multipart is one payload. Its descendants must not
                # be mistaken for text of the enclosing email.
                attachments.append({"id": str(len(attachments)), "section": section or "TEXT", "contentType": "multipart/" + subtype,
                                    "charset": "utf-8", "encoding": "7bit", "size": 0, "name": name or "attachment"})
                return []
            choices = [visit(child, f"{section}.{i + 1}".lstrip("."), depth + 1) for i, child in enumerate(part[:boundary])]
            if subtype == "alternative":
                return next((choice for choice in choices if any(p["contentType"] == "text/plain" for p in choice)), next((choice for choice in choices if choice), []))
            return [leaf for choice in choices for leaf in choice]
        if len(part) < 7 or type(part[6]) is not int or part[6] < 0:
            raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
        main, sub = decoded(part[0]).lower(), decoded(part[1]).lower()
        parameters = params(part[2]); disposition_index = 9 if main == "text" else 11 if main == "message" and sub == "rfc822" else 8
        disposition = part[disposition_index] if len(part) > disposition_index else None
        disposition_kind = decoded(disposition[0]).lower() if isinstance(disposition, list) and disposition else ""
        disposition_params = params(disposition[1]) if isinstance(disposition, list) and len(disposition) > 1 else {}
        name = disposition_params.get("filename", parameters.get("name", ""))
        leaf = {"section": section or "1", "contentType": main + "/" + sub, "charset": parameters.get("charset", "utf-8"),
                "encoding": decoded(part[5]), "size": part[6]}
        if name or disposition_kind == "attachment" or main != "text" or sub not in {"plain", "html"}:
            attachments.append({**leaf, "id": str(len(attachments)), "name": name or "attachment"})
            return []
        return [leaf]

    texts = visit(structure)
    return texts, attachments


def fetch_layout(client, uid):
    records = fetch_records(client, [uid], f"(UID BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
    if not records:
        raise MailError("NOT_FOUND")
    if len(records) != 1 or records[0].get("uid") != uid:
        raise MailError("INVALID_MAIL_INCOMPLETE")
    record = records[0]
    texts, attachments = message_parts(record.get("bodystructure"))
    return header_bytes(record), texts, attachments


def fetch_section(client, uid, section, limit, attachment=False):
    if not re.fullmatch(r"\d+(?:\.\d+)*", section) and not (attachment and section == "TEXT"):
        raise MailError("INVALID_MAIL_IMAP_PROTOCOL_FAILED")
    records = fetch_records(client, [uid], f"(UID BODY.PEEK[{section}])", limit)
    if len(records) != 1 or records[0].get("uid") != uid:
        raise MailError("INVALID_MAIL_INCOMPLETE")
    raw = records[0].get(f"body[{section}]")
    if not isinstance(raw, bytes):
        raise MailError("INVALID_MAIL_INCOMPLETE")
    if len(raw) > limit:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    return raw


def fetch_text_mail(client, uid):
    headers, texts, attachments = fetch_layout(client, uid)
    parts, total = [], 0
    for text in texts:
        raw = fetch_section(client, uid, text["section"], MAX_MAIL - total)
        total += len(raw)
        part = EmailMessage(policy=policy.SMTP)
        part.set_type(text["contentType"]); part.set_param("charset", text["charset"])
        if text["encoding"]:
            part["Content-Transfer-Encoding"] = text["encoding"]
        part.set_payload(raw)
        parts.append(part)
    if len(parts) > 1 and all(part.get_content_type() == "text/html" for part in parts):
        # HTML-only leaves must be a root document for downstream MIME parsers
        # to derive readable text, rather than children of synthetic mixed MIME.
        combined = EmailMessage(policy=policy.SMTP)
        combined.set_content("\n<hr>\n".join(part.get_content() for part in parts), subtype="html", charset="utf-8")
        parts = [combined]
    if len(parts) == 1:
        raw = headers.rstrip(b"\r\n") + b"\r\n" + parts[0].as_bytes()
        if len(raw) > MAX_MAIL:
            raise MailError("INVALID_MAIL_TOO_LARGE")
        return {"format": "text-v1", "raw": base64.b64encode(raw).decode("ascii"),
                "attachments": [{k: item[k] for k in ("id", "name", "size", "contentType")} for item in attachments]}
    boundary = ("agentsee-" + secrets.token_hex(16)).encode("ascii")
    raw = headers.rstrip(b"\r\n") + b'\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="' + boundary + b'"\r\n\r\n'
    raw += b"".join(b"--" + boundary + b"\r\n" + part.as_bytes() + b"\r\n" for part in parts) + b"--" + boundary + b"--\r\n"
    if len(raw) > MAX_MAIL:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    public_attachments = [{k: item[k] for k in ("id", "name", "size", "contentType")} for item in attachments]
    return {"format": "text-v1", "raw": base64.b64encode(raw).decode("ascii"), "attachments": public_attachments}


def fetch_attachment(client, uid, attachment_id):
    if not isinstance(attachment_id, str) or not re.fullmatch(r"\d+", attachment_id):
        raise MailError("INVALID_MESSAGE_ID")
    _, _, attachments = fetch_layout(client, uid)
    if int(attachment_id) >= len(attachments):
        raise MailError("NOT_FOUND")
    attachment = attachments[int(attachment_id)]
    raw = fetch_section(client, uid, attachment["section"], MAX_MAIL, attachment=True)
    part = EmailMessage(); part["Content-Transfer-Encoding"] = attachment["encoding"]; part.set_payload(raw)
    content = part.get_payload(decode=True)
    if content is None or len(content) > MAX_MAIL:
        raise MailError("INVALID_MAIL_TOO_LARGE")
    return {"format": "attachment-v1", "name": attachment["name"], "contentType": attachment["contentType"], "encoding": "base64", "data": base64.b64encode(content).decode("ascii")}


def quote_search(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("INVALID_MAIL_QUERY")
    return b'"' + value.encode("utf8").replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def search_dates(options):
    criteria = []
    for key, command in (("since", "SINCE"), ("before", "BEFORE")):
        date = options[key]
        if date is not None:
            if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise MailError("INVALID_MAIL_QUERY")
            try:
                value = datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                raise MailError("INVALID_MAIL_QUERY") from None
            criteria.extend([command, value.strftime("%d-%b-%Y")])
    if options['since'] and options['before'] and options['since'] >= options['before']:
        raise MailError('INVALID_MAIL_QUERY')
    return criteria


def search_mail(client, request, current):
    """Server-side search with date/UID paging; explicit header-only fallback.

    Old Exchange servers advertising only US-ASCII silently mis-search UTF8
    literals without CHARSET. Never present those empty results as full-text.
    """
    query = request.get("query", "")
    if not isinstance(query, str) or len(query) > 2000:
        raise MailError("INVALID_MAIL_QUERY")
    quote_search(query)
    options = {key: request.get(key) for key in ("since", "before", "cursor")}
    def option(match):
        key, value = match.group(1), match.group(2)
        if options[key] is not None and options[key] != value:
            raise MailError("INVALID_MAIL_QUERY")
        options[key] = value
        return ""
    query = re.sub(r"(?<!\S)(since|before|cursor):([^\s]+)", option, query).strip()
    if len(query) > 500:
        raise MailError("INVALID_MAIL_QUERY")
    limit = request.get("limit", 50)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise MailError("INVALID_MAIL_QUERY")
    has_attachment = request.get("hasAttachment")
    if "hasAttachment" in request and type(has_attachment) is not bool:
        raise MailError("INVALID_MAIL_QUERY")
    folder = getattr(client, "workspace_folder", folder_name(request.get("folder", "INBOX")))
    criteria = search_dates(options)
    terms = [query, options['since'], options['before']]
    fingerprint = hashlib.sha256(json.dumps([*terms, folder, has_attachment], ensure_ascii=False).encode()).hexdigest()
    cursor = options['cursor']
    original_count = None
    if cursor is not None:
        if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,8192}", cursor):
            raise MailError("INVALID_MAIL_QUERY")
        try:
            page = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        except (ValueError, UnicodeError):
            raise MailError("INVALID_MAIL_QUERY") from None
        legacy = isinstance(page, dict) and page.get('v') == 1 and folder == "INBOX" and has_attachment is None
        expected = hashlib.sha256(json.dumps(terms, ensure_ascii=False).encode()).hexdigest() if legacy else fingerprint
        if (not isinstance(page, dict) or page.get('v') not in {1, 2} or page.get('hash') != expected
                or (not legacy and (page.get('v') != 2 or page.get('folder') != folder))):
            raise MailError("INVALID_MAIL_QUERY")
        if page.get('validity') != current:
            raise MailError("INVALID_MAILBOX_CHANGED")
        before_uid = page.get('beforeUid')
        if type(before_uid) is not int or not 1 < before_uid <= 4294967295:
            raise MailError("INVALID_MAIL_QUERY")
        original_count = page.get('count')
        if original_count is not None and (type(original_count) is not int or not 0 <= original_count <= 4294967295):
            raise MailError('INVALID_MAIL_QUERY')
        criteria.extend(['UID', f'1:{before_uid - 1}'])
    fallback = False
    if query and not query.isascii():
        client.literal = query.encode('utf8')
        try:
            status, values = client.uid('SEARCH', None, 'CHARSET', 'UTF-8', *criteria, 'TEXT')
        except imaplib.IMAP4.error:
            raise MailError('INVALID_MAIL_QUERY') from None
        if status != 'OK':
            if not any(isinstance(value, bytes) and b'BADCHARSET' in value.upper() for value in values):
                raise MailError('INVALID_MAIL_QUERY')
            fallback = True
            ids = uid_search(client, *(criteria or ['ALL']))
        else:
            ids = [int(value) for value in (values[0] or b'').split()]
    else:
        ids = uid_search(client, *criteria, *(['TEXT', quote_search(query)] if query else ([] if criteria else ['ALL'])))
    ids = sorted(set(ids), reverse=True)
    if original_count is None:
        original_count = len(ids)
    chosen = ids[:limit]
    messages = fetch_summary(client, chosen, current, folder, has_attachment)
    if fallback:
        term = unicodedata.normalize('NFKC', query).casefold()
        messages = [message for message in messages if term in unicodedata.normalize('NFKC', ' '.join(str(message.get(k, '')) for k in ('from', 'to', 'cc', 'subject'))).casefold()]
    more = len(ids) > len(chosen)
    next_cursor = base64.urlsafe_b64encode(json.dumps({'v': 2, 'folder': folder, 'validity': current, 'beforeUid': min(chosen), 'hash': fingerprint, 'count': original_count}, separators=(',', ':')).encode()).decode().rstrip('=') if more else None
    legacy = ' '.join([*(f'{key}:{options[key]}' for key in ('since', 'before') if options[key]), query]).strip()
    next_query = f'cursor:{next_cursor} {legacy}'.strip() if next_cursor else None
    return {'messages': messages, 'nextCursor': next_cursor, 'nextQuery': next_query if next_query and len(next_query) <= 500 else None,
            'hasMore': more, 'folder': folder, 'totalMatches': None if fallback or has_attachment is not None else original_count, 'scannedMessages': len(chosen),
            'searchScope': 'headers' if fallback else 'full-text',
            'bodySearchSupported': not fallback,
            **({'searchNotice': 'This server supports only US-ASCII search. Non-ASCII terms are matched against headers across cursor pages; bodies are not scanned. Continue while hasMore, including empty pages.'} if fallback else {})}


def imap_operation(request):
    client = imap_connect(request)
    try:
        current = validity(client)
        operation = request["operation"]
        if operation == "verify":
            return {"verified": True}
        if operation == "search":
            return search_mail(client, request, current)
        if operation == "read":
            folder, wanted, uid = imap_identity(request.get("messageId", ""))
            if folder != getattr(client, "workspace_folder", folder):
                raise MailError("INVALID_MESSAGE_ID")
            if wanted != current:
                raise MailError("INVALID_MAILBOX_CHANGED")
            if request.get("format") == "text-v1":
                return fetch_text_mail(client, int(uid))
            if request.get("format") == "attachment-v1":
                return fetch_attachment(client, int(uid), request.get("attachmentId"))
            return fetch_mail(client, int(uid))
        if operation == "incoming":
            cursor = request.get("cursor") or {}
            after = int(cursor.get("uid", 0)) if cursor.get("validity") == current else 0
            since = datetime.fromisoformat(request["since"]).astimezone(UTC)
            ids = sorted(uid for uid in uid_search(client, "SINCE", since.strftime("%d-%b-%Y"), "UID", f"{after + 1}:*") if uid > after)[:25]
            messages, total = [], 0
            for uid in ids:
                if request.get("format") == "text-v1":
                    message = fetch_text_mail(client, uid)
                    metadata = fetch_records(client, [uid], "(UID INTERNALDATE)", MAX_HEADER)
                    if len(metadata) != 1 or metadata[0].get("uid") != uid:
                        raise MailError("INVALID_MAIL_INCOMPLETE")
                    internal_date = metadata[0].get("internaldate")
                    if not isinstance(internal_date, bytes):
                        raise MailError("INVALID_MAIL_INCOMPLETE")
                    message.update({"uid": uid, "internalDate": internal_date.decode("ascii")})
                else:
                    message = fetch_mail(client, uid)
                length = len(message["raw"])
                # A single MIME can be 10 MiB. Stop at a bounded response and
                # leave the next UID for the existing consumer's next request.
                if messages and total + length > (MAX_MAIL * 4 // 3 + 8):
                    break
                messages.append(message)
                total += length
            return {"messages": messages, "cursor": {"validity": current, "uid": messages[-1]["uid"] if messages else after}}
        raise MailError("INVALID_MAIL_OPERATION")
    finally:
        with contextlib.suppress(OSError):
            client.shutdown()  # No EXPUNGE, STORE, writable SELECT or logout side effects.


def execute(request):
    if request.get("operation") == "discover":
        return discover(request)
    credential = request.get("credential")
    if not isinstance(credential, dict) or any(not isinstance(credential.get(k), str) or not credential[k]
            or len(credential[k]) > 8192 or any(c in credential[k] for c in "\r\n\x00") for k in ("username", "password")):
        raise MailError("INVALID_MAIL_CONTENT")
    if request.get("operation") == "verify":
        if "mailRead" in request.get("services", []):
            imap_operation(request)
        if "mailSend" in request.get("services", []):
            smtp_connect(request).close()  # AUTH only. Never MAIL/RCPT/DATA.
        return {"verified": True}
    if request.get("operation") in {"read", "search", "incoming"}:
        if "mailRead" not in request.get("services", []):
            raise MailError("FORBIDDEN")
        return imap_operation(request)
    if request.get("operation") == "send":
        if "mailSend" not in request.get("services", []):
            raise MailError("FORBIDDEN")
        raw = base64.b64decode(request["raw"], validate=True)
        sender, recipient = request["from"], request["to"]
        if (len(raw) > 1024 * 1024 or not all(isinstance(v, str) and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", v) for v in (sender, recipient))):
            raise MailError("INVALID_MAIL_CONTENT")
        client = smtp_connect(request)
        try:
            # Never retry a submission: after DATA, a lost response is ambiguous.
            refused = client.sendmail(sender, [recipient], raw)
            if refused:
                raise MailError("INVALID_MAIL_REJECTED")
            return {"accepted": True}
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise MailError("INVALID_MAIL_REJECTED") from None
        except MailError:
            raise
        except (OSError, smtplib.SMTPException):
            raise MailError("INVALID_MAIL_SEND_OUTCOME_UNKNOWN") from None
        finally:
            client.close()
    raise MailError("INVALID_MAIL_OPERATION")


def discover(request):
    global TIMEOUT
    TIMEOUT = 3
    hosts = request.get("hosts")
    if not isinstance(hosts, list) or not 1 <= len(hosts) <= 4:
        raise MailError("INVALID_MAIL_SERVER")
    for host in hosts:
        endpoint({"host": host, "port": 993, "security": "tls"})

    def probe(host, protocol):
        for port, security in ([(993, "tls"), (143, "starttls")] if protocol == "imap" else [(587, "starttls"), (465, "tls")]):
            client = None
            try:
                context = ssl.create_default_context()
                if protocol == "imap":
                    client = (ImapTLS(host, port, ssl_context=context, timeout=TIMEOUT) if security == "tls"
                              else Imap(host, port, timeout=TIMEOUT))
                    if security == "starttls":
                        client.starttls(ssl_context=context)
                    # A TLS handshake with an unrelated service is insufficient.
                    status, caps = client.capability()
                    if status != "OK" or b"LOGINDISABLED" in b" ".join(caps).upper():
                        continue
                else:
                    options = {"local_hostname": "localhost", "timeout": TIMEOUT}
                    client = (SmtpTLS(host, port, context=context, **options) if security == "tls"
                              else Smtp(host, port, **options))
                    if client.ehlo()[0] != 250:
                        continue
                    if security == "starttls":
                        client.starttls(context=context)
                        if client.ehlo()[0] != 250:
                            continue
                    if not set(client.esmtp_features.get("auth", "").upper().split()) & {"PLAIN", "LOGIN", "CRAM-MD5"}:
                        continue
                return {"host": host, "port": port, "security": security}
            except (OSError, imaplib.IMAP4.error, smtplib.SMTPException, MailError):
                continue
            finally:
                if client:
                    with contextlib.suppress(OSError):
                        client.shutdown() if protocol == "imap" else client.close()

    # Each libc/OpenSSL worker can reserve a large virtual-memory arena. Stay
    # within the same 192 MiB process budget even for four candidate hosts.
    with ThreadPoolExecutor(max_workers=2) as pool:
        incoming = [pool.submit(probe, host, "imap") for host in hosts]
        outgoing = [pool.submit(probe, host, "smtp") for host in hosts]
        # MX is only a candidate. An endpoint is returned ONLY after verified
        # TLS plus the correct application protocol, with no LOGIN/AUTH/DATA.
        inbound = next((v for f in incoming if (v := f.result())), None)
        outbound = next((v for f in outgoing if (v := f.result())), None)
    return {"inbound": inbound, "outbound": outbound}


def main():
    operation = None
    def deadline(*_):
        # Stop even when a libc DNS lookup is stuck in a discovery thread.
        code = "INVALID_MAIL_SEND_OUTCOME_UNKNOWN" if operation == "send" else "INVALID_MAIL_TIMEOUT"
        print(json.dumps({"ok": False, "error": code}), flush=True)
        os._exit(0)
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(90)
    # Bound malicious server literals/SEARCH replies before imaplib allocates them.
    resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024, 192 * 1024 * 1024))
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise MailError("INVALID_MAIL_CONTENT")
        request = json.loads(raw)
        operation = request.get("operation")
        value = execute(request)
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=True))
    except Exception as error:  # health: allow BLE001 -- the sandbox protocol boundary must never echo untrusted vendor diagnostics or credentials
        # A provider's text may repeat a username or credential. Codes only.
        code = str(error) if isinstance(error, MailError) else "INVALID_MAIL_SEND_OUTCOME_UNKNOWN" if operation == "send" else "INVALID_MAIL_IMAP_PROTOCOL_FAILED"
        print(json.dumps({"ok": False, "error": code}))


if __name__ == "__main__":
    main()
