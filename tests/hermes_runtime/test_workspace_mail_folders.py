"""Mailbox contracts through native MCP, encrypted temporary grants and fake IMAP.

No provider, socket, private account, browser or Kubernetes I/O. Protocol section
handling reuses SectionClient, which rejects whole-message and attachment reads
unless the corresponding explicit operation asks for a selected MIME section.
"""

import base64
import copy
import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from hermes_runtime.workspace import mcp, sandbox_mail as mail
from hermes_runtime.workspace.service import Workspace
from hermes_runtime.workspace.store import WorkspaceError
from tests.hermes_runtime.test_workspace_gmail import message as gmail_message
from tests.hermes_runtime.test_workspace_gmail import workspace as google_workspace
from tests.hermes_runtime.test_workspace_mail_sections import (
    SectionClient, nested_structure, text_structure,
)
from tests.hermes_runtime.test_workspace_native import ENV, ID, SECRET, connection


SENT = 'Почта/Отправленные "мои" \\ &'
CUSTOM = 'Проекты/Оплаты "2026" \\ & * %'


def modified_utf7(value):
    """Encode only fixture mailbox names, independently of the production codec."""
    chunks, pending = [], []
    def flush():
        if pending:
            payload = ''.join(pending).encode('utf-16-be')
            chunks.append('&' + base64.b64encode(payload).decode().rstrip('=').replace('/', ',') + '-')
            pending.clear()
    for character in value:
        if 32 <= ord(character) <= 126:
            flush()
            chunks.append('&-' if character == '&' else character)
        else:
            pending.append(character)
    flush()
    return ''.join(chunks).encode('ascii')


def quoted(value):
    return b'"' + value.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'


def message_id(folder, uid=42, validity='7'):
    encoded = base64.urlsafe_b64encode(folder.encode()).decode().rstrip('=')
    return f'imap-v1:{encoded}:{validity}:{uid}'


def box(*, flags=b'', uids=(42, 41), structure=None, parts=None):
    return {'flags': flags, 'uids': tuple(uids), 'validity': '7',
            'structure': nested_structure() if structure is None else structure,
            'parts': parts, 'by_uid': {}}


class MailboxClient(SectionClient):
    """Exercise LIST/quoted readonly selection while retaining real FETCH replies."""
    capabilities = (b'IMAP4rev1',)
    utf8_enabled = False
    _encoding = 'ascii'

    def __init__(self, host):
        super().__init__()
        self.host, self.selected = host, None
        self.selections, self.list_calls = [], []

    def login(self, username, password):
        assert username and password
        return 'OK', [b'authenticated synthetic account']

    def list(self, directory='""', pattern='*', *args):
        self.list_calls.append((directory, pattern, args))
        return 'OK', [b'(' + value['flags'] + b') "/" ' + quoted(modified_utf7(name))
                      for name, value in self.host.boxes.items()]

    def select(self, mailbox='INBOX', readonly=False):
        wire = mailbox if isinstance(mailbox, bytes) else mailbox.encode('ascii')
        self.selections.append((wire, readonly))
        assert readonly is True, 'Folder support must never use writable SELECT'
        assert not any(character in wire for character in (b'\r', b'\n', b'\x00'))
        for name in self.host.boxes:
            encoded = modified_utf7(name)
            bare_safe = not any(character in encoded for character in (b' ', b'"', b'\\', b'*', b'%'))
            if wire == quoted(encoded) or (bare_safe and wire == encoded):
                self.selected = name
                return 'OK', [str(len(self.host.boxes[name]['uids'])).encode()]
        return 'NO', [b'No such synthetic mailbox']

    def response(self, kind):
        assert kind == 'UIDVALIDITY' and self.selected is not None
        return kind, [self.host.boxes[self.selected]['validity'].encode()]

    def uid(self, command, uid, query=None, *extra):
        assert self.selected is not None
        current = self.host.boxes[self.selected]
        if command.upper() == 'SEARCH':
            self.calls.append((command, uid, query, extra))
            criteria = ' '.join(item.decode() if isinstance(item, bytes) else str(item)
                                for item in (query, *extra))
            bound = re.search(r'\bUID\s+1:(\d+)\b', criteria)
            uids = [value for value in current['uids'] if not bound or value <= int(bound[1])]
            return 'OK', [b' '.join(str(value).encode() for value in uids)]
        assert command.upper() == 'FETCH'
        rows = []
        for wanted in str(uid).split(','):
            value = int(wanted)
            assert value in current['uids'], 'FETCH must stay in the selected mailbox'
            self.structure = current['by_uid'].get(value, current['structure'])
            if current['parts'] is not None:
                self.parts = current['parts']
            status, reply = super().uid(command, wanted, query, *extra)
            assert status == 'OK'
            rows.extend(reply)
        return 'OK', rows


class MailboxHost:
    def __init__(self):
        self.boxes = {'INBOX': box(), SENT: box(flags=b'\\Sent'),
                      'Drafts': box(flags=b'\\Drafts'), 'Archive': box(flags=b'\\Archive'),
                      CUSTOM: box()}
        self.clients = []

    def connect(self, *args, **kwargs):
        client = MailboxClient(self)
        self.clients.append(client)
        return client


@pytest.fixture
def native_mail(tmp_path, monkeypatch):
    host = MailboxHost()
    monkeypatch.setattr(mail, 'ImapTLS', host.connect)
    def run(home, program, request):
        assert program == 'sandbox_mail'
        assert request['operation'] in {'search', 'read'}
        try:
            return mail.execute(request)
        except mail.MailError as error:
            raise WorkspaceError(str(error)) from None
    (tmp_path / 'config.yaml').write_text(f'autonomous:\n  workspace:\n    native: true\n    environment_id: {ENV}\n')
    workspace = Workspace(tmp_path, runner=run)
    workspace.store.upsert(connection(services=['mailRead']), SECRET)
    assert SECRET['password'].encode() not in workspace.store.vault._vault_path.read_bytes()
    return workspace, host


def reply(workspace, name, **arguments):
    return mcp.dispatch({'method': 'tools/call', 'params': {
        'name': name, 'arguments': {'connectionId': ID, **arguments}}}, workspace)


def call(workspace, name, **arguments):
    result = reply(workspace, name, **arguments)
    assert result['isError'] is False, result['content']
    return json.loads(result['content'][0]['text'])


def assert_rejected(workspace, name, **arguments):
    result = reply(workspace, name, **arguments)
    assert result['isError'] is True
    assert result['content'][0]['text'].startswith(('INVALID_', 'NOT_FOUND', 'FORBIDDEN'))


def assert_metadata_only(clients):
    assert all(not client.payload_sections for client in clients)
    assert all('BODY.PEEK[]' not in str(query).upper()
               and 'BODY.PEEK[TEXT]' not in str(query).upper()
               for client in clients for _, _, query, _ in client.calls)


def test_sent_search_then_read_and_attachment_do_not_need_folder_repeated(native_mail):
    workspace, host = native_mail
    sent = call(workspace, 'mail_search', folder='Sent', limit=1)
    identity = sent['messages'][0]['id']
    assert identity == message_id(SENT)
    assert_metadata_only(host.clients)
    read = call(workspace, 'mail_read', messageId=identity)
    assert 'Hello мир!' in read['text']
    assert read['attachments'][0]['name'] == 'report.pdf'
    assert host.clients[-1].selected == SENT
    assert host.clients[-1].payload_sections == ['1.1']
    attachment = call(workspace, 'mail_attachment', messageId=identity,
                      attachmentId=read['attachments'][0]['id'])
    assert base64.b64decode(attachment['data']) == b'%PDF fixture-only attachment'
    assert host.clients[-1].selected == SENT
    assert host.clients[-1].payload_sections == ['2']


def test_same_uidvalidity_and_uid_in_two_mailboxes_have_distinct_readable_ids(native_mail):
    workspace, host = native_mail
    host.boxes['INBOX']['parts'] = {'1.1': b'Inbox original'}
    host.boxes[SENT]['parts'] = {'1.1': b'Sent original without a quoted reply'}
    inbox = call(workspace, 'mail_search', limit=1)['messages'][0]['id']
    sent = call(workspace, 'mail_search', folder='Sent', limit=1)['messages'][0]['id']
    assert inbox == '7:42' and sent == message_id(SENT) and inbox != sent
    assert call(workspace, 'mail_read', messageId=sent)['text'].strip() == 'Sent original without a quoted reply'
    assert call(workspace, 'mail_read', messageId=inbox)['text'].strip() == 'Inbox original'
    assert host.clients[-1].selected == 'INBOX'


def test_special_use_localized_mailbox_is_utf7_encoded_and_safely_quoted(native_mail):
    workspace, host = native_mail
    result = call(workspace, 'mail_search', folder='Sent', limit=1)
    assert result['messages'][0]['id'] == message_id(SENT)
    client = host.clients[-1]
    assert client.capabilities == (b'IMAP4rev1',), 'Fixture intentionally has no SPECIAL-USE capability'
    assert client.selected == SENT
    assert client.selections[-1] == (quoted(modified_utf7(SENT)), True)
    assert client.list_calls and client.shutdown_called


def test_arbitrary_unicode_hierarchical_mailbox_is_an_exact_name_not_a_pattern(native_mail):
    workspace, host = native_mail
    result = call(workspace, 'mail_search', folder=CUSTOM, limit=1)
    assert result['messages'][0]['id'] == message_id(CUSTOM)
    assert host.clients[-1].selected == CUSTOM
    assert host.clients[-1].selections[-1] == (quoted(modified_utf7(CUSTOM)), True)


@pytest.mark.parametrize('alias,actual', [('Drafts', 'Drafts'), ('Archive', 'Archive')])
def test_other_special_use_aliases_select_their_mailbox(native_mail, alias, actual):
    workspace, host = native_mail
    assert call(workspace, 'mail_search', folder=alias, limit=1)['messages'][0]['id'] == message_id(actual)
    assert host.clients[-1].selected == actual


def test_ambiguous_special_use_alias_refuses_without_search_or_fetch(native_mail):
    workspace, host = native_mail
    host.boxes['Another sent folder'] = box(flags=b'\\Sent')
    assert_rejected(workspace, 'mail_search', folder='Sent')
    assert all(not client.calls for client in host.clients)


def test_all_mail_is_not_silently_used_as_archive(native_mail):
    workspace, host = native_mail
    del host.boxes['Archive']
    host.boxes['All Mail'] = box(flags=b'\\All')
    assert_rejected(workspace, 'mail_search', folder='Archive')
    assert all(not client.calls for client in host.clients)


def test_literal_list_name_and_imaplib_empty_trailer_select_sent(native_mail, monkeypatch):
    workspace, host = native_mail
    encoded = modified_utf7(SENT)
    def listing(self, *args):
        return 'OK', [(b'(\\Sent) "/" {' + str(len(encoded)).encode() + b'}', encoded), b'']
    monkeypatch.setattr(MailboxClient, 'list', listing)
    assert call(workspace, 'mail_search', folder='Sent')['messages'][0]['id'] == message_id(SENT)
    assert host.clients[-1].selected == SENT


def test_quoted_backslash_delimiter_supports_legacy_sent_name(native_mail, monkeypatch):
    workspace, host = native_mail
    actual = 'INBOX\\Sent Items'
    host.boxes = {'INBOX': box(), actual: box()}
    def listing(self, *args):
        return 'OK', [b'() ' + quoted(b'\\') + b' ' + quoted(modified_utf7(name)) for name in self.host.boxes]
    monkeypatch.setattr(MailboxClient, 'list', listing)
    assert call(workspace, 'mail_search', folder='Sent')['messages'][0]['id'] == message_id(actual)
    assert host.clients[-1].selected == actual


def test_legacy_inbox_ids_remain_readable_without_folder(native_mail):
    workspace, host = native_mail
    result = call(workspace, 'mail_read', messageId='7:42')
    assert 'Hello мир!' in result['text']
    assert host.clients[-1].selected == 'INBOX'


def legacy_cursor(query='', before_uid=43):
    fingerprint = hashlib.sha256(json.dumps([query, None, None], ensure_ascii=False).encode()).hexdigest()
    payload = {'v': 1, 'validity': '7', 'beforeUid': before_uid, 'hash': fingerprint, 'count': 3}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode().rstrip('=')


def test_v1_inbox_cursor_still_pages_the_old_mailbox(native_mail):
    workspace, host = native_mail
    host.boxes['INBOX']['uids'] = (43, 42, 41)
    result = call(workspace, 'mail_search', cursor=legacy_cursor(), limit=1)
    assert [item['id'] for item in result['messages']] == ['7:42']
    assert result['hasMore'] and result['nextCursor']
    assert_metadata_only(host.clients)


@pytest.mark.parametrize('change', [{'folder': 'Sent'}, {'hasAttachment': True}])
def test_v1_cursor_cannot_be_reused_for_another_folder_or_attachment_filter(native_mail, change):
    workspace, host = native_mail
    assert_rejected(workspace, 'mail_search', cursor=legacy_cursor(), limit=1, **change)
    assert all(not client.calls for client in host.clients)


@pytest.mark.parametrize('change', [{'folder': CUSTOM}, {'hasAttachment': True},
                                   {'query': 'different'}, {'since': '2026-10-02'},
                                   {'before': '2026-11-02'}])
def test_new_cursor_rejects_changed_mailbox_query_dates_or_filter_before_fetch(native_mail, change):
    workspace, host = native_mail
    args = {'folder': 'Sent', 'query': 'invoice', 'since': '2026-10-01',
            'before': '2026-11-01', 'limit': 1}
    first = call(workspace, 'mail_search', **args)
    assert first['hasMore'] and first['nextCursor']
    existing = len(host.clients)
    assert_rejected(workspace, 'mail_search', **{**args, **change, 'cursor': first['nextCursor']})
    assert all(not client.calls for client in host.clients[existing:])


def test_changed_uidvalidity_invalidates_cursor_and_message_before_fetch(native_mail):
    workspace, host = native_mail
    first = call(workspace, 'mail_search', folder='Sent', limit=1)
    host.boxes[SENT]['validity'] = '8'
    existing = len(host.clients)
    assert_rejected(workspace, 'mail_search', folder='Sent', limit=1, cursor=first['nextCursor'])
    assert_rejected(workspace, 'mail_read', messageId=first['messages'][0]['id'])
    assert all(not client.calls for client in host.clients[existing:])


def test_has_attachment_empty_page_keeps_cursor_and_finds_older_message_without_payload(native_mail):
    workspace, host = native_mail
    host.boxes[SENT] = box(flags=b'\\Sent', uids=range(1, 54), structure=text_structure())
    host.boxes[SENT]['by_uid'][50] = nested_structure()
    args = {'folder': 'Sent', 'hasAttachment': True, 'limit': 2}
    first = call(workspace, 'mail_search', **args)
    assert first['messages'] == []
    assert first['hasMore'] and first['nextCursor']
    assert first['totalMatches'] is None and first['scannedMessages'] <= 2
    second = call(workspace, 'mail_search', **args, cursor=first['nextCursor'])
    assert [item['id'] for item in second['messages']] == [message_id(SENT, 50)]
    assert second['hasMore'] and second['nextCursor'] != first['nextCursor']
    assert second['totalMatches'] is None and second['scannedMessages'] <= 2
    assert_metadata_only(host.clients)
    for client in host.clients:
        fetched = {int(uid) for command, uid, _, _ in client.calls if command.upper() == 'FETCH'}
        assert len(fetched) <= 2
        assert any('BODYSTRUCTURE' in str(query).upper() for _, _, query, _ in client.calls)


def test_has_attachment_uses_mime_parts_not_merely_multipart_or_text_content(native_mail):
    workspace, host = native_mail
    host.boxes[SENT] = box(flags=b'\\Sent', uids=(3, 2, 1), structure=text_structure())
    host.boxes[SENT]['by_uid'][2] = [text_structure(), text_structure(b'HTML'), b'ALTERNATIVE']
    attached_text = text_structure()
    attached_text[2] = [b'CHARSET', b'UTF-8', b'NAME', b'note.txt']
    host.boxes[SENT]['by_uid'][1] = attached_text
    result = call(workspace, 'mail_search', folder='Sent', hasAttachment=True)
    assert [item['id'] for item in result['messages']] == [message_id(SENT, 1)]
    assert result['hasMore'] is False and result['totalMatches'] is None
    assert_metadata_only(host.clients)


@pytest.fixture
def gmail(tmp_path):
    requests = []
    def run(home, program, request):
        assert program == 'sandbox_google' and request['method'] == 'GET'
        requests.append(copy.deepcopy(request))
        path = request['path']
        if path.split('?')[0] == 'labels':
            return {'labels': [{'id': 'Label_payments', 'name': 'Оплаты', 'type': 'user'}]}
        if path.startswith('messages?'):
            return {'messages': [{'id': 'gmail-one'}], 'resultSizeEstimate': 1}
        assert path.startswith('messages/gmail-one?')
        value = gmail_message()
        value['labelIds'] = ['SENT']
        return value
    return google_workspace(tmp_path, run), requests


@pytest.mark.parametrize('folder,label', [(None, 'INBOX'), ('INBOX', 'INBOX'),
                                         ('Sent', 'SENT'), ('Drafts', 'DRAFT'),
                                         ('Оплаты', 'Label_payments')])
def test_gmail_folder_scope_and_attachment_filter_use_labels_without_reading_bodies(gmail, folder, label):
    workspace, requests = gmail
    args = {'query': 'in:anywhere invoice', 'hasAttachment': True}
    if folder is not None:
        args['folder'] = folder
    result = call(workspace, 'mail_search', **args)
    listing = next(request for request in requests if request['path'].startswith('messages?'))
    params = parse_qs(urlsplit(listing['path']).query)
    assert params['labelIds'] == [label]
    assert 'has:attachment' in params['q'][0] and 'invoice' in params['q'][0]
    assert result['messages'][0]['id'] == 'gmail-one'
    assert all('format=full' not in request['path'] and '/attachments/' not in request['path']
               for request in requests)


def test_gmail_archive_is_an_explicit_non_inbox_view(gmail):
    workspace, requests = gmail
    call(workspace, 'mail_search', folder='Archive')
    listing = next(request for request in requests if request['path'].startswith('messages?'))
    params = parse_qs(urlsplit(listing['path']).query)
    assert 'INBOX' not in params.get('labelIds', [])
    assert all(term in params['q'][0] for term in ('-in:inbox', '-in:spam', '-in:trash'))


def test_gmail_sent_id_reads_original_text_and_attachment_metadata_without_folder(gmail):
    workspace, requests = gmail
    identity = call(workspace, 'mail_search', folder='Sent')['messages'][0]['id']
    requests.clear()
    result = call(workspace, 'mail_read', messageId=identity)
    assert result['text'] == 'Synthetic text'
    assert result['attachments'][0]['name'] == 'huge.pdf'
    assert len(requests) == 1 and requests[0]['path'].endswith('?format=full')
    assert '/attachments/' not in requests[0]['path']
