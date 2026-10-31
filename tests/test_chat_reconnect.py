"""A socket is a subscriber, not the lifetime of a durable generation."""
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_durable_flow import context
from companion_ever.entrypoints.web.server import create_app
from companion_ever.config import WebConfig
from companion_ever.conversations.chat import MESSAGE_BREAK


def wait_status(client, gid, status):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = client.get(f'/api/persona/alice/one/generations/{gid}').json()
        if snapshot['status'] == status:
            return snapshot
        time.sleep(.01)
    raise AssertionError(snapshot)


def disconnected_reply(path):
    ctx = context(path)
    release = threading.Event()
    exited = threading.Event()
    calls = []
    def stream(messages):
        calls.append(messages)
        try:
            yield 'first' + MESSAGE_BREAK + 'par'
            assert release.wait(5)
            yield 'tial'
        finally:
            exited.set()
    ctx.llm.stream_chat = stream
    try:
        with TestClient(create_app(ctx, WebConfig())) as client:
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                ws.receive_json()
                ws.send_json({'type': 'chat', 'content': 'hello', 'client_message_id': 'offline'})
                gid = None
                while True:
                    event = ws.receive_json()
                    assert event['type'] != 'error', event
                    if event['type'] == 'received':
                        gid = event['generation_id']
                    if event['type'] == 'delta' and event['content'] == 'par':
                        break
            # Leaving the page must not cancel the worker.
            snapshot = client.get('/api/persona/alice/one/generation').json()
            assert snapshot['id'] == gid and snapshot['status'] == 'running', snapshot
            assert [m['content'] for m in snapshot['messages']] == ['first']
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                ws.receive_json()
                ws.send_json({'type': 'chat', 'content': 'hello', 'client_message_id': 'offline'})
                event = ws.receive_json()
                assert event['type'] == 'received' and event['duplicate'] and event['generation_id'] == gid
                release.set()
                snapshot = wait_status(client, gid, 'completed')
                assert [m['content'] for m in snapshot['messages']] == ['first', 'partial']
                assert all(m['created_at'].endswith('Z') for m in snapshot['messages'])
            assert len(calls) == 1
            ctx.services.persona.create_persona('alice', 'two', '{}')
            assert client.get('/api/persona/alice/two/generation').json() is None
            assert client.get(f'/api/persona/alice/two/generations/{gid}').status_code == 404
            assert client.get('/api/persona/missing/one/generation').status_code == 403
    finally:
        release.set()
        exited.wait(5)
        ctx.conn.close()


def disconnected_cancel(path):
    ctx = context(path)
    release = threading.Event()
    exited = threading.Event()
    def stream(messages):
        # 本轮输入不在 messages 里，由 system 的「当前这一轮」承载
        if '用户本轮说的话**：old' in messages[0]['content']:
            try:
                yield 'old draft'
                assert release.wait(5)
                yield 'stale tail'
            finally:
                exited.set()
        else:
            yield 'new reply'
    ctx.llm.stream_chat = stream
    try:
        with TestClient(create_app(ctx, WebConfig())) as client:
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                ws.receive_json()
                ws.send_json({'type': 'chat', 'content': 'old', 'client_message_id': 'old'})
                while True:
                    event = ws.receive_json()
                    if event['type'] == 'received':
                        old = event['generation_id']
                    if event['type'] == 'delta':
                        break
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                ws.receive_json()
                ws.send_json({'type': 'chat', 'content': 'new', 'client_message_id': 'new'})
                while True:
                    event = ws.receive_json()
                    assert event['type'] != 'error', event
                    if event['type'] == 'received':
                        release.set()
                    if event['type'] == 'done':
                        break
            assert exited.wait(5)
            assert wait_status(client, old, 'cancelled')['messages'] == []
            assert [r[0] for r in ctx.conn.execute('SELECT content FROM conversation_log ORDER BY id')] == ['old', 'new', 'new reply']
    finally:
        release.set()
        exited.wait(5)
        ctx.conn.close()


def failed_reply_retry(path):
    ctx=context(path)
    calls=[]
    def stream(messages):
        calls.append(messages)
        if len(calls)==1:
            raise ConnectionError('upstream unavailable')
        yield 'reply after retry'
    ctx.llm.stream_chat=stream
    try:
        with TestClient(create_app(ctx,WebConfig(pin='1234'))) as client:
            client.headers['X-Pin']='1234'
            response=client.post('/api/messages',json={'user_id':'alice','persona_id':'one','content':'keep my question','client_message_id':'retry-input'})
            assert response.status_code==200,response.text
            assert response.headers['cache-control']=='no-store'
            original=response.json()['generation_id']
            failed=wait_status(client,original,'failed')
            assert failed['can_retry'] and failed['messages']==[]
            retried=client.post(f'/api/persona/alice/one/generations/{original}/retry').json()
            gid=retried['id'];assert gid!=original
            done=wait_status(client,gid,'completed')
            assert [m['content'] for m in done['messages']]==['reply after retry']
            assert ctx.services.chat.generations.get(original)['status']=='failed'
            assert ctx.conn.execute("SELECT count(*) FROM conversation_log WHERE role='user'").fetchone()[0]==1
            assert len(calls)==2
            again=client.post(f'/api/persona/alice/one/generations/{original}/retry').json()
            assert again['id']==gid and len(calls)==2
            receipt=client.post('/api/messages',json={'user_id':'alice','persona_id':'one','content':'keep my question','client_message_id':'retry-input'}).json()
            assert receipt['generation_id']==gid and receipt['duplicate']
            history=client.get('/api/persona/alice/one/history').json()
            assert history[0]['client_message_id']=='retry-input'
            ctx.services.persona.create_persona('alice','two','{}')
            assert client.post(f'/api/persona/alice/two/generations/{original}/retry').status_code==404
    finally:
        ctx.conn.close()


def queued_receipt_recovery(path):
    ctx=context(path)
    ctx.llm.stream_chat=lambda messages:iter(['recovered queued reply'])
    g,_=ctx.services.chat.receive('alice','one','question before restart','restart-key')
    ctx.conn.execute("UPDATE generation SET created_at=datetime('now','-10 minutes') WHERE id=?",(g['id'],))
    try:
        with TestClient(create_app(ctx,WebConfig())) as client:
            result=wait_status(client,g['id'],'completed')
            assert result['messages'][0]['content']=='recovered queued reply'
    finally:
        ctx.conn.close()


def lost_receipt(path):
    from companion_ever.entrypoints.web.routes.chat import _Conn
    from fastapi import WebSocketDisconnect
    ctx = context(path)
    failed_send, release, exited = threading.Event(), threading.Event(), threading.Event()
    receipts = []
    original = _Conn.send
    async def send(conn, payload):
        if payload['type'] == 'received':
            receipts.append(payload)
            failed_send.set()
            raise WebSocketDisconnect()
        await original(conn, payload)
    def stream(messages):
        try:
            assert release.wait(5)
            yield 'saved despite lost acknowledgement'
        finally:
            exited.set()
    ctx.llm.stream_chat = stream
    try:
        with TestClient(create_app(ctx, WebConfig())) as client, patch.object(_Conn, 'send', send):
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                ws.receive_json()
                ws.send_json({'type':'chat','content':'hello','client_message_id':'lost-ack'})
                assert failed_send.wait(5)
            release.set()
            snapshot = wait_status(client, receipts[0]['generation_id'], 'completed')
            assert snapshot['messages'][0]['content'] == 'saved despite lost acknowledgement'
    finally:
        release.set()
        exited.wait(5)
        ctx.conn.close()


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as tmp:
        disconnected_reply(Path(tmp) / 'reply.db')
        disconnected_cancel(Path(tmp) / 'cancel.db')
        lost_receipt(Path(tmp) / 'lost-receipt.db')
        failed_reply_retry(Path(tmp) / 'retry.db')
        queued_receipt_recovery(Path(tmp) / 'queued.db')
    print('[OK] disconnect completion, reconnect deduplication, cancellation and scope isolation')
