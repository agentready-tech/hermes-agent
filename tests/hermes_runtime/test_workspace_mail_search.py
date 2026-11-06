"""Real loopback TLS search regressions; no provider/accounts/Kubernetes traffic.

The Exchange-like fixture supports ASCII SEARCH only by default, rejects every
CHARSET, and a second fixture supports explicit UTF-8 synchronizing literals. Search
results contain 123 UIDs so a fifty-message cap cannot masquerade as pagination.
"""
from contextlib import contextmanager
import base64
from datetime import datetime
from pathlib import Path
import re
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from hermes_runtime.workspace import sandbox_mail as mail

HEADER = (b'From: Synthetic Lead <lead@example.invalid>\r\n'
          b'To: Inbox <inbox@example.invalid>\r\n'
          b'Cc: Copy <copy@example.invalid>\r\n'
          b'Subject: Search fixture\r\n'
          b'Date: Fri, 06 Nov 2026 08:00:00 +0000\r\n\r\n')


def uid_set(value):
    result = []
    for part in value.split(b','):
        if b':' in part:
            first, last = map(int, part.split(b':'))
            result.extend(range(first, last + 1))
        else:
            result.append(int(part))
    return result


class SearchServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *_):
        pass


class SearchHandler(socketserver.BaseRequestHandler):
    def handle(self):
        owner = self.server
        with owner.context.wrap_socket(self.request, server_side=True) as connection:
            stream = connection.makefile('rb')
            connection.sendall(b'* OK Exchange-like synthetic fixture\r\n')
            while line := stream.readline():
                tag, _, rest = line.partition(b' ')
                verb = rest.split(b' ', 1)[0].strip().upper()
                owner.verbs.append(verb)
                if verb == b'CAPABILITY':
                    connection.sendall(b'* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n' + tag + b' OK capability\r\n')
                elif verb == b'LOGIN':
                    connection.sendall(tag + b' OK logged in\r\n')
                elif verb == b'EXAMINE':
                    connection.sendall(b'* 123 EXISTS\r\n* OK [UIDVALIDITY 7] fixture\r\n' + tag + b' OK [READ-ONLY] examined\r\n')
                elif verb == b'UID' and re.match(rb'UID SEARCH\b', rest, re.IGNORECASE):
                    literal = None
                    marker = re.search(rb'\{(\d+)\}\r\n$', line)
                    charset = bool(re.search(rb'\bCHARSET\b', rest, re.IGNORECASE))
                    if charset and not owner.utf8_charset:
                        # Real servers can refuse the charset before asking the
                        # client to send its synchronizing literal.
                        owner.searches.append({'wire': rest.rstrip(b'\r\n'), 'literal': None})
                        connection.sendall(tag + b' NO [BADCHARSET (US-ASCII)] Unsupported charset\r\n')
                        continue
                    if marker:
                        connection.sendall(b'+ continue synthetic literal\r\n')
                        literal = stream.read(int(marker[1]))
                        trailer = stream.readline()
                        if not trailer.endswith(b'\r\n'):
                            return
                    owner.searches.append({'wire': rest.rstrip(b'\r\n'), 'literal': literal})
                    if any(byte >= 128 for byte in rest) and literal is None:
                        connection.sendall(tag + b' BAD Invalid quoted non-ASCII text\r\n')
                    else:
                        ids = owner.utf8_matches if charset and literal is not None else list(owner.uids)
                        uid_filter = re.search(rb'\bUID\s+(\d+):(\d+)\b', rest, re.IGNORECASE)
                        if uid_filter:
                            lower, upper = map(int, uid_filter.groups())
                            ids = [uid for uid in ids if lower <= uid <= upper]
                        connection.sendall(b'* SEARCH ' + b' '.join(str(uid).encode() for uid in ids) + b'\r\n' + tag + b' OK searched\r\n')
                elif verb == b'UID' and re.match(rb'UID FETCH\b', rest, re.IGNORECASE):
                    parts = rest.split(b' ', 3)
                    selected = uid_set(parts[2])
                    fields = parts[3].upper()
                    owner.fetches.append({'uids': selected, 'fields': fields})
                    if b'BODY.PEEK[HEADER.FIELDS (' not in fields or b'BODY.PEEK[]' in fields or b'BODY.PEEK[TEXT]' in fields:
                        owner.body_fetch_attempted = True
                        connection.sendall(tag + b' BAD Body or attachment retrieval forbidden in search\r\n')
                        continue
                    for sequence, uid in enumerate(selected, 1):
                        headers = owner.headers.get(uid, HEADER)
                        prefix = f'* {sequence} FETCH (UID {uid} FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {{{len(headers)}}}\r\n'.encode()
                        connection.sendall(prefix + headers + b')\r\n')
                    connection.sendall(tag + b' OK headers fetched\r\n')
                elif verb == b'LOGOUT':
                    connection.sendall(b'* BYE fixture\r\n' + tag + b' OK logout\r\n')
                    return
                else:
                    connection.sendall(tag + b' BAD unexpected command\r\n')
            stream.close()


class SearchTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cert, key = (str(Path(cls.directory.name) / name) for name in ('cert.pem', 'key.pem'))
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=search.fixture.invalid', '-addext', 'subjectAltName=DNS:search.fixture.invalid',
                        '-keyout', key, '-out', cert], capture_output=True, check=True)
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(cert, key)
        cls.trusted = ssl.create_default_context(cafile=cert)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @contextmanager
    def connected(self, utf8_charset=False, headers=None, utf8_matches=None):
        server = SearchServer(('127.0.0.1', 0), SearchHandler)
        server.context = self.tls
        server.uids = list(range(1, 124))
        server.utf8_charset = utf8_charset
        server.utf8_matches = [123, 2] if utf8_matches is None else utf8_matches
        server.headers = {} if headers is None else headers
        server.verbs, server.searches, server.fetches = [], [], []
        server.body_fetch_attempted = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def connect(*_):
            return socket.create_connection(server.server_address, timeout=2)
        client = None
        try:
            with patch.object(mail, 'public_socket', side_effect=connect), patch.object(mail.ssl, 'create_default_context', return_value=self.trusted):
                client = mail.imap_connect({'inbound': {'host': 'search.fixture.invalid', 'port': 993, 'security': 'tls'},
                                            'credential': {'username': 'synthetic@example.invalid', 'password': 'synthetic-only'}})
            # Authentication can use imaplib's private quoting. SEARCH must never
            # depend on its unstable str/bytes return type.
            with patch.object(client, '_quote', side_effect=AssertionError('SEARCH used private imaplib._quote')):
                yield client, server
        finally:
            if client is not None:
                client.shutdown()
            server.shutdown()
            server.server_close()

    def test_ascii_search_uses_default_charset_and_quotes_locally(self):
        with self.connected() as (client, server):
            result = mail.search_mail(client, {'query': 'invoice'}, '7')
            self.assertTrue(result['messages'])
            self.assertTrue(server.searches)
            self.assertFalse(any(b'CHARSET' in entry['wire'].upper() for entry in server.searches))
            self.assertIn(b'TEXT "invoice"', server.searches[-1]['wire'])
            self.assertFalse(server.body_fetch_attempted)
            self.assertFalse(set(server.verbs) & {b'SELECT', b'STORE', b'EXPUNGE'})

    def test_unicode_capable_server_uses_explicit_utf8_literal_and_fulltext_results(self):
        with self.connected(utf8_charset=True) as (client, server):
            result = mail.search_mail(client, {'query': 'счёт на оплату'}, '7')
            self.assertEqual([item['id'] for item in result['messages']], ['7:123', '7:2'])
            self.assertEqual(server.searches[-1]['literal'], 'счёт на оплату'.encode())
            self.assertIn(b'CHARSET UTF-8', server.searches[-1]['wire'].upper())
            self.assertEqual(result['searchScope'], 'full-text')
            self.assertTrue(result['bodySearchSupported'])
            self.assertEqual(result['totalMatches'], 2)
            self.assertFalse(server.body_fetch_attempted)

    def test_ascii_only_server_fallback_pages_headers_honestly_including_empty_page(self):
        def subject(value):
            encoded = base64.b64encode(value.encode())
            return HEADER.replace(b'Subject: Search fixture', b'Subject: =?UTF-8?B?' + encoded + b'?=')
        # The match is older than the first fifty messages. A second differently
        # cased match proves Unicode decoding plus casefold, not raw-byte lookup.
        with self.connected(headers={40: subject('СЧЁТ на оплату'), 1: subject('Последний счёт')}) as (client, server):
            first = mail.search_mail(client, {'query': 'счёт', 'limit': 50}, '7')
            self.assertEqual(first['messages'], [])
            self.assertTrue(first['hasMore'])
            self.assertTrue(first['nextCursor'])
            second = mail.search_mail(client, {'query': 'счёт', 'limit': 50, 'cursor': first['nextCursor']}, '7')
            third = mail.search_mail(client, {'query': 'счёт', 'limit': 50, 'cursor': second['nextCursor']}, '7')
            self.assertEqual([item['id'] for item in second['messages']], ['7:40'])
            self.assertEqual([item['id'] for item in third['messages']], ['7:1'])
            self.assertFalse(third['hasMore'])
            self.assertIsNone(third['nextCursor'])
            for page in [first, second, third]:
                self.assertEqual(page['searchScope'], 'headers')
                self.assertFalse(page['bodySearchSupported'])
                self.assertIsNone(page['totalMatches'])
                self.assertTrue(page['searchNotice'])
                self.assertLessEqual(page['scannedMessages'], 50)
            self.assertEqual(sum(len(row['uids']) for row in server.fetches), 123)
            self.assertFalse(server.body_fetch_attempted)
            # No unlabelled UTF8 literal fallback which an ASCII-only server may
            # silently report as an empty full-text match.
            for entry in server.searches:
                if entry['literal'] is not None:
                    self.assertIn(b'CHARSET UTF-8', entry['wire'].upper())

    def test_dates_are_real_server_side_since_and_exclusive_before_criteria(self):
        with self.connected() as (client, server):
            mail.search_mail(client, {'query': 'invoice', 'since': '2026-10-01', 'before': '2026-11-01'}, '7')
            wire = server.searches[-1]['wire'].upper()
            self.assertRegex(wire, rb'\bSINCE\s+"?01-OCT-2026"?')
            self.assertRegex(wire, rb'\bBEFORE\s+"?01-NOV-2026"?')
            self.assertIn(b'TEXT "INVOICE"', wire)
            self.assertFalse(server.body_fetch_attempted)

    def test_legacy_query_date_tokens_work_without_panel_argument_changes(self):
        with self.connected() as (client, server):
            mail.search_mail(client, {'query': 'invoice since:2026-10-01 before:2026-11-01'}, '7')
            wire = server.searches[-1]['wire'].upper()
            self.assertIn(b'SINCE', wire)
            self.assertIn(b'BEFORE', wire)
            self.assertNotIn(b'SINCE:2026', wire)
            self.assertNotIn(b'BEFORE:2026', wire)
            self.assertIn(b'TEXT "INVOICE"', wire)

    def test_pagination_returns_older_than_first_fifty_without_body_fetch(self):
        with self.connected() as (client, server):
            request, seen, pages = {'query': '', 'limit': 50}, [], []
            while True:
                page = mail.search_mail(client, request, '7')
                page_ids = [int(item['id'].split(':')[1]) for item in page['messages']]
                seen.extend(page_ids)
                pages.append(len(page_ids))
                self.assertLessEqual(len(page_ids), 50)
                self.assertEqual(page['totalMatches'], 123)
                if not page['hasMore']:
                    self.assertIsNone(page['nextCursor'])
                    break
                self.assertIsInstance(page['nextCursor'], str)
                self.assertTrue(page['nextCursor'])
                request = {'query': '', 'limit': 50, 'cursor': page['nextCursor']}
                self.assertLess(len(pages), 5, 'Pagination cursor did not advance')
            self.assertEqual(pages, [50, 50, 23])
            self.assertEqual(seen, list(range(123, 0, -1)))
            self.assertTrue(all(len(entry['uids']) <= 50 for entry in server.fetches))
            self.assertEqual(sum(len(entry['uids']) for entry in server.fetches), 123)
            self.assertFalse(server.body_fetch_attempted)

    def test_small_limit_and_legacy_cursor_token_advance_same_query(self):
        with self.connected() as (client, server):
            first = mail.search_mail(client, {'query': 'invoice', 'limit': 2}, '7')
            second = mail.search_mail(client, {'query': 'invoice cursor:' + first['nextCursor'], 'limit': 2}, '7')
            self.assertEqual([m['id'] for m in first['messages']], ['7:123', '7:122'])
            self.assertEqual([m['id'] for m in second['messages']], ['7:121', '7:120'])
            self.assertTrue(all(len(row['uids']) == 2 for row in server.fetches))

    def test_quotes_backslashes_and_imap_keywords_remain_one_search_string(self):
        query = '"invoice" \\ ) UID SEARCH ALL'
        with self.connected() as (client, server):
            mail.search_mail(client, {'query': query}, '7')
            wire = server.searches[-1]['wire']
            self.assertIn(b'TEXT "\\"invoice\\" \\\\ ) UID SEARCH ALL"', wire)
            self.assertEqual(len(server.searches), 1)
            self.assertFalse(server.body_fetch_attempted)

    def test_control_characters_are_rejected_before_search_or_fetch(self):
        for query in ['invoice\r\nA99 STORE 1 +FLAGS (\\Seen)', 'invoice\x00', 'invoice\tALL', 'invoice\x7f']:
            with self.subTest(query_repr=repr(query)), self.connected() as (client, server):
                with self.assertRaises(mail.MailError):
                    mail.search_mail(client, {'query': query}, '7')
                self.assertEqual(server.searches, [])
                self.assertEqual(server.fetches, [])

    def test_invalid_or_reversed_dates_fail_without_protocol_search(self):
        for args in [{'since': '2026-02-30'}, {'before': '2026-13-01'}, {'since': '01-Nov-2026'},
                     {'since': '2026-11-02', 'before': '2026-11-01'}, {'since': '2026-11-01', 'before': '2026-11-01'}]:
            with self.subTest(args=args), self.connected() as (client, server):
                with self.assertRaises(mail.MailError):
                    mail.search_mail(client, {'query': '', **args}, '7')
                self.assertEqual(server.searches, [])

    def test_invalid_page_limits_are_not_silently_unbounded(self):
        for limit in [0, 51, -1, True, '50']:
            with self.subTest(limit=limit), self.connected() as (client, server):
                with self.assertRaises(mail.MailError):
                    mail.search_mail(client, {'query': '', 'limit': limit}, '7')
                self.assertEqual(server.searches, [])

    def test_cursor_is_bound_to_uidvalidity_and_query(self):
        with self.connected() as (client, server):
            first = mail.search_mail(client, {'query': 'invoice', 'limit': 2}, '7')
            before = len(server.searches)
            for current, query in [('8', 'invoice'), ('7', 'other search')]:
                with self.subTest(current=current, query=query), self.assertRaises(mail.MailError):
                    mail.search_mail(client, {'query': query, 'limit': 2, 'cursor': first['nextCursor']}, current)
            self.assertEqual(len(server.searches), before)

    def test_cursor_fingerprint_includes_both_date_bounds(self):
        with self.connected() as (client, server):
            args = {'query': 'invoice', 'limit': 2, 'since': '2026-10-01', 'before': '2026-11-01'}
            first = mail.search_mail(client, args, '7')
            before = len(server.searches)
            for change in [{'since': '2026-10-02'}, {'before': '2026-11-02'}, {'since': None}, {'before': None}]:
                with self.subTest(change=change), self.assertRaises(mail.MailError):
                    mail.search_mail(client, {**args, **change, 'cursor': first['nextCursor']}, '7')
            self.assertEqual(len(server.searches), before)


class QuoteSearch(unittest.TestCase):
    def test_quote_search_has_stable_bytes_contract_and_escapes_quoted_specials(self):
        value = mail.quote_search('a "quoted" \\ path')
        self.assertIsInstance(value, bytes)
        self.assertEqual(value, b'"a \\"quoted\\" \\\\ path"')

    def test_quote_search_does_not_accept_protocol_controls(self):
        for value in ['bad\r\ncommand', 'bad\x00', 'bad\x7f']:
            with self.subTest(value_repr=repr(value)), self.assertRaises(mail.MailError):
                mail.quote_search(value)


if __name__ == '__main__':
    unittest.main()
