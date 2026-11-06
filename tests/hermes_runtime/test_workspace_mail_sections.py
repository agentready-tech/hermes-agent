"""Synthetic IMAP section regressions. No socket, account, provider or Kubernetes I/O.

Protocol replies deliberately keep the UID after a literal in several cases and
advertise an Exchange RFC822.SIZE different from each selected MIME part.
"""
import base64
from email import policy
from email.parser import BytesParser
import json
import re
import unittest
from unittest.mock import patch

from hermes_runtime.workspace import sandbox_mail as mail

HEADERS = (b'From: =?UTF-8?B?0JvQuNC0?= <lead@example.invalid>\r\n'
           b'To: Person <person@example.invalid>, Other <other@example.invalid>\r\n'
           b'Cc: Copy <copy@example.invalid>\r\n'
           b'Subject: =?UTF-8?B?0J/RgNC40LLQtdGC?=\r\n'
           b'Date: Fri, 06 Nov 2026 08:00:00 +0000\r\n'
           b'Message-ID: <fixture@example.invalid>\r\n\r\n')
PLAIN = b'Hello =D0=BC=D0=B8=D1=80!\r\n'
HTML = base64.b64encode('<html><body><p>Hello мир!</p></body></html>'.encode())
PDF = base64.b64encode(b'%PDF fixture-only attachment')
TEXT_ATTACHMENT = base64.b64encode(b'Attached note is not the email body')
IMAGE = base64.b64encode(b'fixture image attachment bytes')
RFC822 = base64.b64encode(b'From: nested@example.invalid\r\nSubject: forwarded\r\n\r\nNested message')


def text_structure(subtype=b'PLAIN', encoding=b'QUOTED-PRINTABLE', size=23):
    return [b'TEXT', subtype, [b'CHARSET', b'UTF-8'], None, None,
            encoding, size, 1, None, None, None]


def attachment_structure(major, minor, filename, encoding=b'BASE64', size=123):
    parameters = [b'NAME', filename] if filename else None
    return [major, minor, parameters, None, None, encoding, size,
            None, [b'ATTACHMENT', [b'FILENAME', filename]] if filename else None,
            None]


def nested_structure():
    plain, html = text_structure(), text_structure(b'HTML', b'BASE64', len(HTML))
    pdf = attachment_structure(b'APPLICATION', b'PDF', b'report.pdf', size=512_000_000)
    note = text_structure(size=len(TEXT_ATTACHMENT))
    note[2] = [b'CHARSET', b'UTF-8', b'NAME', b'note.txt']
    note[5] = b'BASE64'
    note[9] = [b'ATTACHMENT', [b'FILENAME', b'note.txt']]
    image = attachment_structure(b'IMAGE', b'PNG', b'logo.png', size=len(IMAGE))
    image[3] = b'<logo@fixture.invalid>'
    image[8] = [b'INLINE', [b'FILENAME', b'logo.png']]
    forwarded = [b'MESSAGE', b'RFC822', None, None, None, b'BASE64', len(RFC822),
                 [None] * 10, text_structure(), 1, None,
                 [b'ATTACHMENT', [b'FILENAME', b'forwarded.eml']], None]
    return [[plain, html, b'ALTERNATIVE', [b'BOUNDARY', b'alt'], None, None],
            pdf, note, image, forwarded, b'MIXED', [b'BOUNDARY', b'mixed'], None, None]


def imap(value):
    if value is None:
        return b'NIL'
    if isinstance(value, int):
        return str(value).encode()
    if isinstance(value, list):
        return b'(' + b' '.join(imap(v) for v in value) + b')'
    return b'"' + value.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'


class SectionClient:
    """Fail closed if the reader tries a whole message, TEXT, or an unwanted part."""
    def __init__(self, structure=None, parts=None, uids=(41, 42), flags=b'\\Seen \\Answered'):
        self.structure = nested_structure() if structure is None else structure
        self.parts = {'1.1': PLAIN, '1.2': HTML, '2': PDF, '3': TEXT_ATTACHMENT,
                      '4': IMAGE, '5': RFC822} if parts is None else parts
        self.uids = uids
        self.flags = flags
        self.calls = []
        self.payload_sections = []
        self.shutdown_called = False
        self.literal_limit = mail.MAX_MAIL

    def _quote(self, value):
        return '"' + value + '"'

    def response(self, kind):
        assert kind == 'UIDVALIDITY'
        return kind, [b'7']

    def shutdown(self):
        self.shutdown_called = True

    def uid(self, command, uid, query=None, *extra):
        self.calls.append((command, uid, query, extra))
        if command.upper() == 'SEARCH':
            return 'OK', [b' '.join(str(i).encode() for i in self.uids)]
        assert command.upper() == 'FETCH', 'Read-only diagnostic must only SEARCH/FETCH'
        expression = str(query).upper()
        if 'BODY.PEEK[]' in expression or 'BODY.PEEK[TEXT]' in expression:
            raise AssertionError('Whole MIME/TEXT would download attachment payloads')
        wanted = [int(i) for i in str(uid).split(',')]
        data = []
        for sequence, message_uid in enumerate(wanted, 1):
            if 'HEADER.FIELDS' in expression:
                prefix = f'{sequence} (FLAGS ({self.flags.decode()}) '.encode()
                if 'BODYSTRUCTURE' in expression:
                    prefix += b'BODYSTRUCTURE ' + imap(self.structure) + b' '
                prefix += b'BODY[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)] {' + str(len(HEADERS)).encode() + b'}'
                data += [(prefix, HEADERS), f' UID {message_uid})'.encode()]
            elif 'BODYSTRUCTURE' in expression:
                data += [f'{sequence} (UID {message_uid} FLAGS ({self.flags.decode()}) '.encode()
                         + b'BODYSTRUCTURE ' + imap(self.structure)
                         + b' RFC822.SIZE 512001234 INTERNALDATE "06-Nov-2026 08:00:00 +0000")']
            else:
                match = re.search(r'BODY\.PEEK\[([0-9.]+)\]', expression)
                assert match, 'Only a selected numeric MIME section may be fetched'
                section = match[1]
                self.payload_sections.append(section)
                content = self.parts[section]
                data += [(f'{sequence} (BODY[{section}] {{{len(content)}}}'.encode(), content),
                         f' UID {message_uid})'.encode()]
        return 'OK', data


class FetchParser(unittest.TestCase):
    def test_literal_followed_by_uid_and_next_message_are_not_combined(self):
        records = mail.parse_fetch([
            (b'1 (FLAGS (\\Seen \\Answered) BODY[HEADER.FIELDS (FROM TO)] {5}', b'hello'),
            b' UID 41)',
            (b'2 (UID 42 BODY[HEADER.FIELDS (FROM TO)] {5}', b'world'), b' FLAGS ())'])
        self.assertEqual([r['uid'] for r in records], [41, 42])
        self.assertIn(b'hello', records[0].values())
        self.assertIn(b'world', records[1].values())
        self.assertIn(b'\\Seen', records[0]['flags'])

    def test_short_literal_cannot_consume_inserted_chunk_separator(self):
        with self.assertRaises(mail.MailError):
            mail.parse_fetch([(b'1 (UID 1 BODY[1] {6}', b'short'), b')'])

    def test_truncated_literal_and_unbalanced_structure_are_rejected(self):
        for response in [[(b'1 (UID 1 BODY[1] {10}', b'short'), b')'],
                         [b'1 (UID 1 BODYSTRUCTURE (("TEXT" "PLAIN")']]:
            with self.subTest(response=response), self.assertRaises(mail.MailError):
                mail.parse_fetch(response)


class MessageParts(unittest.TestCase):
    def test_nested_alternative_selects_body_but_not_named_text_or_inline_image(self):
        texts, attachments = mail.message_parts(nested_structure())
        self.assertEqual([p['section'] for p in texts], ['1.1'])
        self.assertEqual([a['section'] for a in attachments], ['2', '3', '4', '5'])
        self.assertEqual([a['id'] for a in attachments], ['0', '1', '2', '3'])
        self.assertEqual(attachments[0]['name'], 'report.pdf')
        self.assertEqual(attachments[0]['size'], 512_000_000)

    def test_attached_multipart_container_is_not_recursed_into_message_text(self):
        container = [text_structure(), b'MIXED', [b'BOUNDARY', b'attachment'],
                     [b'ATTACHMENT', [b'FILENAME', b'bundle.mime']], None]
        root = [text_structure(), container, b'MIXED', [b'BOUNDARY', b'root'], None, None]
        texts, attachments = mail.message_parts(root)
        self.assertEqual([item['section'] for item in texts], ['1'])
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]['name'], 'bundle.mime')
        self.assertEqual(attachments[0]['section'], '2')
        self.assertEqual(attachments[0]['contentType'], 'multipart/mixed')
        root_texts, root_attachments = mail.message_parts(container)
        self.assertEqual(root_texts, [])
        self.assertEqual(root_attachments[0]['section'], 'TEXT')

    def test_html_only_body_and_empty_plain_are_valid_text_parts(self):
        html, attachments = mail.message_parts(text_structure(b'HTML', b'BASE64', len(HTML)))
        self.assertEqual([p['section'] for p in html], ['1'])
        self.assertEqual(html[0]['contentType'], 'text/html')
        self.assertEqual(attachments, [])
        plain, _ = mail.message_parts(text_structure(size=0))
        self.assertEqual([p['section'] for p in plain], ['1'])

    def test_attachment_filename_parameter_excludes_text_without_disposition(self):
        attached = text_structure()
        attached[2] = [b'CHARSET', b'UTF-8', b'NAME', b'attached.txt']
        texts, attachments = mail.message_parts(attached)
        self.assertEqual(texts, [])
        self.assertEqual(attachments[0]['name'], 'attached.txt')

    def test_rfc2231_filename_and_quoted_parameter_are_decoded(self):
        attachment = attachment_structure(b'APPLICATION', b'PDF', b'placeholder')
        attachment[2] = None
        attachment[8] = [b'ATTACHMENT', [b"FILENAME*", b"utf-8''report%20%D0%BC%D0%B8%D1%80.pdf"]]
        _, attachments = mail.message_parts(attachment)
        self.assertEqual(attachments[0]['name'], 'report мир.pdf')

    def test_unbounded_or_malformed_mime_tree_fails_closed(self):
        nested = text_structure()
        for _ in range(100):
            nested = [nested, b'MIXED']
        for structure in [nested, [b'TEXT'], [None, None, None]]:
            with self.subTest(structure_type=type(structure).__name__), self.assertRaises(mail.MailError):
                mail.message_parts(structure)


class SelectedFetch(unittest.TestCase):
    def test_operation_specific_literal_budget_rejects_before_base_read(self):
        class Reader:
            def read(self, size):
                self.body_was_read = True
                return b'x' * size
            def shutdown(self):
                self.closed = True
        class Bounded(mail.BoundedImapLiteral, Reader):
            pass
        client = Bounded()
        client.literal_limit = 32
        client.body_was_read = False
        client.closed = False
        with self.assertRaisesRegex(mail.MailError, 'TOO_LARGE'):
            client.read(33)
        self.assertFalse(client.body_was_read, 'Header/part limits must apply before allocation')
        self.assertTrue(client.closed)

    def test_read_never_accepts_another_uid_returned_by_server(self):
        class OtherUid(SectionClient):
            def uid(self, command, uid, query=None, *extra):
                status, rows = super().uid(command, uid, query, *extra)
                altered = [(row[0].replace(b'UID 42', b'UID 99'), row[1]) if isinstance(row, tuple)
                           else row.replace(b'UID 42', b'UID 99') for row in rows]
                return status, altered
        client = OtherUid()
        with self.assertRaises(mail.MailError):
            mail.fetch_text_mail(client, 42)
        self.assertEqual(client.payload_sections, [])

    def test_search_returns_headers_and_flags_for_candidates_without_any_payload(self):
        client = SectionClient()
        messages = mail.fetch_summary(client, [42, 41], '7')
        self.assertEqual([m['id'] for m in messages], ['7:42', '7:41'])
        self.assertEqual(messages[0]['subject'], 'Привет')
        for field, address in [('from', 'lead@example.invalid'), ('to', 'person@example.invalid'), ('cc', 'copy@example.invalid')]:
            self.assertIn(address, json.dumps(messages[0][field]))
        self.assertTrue(messages[0]['read'])
        self.assertTrue(messages[0]['replied'])
        self.assertTrue(messages[0]['date'])
        self.assertEqual(client.payload_sections, [])
        self.assertTrue(all('HEADER.FIELDS' in str(c[2]) for c in client.calls))
        self.assertNotIn('text', messages[0])
        self.assertNotIn('raw', messages[0])

    def test_unsolicited_flags_metadata_does_not_hide_requested_mail(self):
        class WithFlagUpdates(SectionClient):
            def uid(self, command, uid, query=None, *extra):
                status, rows = super().uid(command, uid, query, *extra)
                return status, [b'99 (UID 99 FLAGS (\\Seen) MODSEQ (123))',
                                b'98 (FLAGS (\\Answered))'] + rows
        listing = WithFlagUpdates()
        messages = mail.fetch_summary(listing, [42], '7')
        self.assertEqual([item['id'] for item in messages], ['7:42'])
        self.assertEqual(listing.payload_sections, [])
        reader = WithFlagUpdates()
        value = mail.fetch_text_mail(reader, 42)
        self.assertEqual(value['format'], 'text-v1')
        self.assertEqual(reader.payload_sections, ['1.1'])

    def test_text_read_fetches_only_chosen_text_and_keeps_attachment_metadata(self):
        client = SectionClient()
        value = mail.fetch_text_mail(client, 42)
        message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(value['raw']))
        body_text = '\n'.join(p.get_content() for p in message.walk() if p.get_content_type() == 'text/plain')
        self.assertIn('Hello мир!', body_text)
        self.assertEqual(client.payload_sections, ['1.1'])
        self.assertEqual(len(value['attachments']), 4)
        self.assertEqual(value['attachments'][0]['name'], 'report.pdf')
        self.assertEqual(value['attachments'][0]['size'], 512_000_000)
        self.assertNotIn(PDF, base64.b64decode(value['raw']))
        self.assertEqual(message['Subject'], 'Привет')
        self.assertNotIn('RFC822.SIZE', ' '.join(str(c[2]) for c in client.calls))

    def test_html_only_and_base64_encoding_retain_html_content(self):
        client = SectionClient(structure=text_structure(b'HTML', b'BASE64', len(HTML)), parts={'1': HTML})
        value = mail.fetch_text_mail(client, 42)
        message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(value['raw']))
        html_parts = [p.get_content() for p in message.walk() if p.get_content_type() == 'text/html']
        self.assertIn('<p>Hello мир!</p>', '\n'.join(html_parts))
        self.assertEqual(client.payload_sections, ['1'])

    def test_attachment_tool_fetches_exactly_one_selected_section_and_decodes_transfer(self):
        client = SectionClient()
        value = mail.fetch_attachment(client, 42, '0')
        self.assertEqual(value['name'], 'report.pdf')
        self.assertEqual(value['contentType'], 'application/pdf')
        self.assertEqual(value['encoding'], 'base64')
        self.assertEqual(base64.b64decode(value['data']), b'%PDF fixture-only attachment')
        self.assertEqual(client.payload_sections, ['2'])

    def test_unknown_attachment_is_not_replaced_with_whole_message_fetch(self):
        client = SectionClient()
        with self.assertRaisesRegex(mail.MailError, 'NOT_FOUND'):
            mail.fetch_attachment(client, 42, '99')
        self.assertEqual(client.payload_sections, [])

    def test_invalid_selected_part_literal_remains_rejected(self):
        class Truncated(SectionClient):
            def uid(self, command, uid, query=None, *extra):
                if re.search(r'BODY\.PEEK\[[0-9.]+\]', str(query).upper()):
                    return 'OK', [(b'1 (UID 42 BODY[1.1] {999}', b'short'), b')']
                return super().uid(command, uid, query, *extra)
        with self.assertRaises(mail.MailError):
            mail.fetch_text_mail(Truncated(), 42)

    def test_execute_remains_readonly_and_rejects_changed_uidvalidity(self):
        client = SectionClient()
        request = {'operation': 'read', 'format': 'text-v1', 'messageId': '7:42',
                   'credential': {'username': 'synthetic@example.invalid', 'password': 'synthetic-only'},
                   'services': ['mailRead']}
        with patch.object(mail, 'imap_connect', return_value=client):
            value = mail.execute(request)
        self.assertIn('attachments', value)
        self.assertTrue(client.shutdown_called)
        self.assertEqual(client.payload_sections, ['1.1'])
        wrong = SectionClient()
        with patch.object(mail, 'imap_connect', return_value=wrong), self.assertRaisesRegex(mail.MailError, 'MAILBOX_CHANGED'):
            mail.execute({**request, 'messageId': '6:42'})
        self.assertEqual(wrong.calls, [])
        self.assertTrue(wrong.shutdown_called)


if __name__ == '__main__':
    unittest.main()
