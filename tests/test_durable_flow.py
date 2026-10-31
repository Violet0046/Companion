"""No-network regressions: migration, interruption, idempotency and task recovery."""
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from test_chat_service import _make_ctx
from companion_ever.infrastructure.database.connection import init_db
from companion_ever.conversations.chat import MESSAGE_BREAK


def context(path):
    ctx = _make_ctx(str(path), check_same_thread=False)
    ctx.repos.user.create('alice')
    ctx.services.persona.create_persona('alice', 'one', '{}')
    ctx.cfg.idle_session_minutes = 0
    ctx.services.chat.idle_session_minutes = 0
    ctx.llm.last_reasoning = None
    return ctx


def migration(path):
    old = sqlite3.connect(path)
    old.executescript((Path(__file__).parent/'fixtures/legacy_schema_v0.sql').read_text(encoding='utf-8'))
    old.execute("INSERT INTO user(id) VALUES ('alice')")
    for mid, role in [(1, 'user'), (2, 'assistant'), (50, 'session_close')]:
        old.execute('INSERT INTO conversation_log(id,user_id,persona_id,turn_index,role,content) '
                    "VALUES (?,'alice','one',1,?,'text')", (mid, role))
    old.execute("INSERT INTO thinking_log(message_id,user_id,persona_id,turn_index,content) VALUES (2,'alice','one',1,'reason')")
    old.execute('DELETE FROM conversation_log WHERE id=50')
    old.commit()
    old.close()
    conn = init_db(path, check_same_thread=False)
    try:
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('SELECT message_id,content FROM thinking_log').fetchone()[:] == (2, 'reason')
        from companion_ever.conversations.repository import ConversationRepo
        mid = ConversationRepo(conn).log_turn(user_id='alice', persona_id='one', turn_index=1,
                                              role='assistant', content='second bubble')
        assert mid > 50
    finally:
        conn.close()
    conn = init_db(path)
    assert conn.execute('SELECT count(*) FROM conversation_log').fetchone()[0] == 3
    conn.close()
    assert len(list(path.parent.glob(path.name + '.bak-before-jobs-*'))) == 1


def receipt_and_memory(path):
    ctx = context(path)
    try:
        chat = ctx.services.chat
        g, created = chat.receive('alice', 'one', 'hello', 'request1')
        assert created and ctx.services.conversation.count('alice', 'one') == 1
        assert chat.receive('alice', 'one', 'hello', 'request1')[0]['id'] == g['id']
        ctx.llm.chat.side_effect = TimeoutError('fake timeout')
        try:
            list(chat.reply_events(g, stream=False))
        except TimeoutError:
            pass
        assert chat.generations.get(g['id'])['status'] == 'failed'
        assert ctx.services.conversation.count('alice', 'one') == 1
        ctx.llm.chat.side_effect = None
        ctx.llm.chat.return_value = 'one' + MESSAGE_BREAK + 'two'
        assert chat.chat('alice', 'one', 'next', 'request2') == 'one\n\ntwo'
        count = ctx.llm.chat.call_count
        assert chat.chat('alice', 'one', 'next', 'request2') == 'one\n\ntwo'
        assert ctx.llm.chat.call_count == count
        sent = ctx.llm.chat.call_args[0][0]
        # 本轮输入只在 system 的「当前这一轮」里出现一次，不再作为独立消息
        assert sent[0]['content'].count('用户本轮说的话**：next') == 1, (
            "本轮输入应当只被送一次"
        )
        assert not any(m['content'] == 'next' for m in sent[1:]), (
            "本轮输入不应再作为独立消息出现"
        )
        for size in (1, 2, 7):
            raw = 'split one\nsame bubble<message_break>split two\n\nsame second'
            ctx.llm.stream_chat = lambda messages, size=size: iter(raw[i:i+size] for i in range(0,len(raw),size))
            generation, _ = chat.receive('alice','one',f'chunk-size-{size}')
            events = list(chat.reply_events(generation))
            assert [e['message']['content'] for e in events if e['type']=='message_done'] == ['split one\nsame bubble','split two\n\nsame second']
            assert '<message_break>' not in ''.join(e['content'] for e in events if e['type']=='delta')
        ctx.llm.stream_chat = lambda messages: iter(['line one\n', 'line two\n\nparagraph'])
        generation, _ = chat.receive('alice', 'one', 'ordinary newlines')
        events = list(chat.reply_events(generation))
        assert [e['message']['content'] for e in events if e['type']=='message_done'] == ['line one\nline two\n\nparagraph']
        svc = ctx.services.extraction
        job = svc.jobs.enqueue('alice', 'one')
        ctx.services.conversation.log_turn(user_id='alice',persona_id='one',turn_index=100,
                                           role='user',content='arrived after snapshot')
        assert svc.jobs.enqueue('alice','one')['through_message_id'] == job['through_message_id']
        ctx.llm.chat.return_value = json.dumps({'facts': [
            {'type': 'event', 'content': 'should roll back', 'source_quote': 'hello', 'source_message_ids': [1]},
            {'type': 'event', 'content': ''}], 'blocks': []})
        out = svc.run({'user_id': 'alice', 'persona_id': 'one'})
        assert out['job_status'] == 'failed' and out['facts_created'] == 0
        assert ctx.conn.execute("SELECT count(*) FROM note WHERE content='should roll back'").fetchone()[0] == 0
        assert ctx.conn.execute('SELECT count(*) FROM note_evidence').fetchone()[0] == 0
        assert svc.jobs.enqueue('alice', 'one')['id'] == job['id']
        svc.jobs.retry(job['id'])
        first = svc.jobs.claim(job['id'])
        ctx.conn.execute('UPDATE memory_job SET lease_expires_at=0 WHERE id=?', (job['id'],))
        conn = init_db(path, check_same_thread=False)
        from companion_ever.memory.extraction.jobs import MemoryJobRepo
        try:
            recovered = MemoryJobRepo(conn).claim(job['id'])
            assert recovered['through_message_id'] == job['through_message_id']
            assert recovered['lease_token'] != first['lease_token']
            try:
                svc.jobs.succeed(job['id'], first['lease_token'])
                raise AssertionError('stale worker committed')
            except RuntimeError:
                pass
        finally:
            conn.close()
    finally:
        ctx.conn.close()


def interruption(path):
    from fastapi.testclient import TestClient
    from companion_ever.entrypoints.web.server import create_app
    from companion_ever.config import WebConfig
    ctx = context(path)
    release = threading.Event()
    exited = threading.Event()
    def stream(messages):
        # 本轮输入不在 messages 里，由 system 的「当前这一轮」承载
        if '用户本轮说的话**：A' in messages[0]['content']:
            try:
                yield 'kept' + MESSAGE_BREAK + 'unfinished'
                release.wait(5)
                yield 'late old output'
            finally:
                exited.set()
        else:
            assert any(m['role'] == 'user' and m['content'].endswith(' A') for m in messages)
            yield 'new one' + MESSAGE_BREAK + 'new two'
    ctx.llm.stream_chat = stream
    try:
        with TestClient(create_app(ctx, WebConfig())) as client:
            with client.websocket_connect('/ws/chat?user=alice&persona=one') as ws:
                assert ws.receive_json()['type'] == 'ready'
                ws.send_json({'type': 'chat', 'content': 'A', 'client_message_id': 'a'})
                first = None
                while True:
                    event = ws.receive_json()
                    assert event['type'] != 'error', event
                    if event['type'] == 'received':
                        first = event['generation_id']
                    if event['type'] == 'message_done':
                        break
                ws.send_json({'type': 'chat', 'content': 'B', 'client_message_id': 'b'})
                second = None
                while True:
                    event = ws.receive_json()
                    assert event['type'] != 'error', event
                    if event['type'] == 'received':
                        second = event['generation_id']
                        release.set()
                    if event['type'] == 'done' and event['generation_id'] == second:
                        break
                assert ctx.services.chat.generations.get(first)['status'] == 'cancelled'
            rows = ctx.conn.execute('SELECT content FROM conversation_log ORDER BY id').fetchall()
            assert [r[0] for r in rows] == ['A', 'kept', 'B', 'new one', 'new two']
            result = client.get(f'/api/persona/alice/one/generations/{second}')
            assert result.status_code == 200 and len(result.json()['messages']) == 2
    finally:
        release.set()
        exited.wait(3)
        ctx.conn.close()


def backup(path):
    from types import SimpleNamespace
    from companion_ever.entrypoints.cli.maintain_cmd import cmd_backup
    # A deliberately old schema must be copied as-is, including committed WAL
    # pages, without initializing services or upgrading the source database.
    conn=sqlite3.connect(path)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('CREATE TABLE sample(value TEXT)')
        conn.execute("INSERT INTO sample VALUES ('committed in WAL')")
        conn.commit()
        assert Path(str(path)+'-wal').stat().st_size>0
        assert cmd_backup(SimpleNamespace(db=path))==0
        copies=list(path.parent.glob(path.name+'.bak-*'))
        assert len(copies)==1
        saved=sqlite3.connect(copies[0])
        try:
            assert saved.execute('SELECT value FROM sample').fetchone()[0]=='committed in WAL'
            assert saved.execute('PRAGMA user_version').fetchone()[0]==0
            assert saved.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        finally:
            saved.close()
        assert conn.execute('PRAGMA user_version').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='note'").fetchone()[0]==0
    finally:
        conn.close()
    missing=path.with_name('missing.db')
    assert cmd_backup(SimpleNamespace(db=missing))==1 and not missing.exists()


def main():
    with tempfile.TemporaryDirectory() as directory:
        for name, test in [('migration', migration), ('receipt', receipt_and_memory), ('interrupt', interruption), ('backup',backup)]:
            test(Path(directory) / (name + '.db'))
            print('[OK] ' + name, flush=True)


if __name__ == '__main__':
    main()
