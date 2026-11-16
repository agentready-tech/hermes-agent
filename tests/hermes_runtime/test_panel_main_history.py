"""The managed Panel pages its durable display history independently of model context."""
import json

import pytest

from hermes_runtime.adapters.main_history import PAGE_BYTES, history_page
from hermes_runtime.sessions import HOME_SESSION_ID as MAIN
from tests.hermes_runtime.test_panel_main_bootstrap import runtime

pytest_plugins = ['tests.hermes_runtime.test_main_schedule_tools']


@pytest.mark.asyncio
async def test_panel_reads_compacted_history_across_tool_only_page(main_home, monkeypatch):
    from hermes_cli.gateway_client import GatewayClient, GatewayRPCError
    from hermes_runtime.standalone.panel_chat import MainChat
    from tui_gateway.contracts.canonical_sessions import CanonicalSnapshot, PanelHistoryResult
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    home, db = main_home
    db.create_session('unrelated', source='delegate', parent_session_id=MAIN)
    db.append_message('unrelated', 'assistant', 'Another conversation')
    db.append_message(MAIN, 'user', 'Previously archived question')
    db.append_message(MAIN, 'assistant', 'Previously archived answer')
    activity = []
    for i in range(44):
        call = str(i)
        activity.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': call, 'function': {'name': 'read_file', 'arguments': json.dumps({'path': call})}}]},
            {'role': 'tool', 'tool_call_id': call, 'content': 'a result'},
        ])
    db.append_messages_batch(MAIN, activity)
    db.archive_and_compact(MAIN, [{'role': 'user', 'content': 'Rewound'}, {'role': 'assistant', 'content': 'Also rewound'}])
    db.rewind_to_message(MAIN, db.get_messages(MAIN)[0]['id'])
    for i in range(40):
        db.append_message(MAIN, 'user' if i % 2 == 0 else 'assistant', f'Live turn {i}')
    _authority, owner = runtime(home, db, monkeypatch)

    async def native(socket):
        async for frame in socket:
            request = json.loads(frame)
            response = await owner.dispatch(request)
            if 'result' in response:
                # The generated typed Gateway wire must accept and retain the cursor and ordinals.
                contract = CanonicalSnapshot if request['method'] == 'session.resume' else PanelHistoryResult
                typed = contract.model_validate(response['result'])
                assert typed.history is not None
                assert all(row.history_index is not None for row in typed.messages)
            await socket.send(json.dumps(response))

    async with serve(native, '127.0.0.1', 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        async with connect(f'ws://127.0.0.1:{port}') as socket, GatewayClient(socket) as gateway:
            with pytest.raises(GatewayRPCError, match='permission_denied'):
                await gateway.rpc('panel.history.before', session_id=MAIN)
            chat = MainChat(gateway)
            snapshot = await chat.dispatch({'method': 'session.resume', 'params': {'history_native': True}})
            assert [row['text'] for row in snapshot['messages']] == [f'Live turn {i}' for i in range(40)]
            before = snapshot['history']['before']
            assert snapshot['history']['native'] and snapshot['history']['has_more']
            assert snapshot['history']['total'] == snapshot['messages'][-1]['history_index'] + 1
            append_id = db.append_message(MAIN, 'user', 'Arrived during pagination')
            page = await chat.dispatch({'method': 'panel.history.before',
                                        'params': {'session_id': MAIN, 'before': before}})
            assert page['history']['total'] == append_id + 1
            assert len(page['messages']) > 40
            assert all(row['history_index'] < before for row in page['messages'])
            assert 'Previously archived answer' in {row.get('text') for row in page['messages']}
            assert page['messages'][-1]['args'] == {'path': '43'}
            older = await gateway.rpc('panel.history.before', session_id=MAIN, before=page['history']['before'])
            assert older['history'] == {'native': True, 'total': append_id + 1, 'before': 0, 'has_more': False}
            assert 'Previously archived question' in {row.get('text') for row in older['messages']}
            wire = json.dumps([snapshot, page, older])
            assert all(value not in wire for value in ('Another conversation', 'Rewound', 'Also rewound'))
            with pytest.raises(GatewayRPCError, match='permission_denied'):
                await gateway.rpc('panel.history.before', session_id='unrelated')
    assert len(db.get_messages(MAIN)) == 41


def test_archive_page_measures_encoded_budget_and_makes_progress(main_home):
    home, db = main_home
    for i in range(9):
        db.append_message(MAIN, 'assistant', f'{i} ' + '\U0001f310' * 16_000)
    page = history_page(db, MAIN, home)
    assert 0 < page['history']['before'] < page['history']['total']
    assert page['messages'] and '8 ' in page['messages'][-1]['text'][:2]
    assert len(json.dumps(page).encode()) < PAGE_BYTES + 1024
    older = history_page(db, MAIN, home, before=page['history']['before'])
    assert older['messages'] and older['history']['before'] < page['history']['before']
