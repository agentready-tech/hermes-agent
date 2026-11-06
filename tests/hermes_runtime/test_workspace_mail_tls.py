"""Real local TLS protocol fixtures. No provider, account or Kubernetes traffic."""
import base64
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from hermes_runtime.workspace import sandbox_mail as mail

MIME = b"From: lead@example.invalid\r\nSubject: Fixture\r\nContent-Type: text/plain\r\n\r\nSynthetic inquiry"


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *_):
        pass  # Certificate rejection intentionally aborts a fixture handshake.


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server, sock = self.server, self.request
        if server.tls_immediate:
            sock = server.context.wrap_socket(sock, server_side=True)
        stream = sock.makefile("rb")
        def reply(value):
            sock.sendall(value)
        reply(b"* OK fixture IMAP\r\n" if server.protocol == "imap" else b"220 fixture SMTP\r\n")
        while line := stream.readline():
            command = line.decode().rstrip("\r\n")
            # Record verbs, not passwords or MIME.
            parts = command.split(" ")
            verb = parts[1].upper() if server.protocol == "imap" else parts[0].upper()
            server.verbs.append(verb)
            if server.protocol == "imap":
                tag = parts[0].encode()
                if verb == "CAPABILITY":
                    reply(b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag + b" OK CAPABILITY\r\n")
                elif verb == "LOGIN":
                    if server.reject_auth:
                        reply(tag + b" NO rejected secret must never leave boundary\r\n")
                    else:
                        reply(tag + b" OK LOGIN\r\n")
                elif verb == "EXAMINE":
                    reply(b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7] valid\r\n" + tag + b" OK [READ-ONLY] EXAMINE\r\n")
                elif verb == "UID" and parts[2].upper() == "SEARCH":
                    reply(b"* SEARCH 1 2\r\n" + tag + b" OK SEARCH\r\n")
                elif verb == "UID" and parts[2].upper() == "FETCH":
                    if "RFC822.SIZE" in command:
                        size = server.advertised_size if server.advertised_size is not None else len(MIME)
                        reply(f'* 1 FETCH (UID {parts[3]} RFC822.SIZE {size} INTERNALDATE "05-Nov-2026 15:00:00 +0000")\r\n'.encode() + tag + b" OK FETCH\r\n")
                    else:
                        server.body_peek.append("BODY.PEEK[]" in command)
                        server.body_partial.append("BODY.PEEK[]<" in command)
                        if server.oversized_literal:
                            reply(f'* 1 FETCH (UID {parts[3]} BODY[] {{{mail.MAX_MAIL + 1}}}\r\n'.encode())
                            continue
                        reply(f'* 1 FETCH (UID {parts[3]} BODY[] {{{len(MIME)}}}\r\n'.encode() + MIME + b")\r\n" + tag + b" OK FETCH\r\n")
                else:
                    reply(tag + b" BAD unexpected\r\n")
            else:
                if verb == "EHLO":
                    reply(b"250-fixture\r\n250-STARTTLS\r\n250 AUTH PLAIN\r\n")
                elif verb == "STARTTLS":
                    reply(b"220 ready\r\n")
                    stream.close()
                    sock = server.context.wrap_socket(sock, server_side=True)
                    stream = sock.makefile("rb")
                elif verb == "AUTH":
                    reply(b"535 5.7.8 rejected\r\n" if server.reject_auth else b"235 2.7.0 OK\r\n")
                elif verb in {"MAIL", "RCPT"}:
                    reply(b"250 OK\r\n")
                elif verb == "DATA":
                    reply(b"354 ready\r\n")
                    while stream.readline() != b".\r\n":
                        pass
                    if server.drop_after_data:
                        break
                    reply(b"250 accepted\r\n")
                else:
                    reply(b"250 OK\r\n")
        stream.close()
        sock.close()


class MailProtocols(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cert, key = (str(Path(cls.directory.name) / name) for name in ("cert.pem", "key.pem"))
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=mail.fixture.invalid",
                        "-addext", "subjectAltName=DNS:mail.fixture.invalid", "-keyout", key, "-out", cert], capture_output=True, check=True)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cert, key)
        cls.trusted = ssl.create_default_context(cafile=cert)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def start_server(self, protocol, immediate, reject_auth=False, drop_after_data=False, advertised_size=None, oversized_literal=False):
        server = Server(("127.0.0.1", 0), Handler)
        server.protocol, server.tls_immediate = protocol, immediate
        server.reject_auth, server.drop_after_data = reject_auth, drop_after_data
        server.context, server.verbs, server.body_peek, server.body_partial = self.context, [], [], []
        server.advertised_size, server.oversized_literal = advertised_size, oversized_literal
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self, operation):
        return {"operation": operation, "credential": {"username": "fixture@example.invalid", "password": "synthetic-only"},
                "services": ["mailRead", "mailSend"], "inbound": {"host": "mail.fixture.invalid", "port": 993, "security": "tls"},
                "outbound": {"host": "mail.fixture.invalid", "port": 587, "security": "starttls"}}

    def network(self, imap=None, smtp=None):
        def connect(_host, port):
            server = imap if port in {993, 143} else smtp
            if not server:
                raise TimeoutError()
            return socket.create_connection(server.server_address, timeout=2)
        stack = patch.multiple(mail, public_socket=connect)
        self.addCleanup(stack.stop)
        stack.start()
        tls = patch.object(mail.ssl, "create_default_context", return_value=self.trusted)
        tls.start()
        self.addCleanup(tls.stop)

    def test_secure_verify_logs_in_but_never_sends_and_never_mutates_mailbox(self):
        imap, smtp = self.start_server("imap", True), self.start_server("smtp", False)
        self.network(imap, smtp)
        self.assertEqual(mail.execute(self.request("verify")), {"verified": True})
        self.assertIn("LOGIN", imap.verbs)
        self.assertIn("EXAMINE", imap.verbs)
        self.assertNotIn("SELECT", imap.verbs)
        self.assertIn("STARTTLS", smtp.verbs)
        self.assertIn("AUTH", smtp.verbs)
        self.assertNotIn("MAIL", smtp.verbs)
        self.assertNotIn("DATA", smtp.verbs)

    def test_discovery_checks_tls_and_protocol_without_any_credentials_or_login(self):
        imap, smtp = self.start_server("imap", True), self.start_server("smtp", False)
        self.network(imap, smtp)
        value = mail.execute({"operation": "discover", "hosts": ["mail.fixture.invalid"]})
        self.assertEqual(value["inbound"]["port"], 993)
        self.assertEqual(value["outbound"], {"host": "mail.fixture.invalid", "port": 587, "security": "starttls"})
        self.assertNotIn("LOGIN", imap.verbs)
        self.assertNotIn("AUTH", smtp.verbs)
        self.assertNotIn("MAIL", smtp.verbs)

    def test_uid_validity_and_read_only_fetch_preserve_cursor_and_never_mark_seen(self):
        imap = self.start_server("imap", True)
        self.network(imap)
        request = {**self.request("read"), "messageId": "7:1"}
        self.assertEqual(base64.b64decode(mail.execute(request)["raw"]), MIME)
        with self.assertRaisesRegex(mail.MailError, "MAILBOX_CHANGED"):
            mail.execute({**request, "messageId": "6:1"})
        incoming = mail.execute({**self.request("incoming"), "since": "2026-11-05T00:00:00Z", "cursor": {"validity": "7", "uid": 1}})
        self.assertEqual([m["uid"] for m in incoming["messages"]], [2])
        self.assertEqual(incoming["cursor"], {"validity": "7", "uid": 2})
        self.assertTrue(all(imap.body_peek))
        self.assertFalse(set(imap.verbs) & {"SELECT", "STORE", "EXPUNGE"})

    def test_exchange_mime_size_estimate_does_not_reject_complete_literal(self):
        for estimate in [len(MIME) - 12, len(MIME) + 12]:
            with self.subTest(estimate=estimate):
                imap = self.start_server("imap", True, advertised_size=estimate)
                self.network(imap)
                self.assertEqual(base64.b64decode(mail.execute({**self.request("read"), "messageId": "7:1"})["raw"]), MIME)
                self.assertTrue(all(imap.body_peek))
                self.assertFalse(any(imap.body_partial), "Completeness must come from a full PEEK literal")
                self.assertFalse(set(imap.verbs) & {"SELECT", "STORE", "EXPUNGE"})

    def test_underestimated_message_is_bounded_before_reading_oversized_literal(self):
        imap = self.start_server("imap", True, advertised_size=100, oversized_literal=True)
        self.network(imap)
        with self.assertRaisesRegex(mail.MailError, "^INVALID_MAIL_TOO_LARGE$"):
            mail.execute({**self.request("read"), "messageId": "7:1"})

    def test_incomplete_or_wrong_body_literal_is_still_rejected(self):
        class Client:
            def __init__(self, body):
                self.body = body
            def uid(self, command, _uid, query):
                if "RFC822.SIZE" in query:
                    return "OK", [b'1 (UID 1 RFC822.SIZE 10)']
                return "OK", self.body
        for body in [[(b'1 (BODY[] {10}', b'short'), b')'], [(b'1 (OTHER {5}', b'short'), b')'], [b'1 (BODY[] NIL)']]:
            with self.subTest(body=body), self.assertRaisesRegex(mail.MailError, "^INVALID_MAIL_INCOMPLETE$"):
                mail.fetch_mail(Client(body), 1)

    def test_auth_and_connect_errors_are_distinct_and_provider_text_is_not_returned(self):
        imap = self.start_server("imap", True, reject_auth=True)
        self.network(imap)
        request = self.request("read")
        with self.assertRaisesRegex(mail.MailError, "^INVALID_MAIL_IMAP_AUTH_FAILED$"):
            mail.execute(request)
        with patch.object(mail, "public_socket", side_effect=TimeoutError("private provider diagnostics")):
            with self.assertRaisesRegex(mail.MailError, "^INVALID_MAIL_IMAP_UNREACHABLE$"):
                mail.execute(request)

    def test_unknown_smtp_outcome_is_not_retried(self):
        smtp = self.start_server("smtp", False, drop_after_data=True)
        self.network(smtp=smtp)
        request = {**self.request("send"), "from": "fixture@example.invalid", "to": "recipient@example.invalid", "raw": base64.b64encode(MIME).decode()}
        with self.assertRaisesRegex(mail.MailError, "SEND_OUTCOME_UNKNOWN"):
            mail.execute(request)
        self.assertEqual(smtp.verbs.count("DATA"), 1)

    def test_wrong_tls_name_is_rejected_and_search_cannot_inject_commands(self):
        imap = self.start_server("imap", True)
        self.network(imap)
        request = self.request("read")
        request["inbound"]["host"] = "different.fixture.invalid"
        with self.assertRaisesRegex(mail.MailError, "^INVALID_MAIL_IMAP_TLS_FAILED$"):
            mail.execute(request)
        with self.assertRaisesRegex(mail.MailError, "INVALID_MAIL_QUERY"):
            mail.execute({**self.request("search"), "query": "test\r\nBAD"})
        self.assertNotIn("UID", imap.verbs)


class Boundaries(unittest.TestCase):
    def test_private_or_mixed_dns_never_opens_a_socket(self):
        for addresses in [["127.0.0.1"], ["8.8.8.8", "169.254.169.254"], ["10.216.0.1"]]:
            with patch.object(mail.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (a, 993)) for a in addresses]), patch.object(mail.socket, "create_connection") as connect:
                with self.assertRaisesRegex(mail.MailError, "PUBLIC_MAIL_SERVER"):
                    mail.public_socket("fixture.invalid", 993)
                connect.assert_not_called()

    def test_credentials_and_search_cannot_inject_protocol_commands(self):
        request = {"operation": "verify", "credential": {"username": "x\r\nBAD", "password": "synthetic"}}
        with self.assertRaisesRegex(mail.MailError, "MAIL_CONTENT"):
            mail.execute(request)



if __name__ == "__main__":
    unittest.main()
