"""Folder discovery through native MCP and an encrypted store; no provider/network I/O."""

import copy
import base64
import io
import json

import pytest

from hermes_runtime.workspace import mcp, sandbox_mail as mail
from hermes_runtime.workspace.service import Workspace
from hermes_runtime.workspace.store import WorkspaceError
from tests.hermes_runtime.test_workspace_gmail import workspace as google_workspace
from tests.hermes_runtime.test_workspace_mail_folders import (
    CUSTOM, SENT, MailboxClient, MailboxHost, box, call, modified_utf7, quoted, reply,
)
from tests.hermes_runtime.test_workspace_native import ENV, ID, SECRET, connection
from tests.hermes_runtime.test_workspace_mail_sections import PDF


@pytest.fixture
def listing(tmp_path, monkeypatch):
    host = MailboxHost()
    host.rows = [
        b'(\\HasNoChildren) "/" INBOX',
        b'(\\sEnT) "/" ' + quoted(modified_utf7(SENT)),
        b'() "/" ' + quoted(modified_utf7(CUSTOM)),
        b'(\\NoSelect \\HasChildren) "/" ' + quoted(modified_utf7('Проекты')),
        b'(\\NonExistent) "/" "Removed"',
        b'(\\Archive) NIL "Archive"',
        b'() "&" ' + quoted(modified_utf7('Projects&Payments')),
        b'() "\\\\" "Projects\\\\Payments"',
        b'() NIL ' + quoted(modified_utf7('Счета/e\u0301/😀')),
    ]
    literal = modified_utf7('Почта/Черновики')
    host.rows += [(b'(\\Drafts) "/" {' + str(len(literal)).encode() + b'}', literal), b'', None]
    host.failure = None

    class Client(MailboxClient):
        def list(self, directory='""', pattern='*'):
            self.list_calls.append((directory, pattern))
            if host.failure:
                raise host.failure
            return 'OK', host.rows

    def connect(*args, **kwargs):
        client = Client(host)
        host.clients.append(client)
        return client

    monkeypatch.setattr(mail, 'ImapTLS', connect)
    requests = []
    def run(home, program, request):
        assert program == 'sandbox_mail'
        requests.append(copy.deepcopy(request))
        try:
            return mail.execute(request)
        except mail.MailError as error:
            raise WorkspaceError(str(error)) from None

    (tmp_path / 'config.yaml').write_text(f'autonomous:\n  workspace:\n    native: true\n    environment_id: {ENV}\n')
    workspace = Workspace(tmp_path, runner=run)
    workspace.store.upsert(connection(services=['mailRead']), SECRET)
    return workspace, host, requests


def test_listing_returns_exact_decoded_paths_roles_selectability_and_delimiters(listing):
    workspace, host, requests = listing
    result = call(workspace, 'mail_folders')
    folders = {row['name']: row for row in result['folders']}
    assert folders['INBOX'] == {'name': 'INBOX', 'role': 'inbox', 'selectable': True, 'delimiter': '/'}
    assert folders[SENT] == {'name': SENT, 'role': 'sent', 'selectable': True, 'delimiter': '/'}
    assert folders[CUSTOM]['selectable'] is True and folders[CUSTOM]['role'] is None
    assert folders['Проекты']['selectable'] is False
    assert folders['Removed']['selectable'] is False
    assert folders['Archive']['delimiter'] is None and folders['Archive']['role'] == 'archive'
    assert folders['Projects&Payments']['delimiter'] == '&'
    assert folders['Projects\\Payments']['delimiter'] == '\\'
    assert folders['Почта/Черновики']['role'] == 'drafts'
    assert 'Счета/e\u0301/😀' in folders, 'Discovery must preserve Unicode, including normalization form'
    assert len(folders) == 10
    assert host.clients[0].list_calls == [('""', '*')]
    assert not host.clients[0].selections and not host.clients[0].calls
    assert requests[0]['operation'] == 'folders'
    serialized = json.dumps(result, ensure_ascii=False)
    assert SECRET['password'] not in serialized and SECRET['username'] not in serialized
    assert 'credential' not in serialized and 'address' not in serialized
    # The returned name is directly usable by the existing search contract.
    searched = call(workspace, 'mail_search', folder=folders[CUSTOM]['name'], limit=1)
    assert searched['folder'] == CUSTOM
    assert host.clients[-1].selections == [(quoted(modified_utf7(CUSTOM)), True)]


@pytest.mark.parametrize('change,expected', [({'services': ['mailSend']}, 'FORBIDDEN'), ({'enabled': False}, 'NOT_FOUND')])
def test_folder_discovery_reuses_mail_read_and_enabled_connection_guards(listing, change, expected):
    workspace, host, requests = listing
    workspace.store.update(ID, change)
    result = reply(workspace, 'mail_folders')
    assert result['isError'] and result['content'][0]['text'] == expected
    assert not requests and not host.clients


def test_other_profile_cannot_discover_another_employees_folders(listing, tmp_path):
    workspace, host, requests = listing
    other = Workspace(tmp_path / 'other', runner=workspace.runner)
    result = reply(other, 'mail_folders')
    assert result['isError'] and result['content'][0]['text'] == 'NOT_FOUND'
    assert not requests and not host.clients


def test_sandbox_folder_discovery_enforces_mail_read_before_connect(listing):
    _workspace, host, _requests = listing
    with pytest.raises(mail.MailError, match='^FORBIDDEN$'):
        mail.execute({'operation': 'folders', 'services': ['mailSend'], 'credential': SECRET})
    assert not host.clients


@pytest.mark.parametrize('rows', [[], [None, b'']])
def test_empty_folder_list_is_not_replaced_by_guessed_names(listing, rows):
    workspace, host, _requests = listing
    host.rows = rows
    assert call(workspace, 'mail_folders') == {'folders': []}


def test_exact_listing_name_roundtrips_search_read_and_attachment_while_sent_alias_is_preserved(listing):
    workspace, host, _requests = listing
    host.rows.append(b'() "/" "Sent"')
    host.boxes['Sent'] = box(parts={'1.1': b'Literal Sent original', '2': PDF})
    host.boxes[SENT]['parts'] = {'1.1': b'Localized Sent original', '2': PDF}
    folders = {row['name']: row for row in call(workspace, 'mail_folders')['folders']}
    assert folders['Sent']['role'] is None and folders[SENT]['role'] == 'sent'
    alias = call(workspace, 'mail_search', folder='Sent', folderExact=False, limit=1)
    assert alias['folder'] == SENT
    exact = call(workspace, 'mail_search', folder=folders['Sent']['name'], folderExact=True, limit=1)
    assert exact['folder'] == 'Sent'
    identity = exact['messages'][0]['id']
    assert identity != alias['messages'][0]['id']
    read = call(workspace, 'mail_read', messageId=identity)
    assert read['text'].strip() == 'Literal Sent original'
    assert host.clients[-1].selected == 'Sent'
    attachment = call(workspace, 'mail_attachment', messageId=identity, attachmentId=read['attachments'][0]['id'])
    assert base64.b64decode(attachment['data']) == b'%PDF fixture-only attachment'
    assert host.clients[-1].selected == 'Sent'
    rejected = reply(workspace, 'mail_search', folder='Sent', folderExact=False, limit=1, cursor=exact['nextCursor'])
    assert rejected['isError'] is True, 'Cursor must remain bound to its resolved literal mailbox'


def test_inbox_role_is_case_insensitive_without_rewriting_listed_name(listing):
    workspace, host, _requests = listing
    host.rows = [b'() "/" inBoX']
    assert call(workspace, 'mail_folders') == {'folders': [
        {'name': 'inBoX', 'role': 'inbox', 'selectable': True, 'delimiter': '/'}]}
    assert call(workspace, 'mail_search', folder='inBoX', folderExact=True, limit=1)['messages'][0]['id'] == '7:42'


@pytest.mark.parametrize('value', [None, 'true', 1])
def test_invalid_exact_selector_is_refused_before_sandbox(listing, value):
    workspace, host, requests = listing
    assert reply(workspace, 'mail_search', folder='Sent', folderExact=value)['isError'] is True
    assert not requests and not host.clients


@pytest.mark.parametrize('row', [b'private malformed response', b'() "/" "unterminated', b'() "/" "&bad"'])
def test_malformed_list_is_an_explicit_safe_error(listing, row):
    workspace, host, _requests = listing
    host.rows = [row]
    result = reply(workspace, 'mail_folders')
    assert result['isError'] and result['content'][0]['text'] == 'INVALID_MAIL_IMAP_PROTOCOL_FAILED'
    assert not host.clients[0].selections


def test_list_timeout_is_not_retried_and_has_a_safe_error(listing):
    workspace, host, _requests = listing
    host.failure = TimeoutError('private provider diagnostic')
    result = reply(workspace, 'mail_folders')
    assert result['isError'] and result['content'][0]['text'] == 'INVALID_MAIL_IMAP_UNREACHABLE'
    assert len(host.clients) == 1 and host.clients[0].list_calls == [('""', '*')]


def test_gmail_folder_discovery_projects_only_existing_label_names(tmp_path):
    requests = []
    def run(home, program, request):
        assert program == 'sandbox_google' and request['service'] == 'gmail'
        assert request['method'] == 'GET' and request['path'] == 'labels'
        requests.append(request)
        return {'labels': [{'id': 'INBOX', 'name': 'INBOX', 'type': 'system'},
                           {'id': 'SENT', 'name': 'SENT', 'type': 'system'},
                           {'id': 'Label_1', 'name': 'Оплаты/Счета', 'type': 'user', 'messagesTotal': 123}]}
    workspace = google_workspace(tmp_path, run)
    result = call(workspace, 'mail_folders')
    assert result == {'folders': [
        {'name': 'INBOX', 'role': 'inbox', 'selectable': True, 'delimiter': None},
        {'name': 'SENT', 'role': 'sent', 'selectable': True, 'delimiter': None},
        {'name': 'Оплаты/Счета', 'role': None, 'selectable': True, 'delimiter': None}]}
    assert len(requests) == 1 and 'messagesTotal' not in str(result)


@pytest.mark.parametrize('folder,exact,label', [('Sent', False, 'SENT'), ('Sent', True, 'Label_sent'),
                                               ('Archive', False, None), ('Archive', True, 'Label_archive')])
def test_gmail_exact_names_disambiguate_reserved_aliases(tmp_path, folder, exact, label):
    from urllib.parse import parse_qs, urlsplit
    requests = []
    def run(home, program, request):
        assert program == 'sandbox_google' and request['method'] == 'GET'
        requests.append(request)
        if request['path'] == 'labels':
            return {'labels': [{'id': 'SENT', 'name': 'SENT'}, {'id': 'Label_sent', 'name': 'Sent'},
                               {'id': 'Label_archive', 'name': 'Archive'}]}
        assert request['path'].startswith('messages?')
        return {'messages': []}
    workspace = google_workspace(tmp_path, run)
    call(workspace, 'mail_search', folder=folder, folderExact=exact)
    params = parse_qs(urlsplit(requests[-1]['path']).query)
    assert params.get('labelIds') == ([label] if label else None)
    assert any(request['path'] == 'labels' for request in requests) is exact
    if folder == 'Archive' and not exact:
        assert '-in:inbox' in params['q'][0]


def test_first_mcp_discovery_advertises_folder_tool_and_rejects_secret_arguments(listing):
    workspace, host, requests = listing
    source = io.StringIO(''.join(json.dumps(frame) + '\n' for frame in [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'mail_folders', 'arguments': {'connectionId': ID, 'password': 'not-an-argument'}}},
    ]))
    destination = io.StringIO()
    mcp.serve(source, destination, workspace)
    frames = [json.loads(line) for line in destination.getvalue().splitlines()]
    definition = next(row for row in frames[1]['result']['tools'] if row['name'] == 'mail_folders')
    assert definition['inputSchema'] == {'type': 'object', 'properties': {'connectionId': {'type': 'string'}},
                                         'required': ['connectionId'], 'additionalProperties': False}
    assert definition['annotations']['readOnlyHint'] is True
    assert frames[2]['result']['isError'] is True
    assert not requests and not host.clients
