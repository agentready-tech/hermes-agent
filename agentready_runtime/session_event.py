#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import time
import urllib.parse

import websockets

HOME_SESSION_ID = "agentready_home"
DASHBOARD_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
GATEWAY_URL = "ws://127.0.0.1:9119"
EVENT_CHANNEL = HOME_SESSION_ID


async def rpc(socket, method, params, request_id, events=None):
    await socket.send(
        json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
    )
    while True:
        frame = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
        if frame.get("id") != request_id:
            if events is not None and frame.get("method") == "event":
                events.append(frame)
            continue
        if frame.get("error"):
            raise RuntimeError(frame["error"].get("message", method + " failed"))
        return frame.get("result") or {}


from agentready_runtime.events import event_prompt


async def ask_agent(prompt, *, source, event_id, metadata=None, timeout_seconds=1800):
    if not DASHBOARD_TOKEN:
        raise RuntimeError("HERMES_DASHBOARD_SESSION_TOKEN is required")
    deadline = time.time() + timeout_seconds
    last_error = None
    socket = None
    while socket is None and time.time() < deadline:
        try:
            socket = await websockets.connect(
                GATEWAY_URL
                + "/api/ws?token="
                + urllib.parse.quote(DASHBOARD_TOKEN, safe=""),
                open_timeout=10,
                ping_interval=20,
                max_size=8 * 1024 * 1024,
            )
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as error:
            last_error = error
            await asyncio.sleep(2)
    if socket is None:
        raise RuntimeError(
            "Hermes was unavailable before event submission: {}".format(last_error)
        )

    publisher = None
    try:
        publisher = await websockets.connect(
            "ws://127.0.0.1:9119/api/pub?token={}&channel={}".format(
                urllib.parse.quote(DASHBOARD_TOKEN, safe=""),
                urllib.parse.quote(EVENT_CHANNEL, safe=""),
            ),
            open_timeout=10,
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        )
    except (OSError, asyncio.TimeoutError, websockets.WebSocketException):
        # Live Environment chat is an observer. A missing observer feed must
        # never prevent an email/schedule/channel event from being processed.
        publisher = None

    try:
        stamp = str(int(time.time() * 1000))
        opened = await rpc(
            socket,
            "session.resume",
            {
                "session_id": HOME_SESSION_ID,
                "close_on_disconnect": False,
                "source": "agentready",
                "cols": 100,
            },
            "resume-" + stamp,
        )
        session_id = opened.get("session_id") or opened.get("id")
        if not session_id:
            raise RuntimeError("Hermes did not resume the AgentReady home session")
        if publisher is not None:
            try:
                await publisher.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "agentready.event.received",
                                "session_id": session_id,
                                "payload": {
                                    "source": source,
                                    "event_id": event_id,
                                    "text": prompt,
                                },
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            except websockets.WebSocketException:
                publisher = None
        buffered_events = []
        submitted = await rpc(
            socket,
            "prompt.submit",
            {
                "session_id": session_id,
                "text": event_prompt(prompt, source, event_id, metadata),
            },
            "event-" + stamp,
            buffered_events,
        )
        awaiting_start = submitted.get("status") == "queued"
        accumulated = ""
        while time.time() < deadline:
            if buffered_events:
                frame = buffered_events.pop(0)
                raw_frame = json.dumps(frame)
            else:
                raw_frame = await asyncio.wait_for(
                    socket.recv(), timeout=max(1, deadline - time.time())
                )
                frame = json.loads(raw_frame)
            event = frame.get("params") if frame.get("method") == "event" else None
            if not event or (
                event.get("session_id") and event.get("session_id") != session_id
            ):
                continue
            if publisher is not None:
                try:
                    await publisher.send(raw_frame)
                except websockets.WebSocketException:
                    publisher = None
            payload = event.get("payload") or {}
            if awaiting_start:
                if event.get("type") != "message.start":
                    continue
                awaiting_start = False
            if event.get("type") == "message.delta":
                accumulated += payload.get("text", "")
            elif event.get("type") == "message.complete":
                if payload.get("status") == "error":
                    raise RuntimeError(
                        payload.get("message") or payload.get("text") or "Hermes failed"
                    )
                return (payload.get("text") or accumulated).strip()
            elif event.get("type") == "error":
                raise RuntimeError(payload.get("message") or "Hermes event failed")
    finally:
        await socket.close()
        if publisher is not None:
            await publisher.close()
    raise RuntimeError("Hermes event timed out")


async def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("event_id")
    parser.add_argument("prompt")
    args = parser.parse_args()
    answer = await ask_agent(args.prompt, source=args.source, event_id=args.event_id)
    if answer:
        print(answer)


if __name__ == "__main__":
    asyncio.run(cli())
