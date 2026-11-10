"""Panel's existing chat wire projected onto the gateway-owned main conversation.

No model, local writer or queue lives in this connector. Admissions, interruption,
prompt waiters and live results belong to the already running native gateway.
"""

import asyncio
import json

from hermes_runtime.sessions import HOME_SESSION_ID


def gateway_chat_available():
    from hermes_runtime.adapters.hermes import enabled
    from hermes_runtime.adapters.state import main_gateway_origin
    return enabled() and bool(main_gateway_origin())


class MainChat:
    def __init__(self, client):
        self.client = client
        self.session_id = None
        self.generation = None
        self.questions = {}

    def question(self, prompt):
        kind = prompt.get('kind')
        if kind not in {'approval', 'clarify'}:
            return None
        identity = f"panel-{kind}-{prompt['execution_generation']}-{prompt['prompt_id']}"
        self.questions[identity] = prompt
        params = {'session_id': HOME_SESSION_ID}
        if kind == 'approval':
            params.update({key: prompt[key] for key in ('command', 'description', 'choices') if key in prompt})
        else:
            params['questions'] = [{'qid': prompt['prompt_id'], 'question': prompt.get('question', ''),
                                    'choices': prompt.get('choices', []), 'multi_select': prompt.get('multi_select', False)}]
        return {'jsonrpc': '2.0', 'id': identity, 'method': kind, 'params': params}

    async def snapshot(self):
        result = await self.client.rpc('session.resume', session_id=HOME_SESSION_ID, display_projection=True)
        if result.get('session_id') != HOME_SESSION_ID:
            raise ValueError('Unexpected main conversation')
        self.session_id = HOME_SESSION_ID
        self.generation = result['execution_generation']
        requests = list(result.get('open_requests') or [])
        requests.extend(question for prompt in result.get('prompts', []) if (question := self.question(prompt)))
        from hermes_runtime.standalone.main_files import project_row
        rows = result.get('display_messages', result['messages'])
        return {**{key: value for key, value in result.items() if key != 'display_messages'},
                'messages': [project_row(row) for row in rows], 'open_requests': requests}

    async def dispatch(self, request):
        method, params = request.get('method'), request.get('params') or {}
        if not isinstance(params, dict):
            raise ValueError('Invalid chat params')
        if method == 'client.capabilities':
            return {'server_requests': True}
        if method in {'session.resume', 'session.history'}:
            if params.get('session_id', HOME_SESSION_ID) != HOME_SESSION_ID:
                raise ValueError('Only the main conversation is available')
            return await self.snapshot()
        if method in {'panel.subagents.list', 'panel.subagents.history'}:
            if not self.session_id:
                raise ValueError('Resume the main conversation first')
            return await self.client.rpc(method, **params)
        if not self.session_id or params.get('session_id') != self.session_id:
            raise ValueError('Resume the main conversation first')
        if method == 'prompt.submit':
            # The browser's stable nonce survives a lost ACK and is committed by Hermes.
            identity = params.get('submission_id')
            if not isinstance(identity, str) or not identity or len(identity) > 120:
                raise ValueError('A stable submission identity is required')
            allowed = {key: params[key] for key in ('text', 'attachments') if key in params}
            return await self.client.rpc(method, session_id=self.session_id, submission_id=identity,
                                         clarify_batch=True, **allowed)
        if method == 'session.interrupt':
            return await self.client.rpc(method, session_id=self.session_id, execution_generation=self.generation)
        if method == 'image.attach_bytes':
            return await self.client.rpc(method, **params)
        if method == 'panel.attachment.download':
            if set(params) != {'session_id', 'id'}:
                raise ValueError('Invalid attachment request')
            result = await self.client.rpc('session.resume', session_id=HOME_SESSION_ID, display_projection=True)
            if result.get('session_id') != self.session_id:
                raise ValueError('Unexpected main conversation')
            from hermes_runtime.adapters.hermes import managed_home
            from hermes_runtime.standalone.main_files import download
            return await asyncio.to_thread(download, result.get('display_messages', result['messages']),
                                           params['id'], managed_home())
        if method == 'file.attach':
            return await asyncio.to_thread(self._file, params)
        raise ValueError('Chat method is not permitted')

    def _file(self, params):
        # The native file stager is reused; a managed main route has no frozen local-session
        # policy. Its existing main workspace is the authorized staging context.
        from hermes_runtime.adapters.hermes import managed_home
        from tui_gateway import server
        if set(params) - {'session_id', 'name', 'data_url'}:
            raise ValueError('Invalid attachment')
        home = managed_home()
        session = {'cwd': str(home / 'workspace'), 'profile_home': str(home)}
        stored, uploaded = server._stage_session_file_attachment(session, raw_path='',
            data_url=params.get('data_url', ''), name=params.get('name', ''))
        ref = server._attachment_ref_path(session, stored)
        return {'attached': True, 'path': str(stored), 'name': stored.name, 'uploaded': uploaded,
                'ref_text': f'@file:{server._format_ref_value(ref)}'}

    async def answer(self, frame):
        prompt = self.questions.get(frame.get('id'))
        if prompt is None:
            # Native secret/sudo/vault/batched-clarify requests use their original id. The
            # answer goes only to the native waiter, never into a prompt, event or history.
            from gateway.session_sudo_prompt import is_response_frame
            if not is_response_frame(frame):
                raise ValueError('Unknown native request')
            await self.client.websocket.send(json.dumps(frame))
            return
        response = frame.get('result') or {}
        params = {'session_id': self.session_id, 'execution_generation': prompt['execution_generation'],
                  'prompt_id': prompt['prompt_id']}
        if prompt['kind'] == 'approval':
            params['choice'] = response.get('choice', 'deny')
        else:
            params['answer'] = (response.get('answers') or {}).get(prompt['prompt_id']) or ''
        await self.client.rpc(prompt['kind'] + '.respond', **params)
        self.questions.pop(frame['id'], None)

    def event(self, frame):
        if frame.get('method') != 'event':
            return frame
        params = frame.get('params') or {}
        if params.get('session_id') not in (None, HOME_SESSION_ID):
            return None
        if params.get('execution_generation') is not None:
            self.generation = params['execution_generation']
        kind = params.get('type')
        if kind in {'approval.request', 'clarify.request'}:
            return self.question(params['payload'])
        if kind in {'approval.settled', 'clarify.settled'}:
            prompt = params['payload']
            identity = f"panel-{kind.split('.')[0]}-{prompt['execution_generation']}-{prompt['prompt_id']}"
            self.questions.pop(identity, None)
            return {'jsonrpc': '2.0', 'method': 'event', 'params': {'type': 'request.cancel',
                    'session_id': HOME_SESSION_ID, 'payload': {'id': identity}}}
        if kind == 'message.complete' and isinstance(params.get('payload'), dict):
            payload = params['payload']
            if payload.get('attachments') or payload.get('attachment_error'):
                from gateway.platforms.base import BasePlatformAdapter
                payload = {**payload}
                for key in ('text', 'content'):
                    if isinstance(payload.get(key), str):
                        payload[key] = BasePlatformAdapter.strip_media_directives_for_display(payload[key])
                return {**frame, 'params': {**params, 'payload': payload}}
        return frame


async def bridge_chat(remote_url, credential, options):
    from hermes_cli.gateway_client import GatewayRPCError, connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    from hermes_runtime.adapters.hermes import managed_home
    from websockets.asyncio.client import connect
    discovered = await asyncio.to_thread(discover_gateway_endpoint, managed_home())
    if discovered.state != 'ready' or discovered.endpoint is None:
        raise ValueError('Main gateway is not ready')
    # Discover only: never start a second owner or fall back to an independent writer.
    async with connect_gateway(discovered.endpoint, max_size=32 * 1024 * 1024) as client, connect(
            remote_url, additional_headers={'Authorization': f'Bearer {credential}'}, **options) as remote:
        chat = MainChat(client)
        await remote.send(json.dumps({'jsonrpc': '2.0', 'method': 'event', 'params': {
            'type': 'gateway.ready', 'payload': {'main_queue': True, 'outgoing_files': 'chat-files-v1'}}}))

        async def requests():
            async for raw in remote:
                request = json.loads(raw)
                if not isinstance(request, dict):
                    raise ValueError('Invalid chat request')
                if not request.get('method'):
                    await chat.answer(request)
                    continue
                response = {'jsonrpc': '2.0', 'id': request.get('id')}
                try:
                    response['result'] = await chat.dispatch(request)
                except (GatewayRPCError, ValueError):
                    # No CLI diagnostics, internal ids, request text, paths or credentials.
                    response['error'] = {'code': 4090, 'message':
                        'Сообщение не отправлено. Проверьте подключение к сотруднику и повторите отправку.'}
                await remote.send(json.dumps(response))
                if request.get('method') == 'prompt.submit' and response.get('result', {}).get('status') == 'terminal':
                    # A lost-ACK retry can find a turn already complete. Repaint from the actual
                    # transcript instead of leaving this viewer's optimistic spinner running.
                    await remote.send(json.dumps({'jsonrpc': '2.0', 'method': 'event', 'params': {
                        'type': 'message.complete', 'session_id': HOME_SESSION_ID,
                        'payload': {'outcome': response['result'].get('outcome')}}}))

        async def events():
            while True:
                frame = await client.events.get()
                if isinstance(frame, Exception):
                    raise frame
                if (projected := chat.event(frame)) is not None:
                    await remote.send(json.dumps(projected))

        tasks = [asyncio.create_task(requests()), asyncio.create_task(events())]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
