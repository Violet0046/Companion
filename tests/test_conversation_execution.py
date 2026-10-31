"""不启动 Web 的会话执行契约：订阅、防重、恢复、重试、取消和权限。"""
import ast
import tempfile
import threading
import time
from pathlib import Path

from test_persona_service import context, expect_error
from companion_ever.conversations.chat import MESSAGE_BREAK


def settled(ctx, gid, status):
    svc = ctx.services.generation
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = svc.state('alice', 'one', gid)
        with svc._lock:
            idle = gid not in svc._executions
        if current['status'] == status and idle:
            return svc.snapshot('alice', 'one', gid)
        time.sleep(.01)
    raise AssertionError(current)


def subscribers_and_scope(path):
    ctx = context(path)
    entered, release = threading.Event(), threading.Event()
    calls, seen = [], []
    def stream(messages):
        calls.append(messages)
        entered.set()
        yield '<thi'
        yield 'nk>private reasoning</think>visible'
        assert release.wait(5)
        yield ' end'
    def broken(event):
        if event['type'] == 'message_done':
            event['message']['content'] = 'corrupted by subscriber'
        raise ConnectionError('subscriber disconnected')
    ctx.llm.stream_chat = stream
    svc = ctx.services.generation
    try:
        generation, created = svc.submit('alice', 'one', 'hello', 'same-key', on_event=broken)
        assert created and entered.wait(5)
        duplicate, created = svc.submit('alice', 'one', 'hello', 'same-key', on_event=seen.append)
        assert not created and duplicate['id'] == generation['id']
        expect_error(ValueError, lambda: svc.submit('alice', 'one', 'different', 'same-key'))
        release.set()
        snapshot = settled(ctx, generation['id'], 'completed')
        assert len(calls) == 1
        assert ctx.services.chat.chat('alice', 'one', 'hello', 'same-key') == 'visible end'
        assert [m['content'] for m in snapshot['messages']] == ['visible end']
        assert 'private reasoning' in snapshot['messages'][0]['thinking']
        assert all('raw' not in e for e in seen)
        assert all('private reasoning' not in e['content'] for e in seen if e['type'] == 'delta')
        assert [e['message']['content'] for e in seen if e['type'] == 'message_done'] == ['visible end']
        assert seen[-1]['type'] == 'done'
        replay = []
        svc.submit('alice', 'one', 'hello', 'same-key', on_event=replay.append)
        settled(ctx, generation['id'], 'completed')
        assert [e['message']['id'] for e in replay if e['type'] == 'message_done'] == [snapshot['messages'][0]['id']]
        assert len(calls) == 1
        ctx.services.persona.create_persona('alice', 'two', '{}')
        expect_error(LookupError, lambda: svc.snapshot('alice', 'two', generation['id']))
        expect_error(LookupError, lambda: svc.retry('alice', 'two', generation['id']))
        expect_error(LookupError, lambda: svc.cancel('alice', 'two', generation['id']))
        expect_error(PermissionError, lambda: svc.latest('missing', 'one'))
    finally:
        release.set()
        ctx.conn.close()


def retry_and_preferences(path):
    ctx = context(path)
    calls, seen = [], []
    def stream(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise ConnectionError('upstream unavailable')
        yield '<think>secret</think>recovered'
    ctx.llm.stream_chat = stream
    ctx.services.settings.set_show_thinking('alice', 'one', False)
    svc = ctx.services.generation
    try:
        generation, _ = svc.submit('alice', 'one', 'keep input', 'retry-key')
        failed = settled(ctx, generation['id'], 'failed')
        assert failed['can_retry']
        retry = svc.retry('alice', 'one', generation['id'], on_event=seen.append)
        assert retry['id'] != generation['id']
        snapshot = settled(ctx, retry['id'], 'completed')
        assert [m['content'] for m in snapshot['messages']] == ['recovered']
        assert 'thinking' not in snapshot['messages'][0]
        assert all(e['type'] != 'thinking' for e in seen)
        assert all('thinking' not in e['message'] for e in seen if e['type'] == 'message_done')
        assert svc.retry('alice', 'one', generation['id'])['id'] == retry['id']
        assert len(calls) == 2
        assert ctx.conn.execute("SELECT count(*) FROM conversation_log WHERE role='user'").fetchone()[0] == 1
        assert svc.latest('alice', 'one')['id'] == retry['id']
        # Visibility changes delivery, never erases stored reasoning.
        ctx.services.settings.set_show_thinking('alice', 'one', True)
        assert svc.snapshot('alice', 'one', retry['id'])['messages'][0]['thinking'] == 'secret'
    finally:
        ctx.conn.close()


def recovery_and_queued_duplicate(path):
    ctx = context(path)
    calls = []
    def stream(messages):
        calls.append(messages)
        yield 'recovered queued receipt'
    ctx.llm.stream_chat = stream
    svc = ctx.services.generation
    try:
        # Simulate a receipt persisted before its caller could start execution.
        queued, _ = ctx.services.chat.receive('alice', 'one', 'lost caller', 'queued-key')
        duplicate, created = svc.submit('alice', 'one', 'lost caller', 'queued-key')
        assert not created and duplicate['id'] == queued['id']
        settled(ctx, queued['id'], 'completed')
        queued, _ = ctx.services.chat.receive('alice', 'one', 'restart', 'restart-key')
        svc.recover()
        settled(ctx, queued['id'], 'completed')
        interrupted, _ = ctx.services.chat.receive('alice', 'one', 'partial', 'partial-key')
        assert ctx.services.chat.generations.claim(interrupted['id'])
        saved = ctx.services.chat.generations.save_message(interrupted['id'], 0, 'already persisted')
        ctx.conn.execute('UPDATE generation SET lease_expires_at=0 WHERE id=?', (interrupted['id'],))
        svc.recover()
        failed = svc.snapshot('alice', 'one', interrupted['id'])
        assert failed['status'] == 'failed' and not failed['can_retry']
        assert failed['messages'][0]['id'] == saved['id']
        assert len(calls) == 2
        # Queued work with revoked access never calls the provider.
        revoked, _ = ctx.services.chat.receive('alice', 'one', 'disabled', 'disabled-key')
        ctx.repos.user.remove_persona('alice', 'one')
        svc.recover()
        assert ctx.services.chat.generations.get(revoked['id'])['status'] == 'failed'
        assert len(calls) == 2
    finally:
        ctx.conn.close()


def cancellation_retains_saved_messages(path):
    ctx = context(path)
    saved, release = threading.Event(), threading.Event()
    def stream(messages):
        yield 'kept' + MESSAGE_BREAK + 'unfinished'
        saved.set()
        assert release.wait(5)
        yield ' late tail'
    ctx.llm.stream_chat = stream
    svc = ctx.services.generation
    try:
        generation, _ = svc.submit('alice', 'one', 'question', 'cancel-key')
        assert saved.wait(5)
        cancelled = svc.cancel('alice', 'one', generation['id'])
        assert cancelled['status'] == 'cancelled'
        assert [m['content'] for m in cancelled['messages']] == ['kept']
        release.set()
        final = settled(ctx, generation['id'], 'cancelled')
        assert [m['content'] for m in final['messages']] == ['kept']
        message_id = final['messages'][0]['id']
        assert ctx.services.conversation.unread_count('alice', 'one') == {'count': 1, 'latest_id': message_id}
        assert ctx.services.conversation.unread_count('alice', 'one', message_id) == {'count': 0, 'latest_id': 0}
    finally:
        release.set()
        ctx.conn.close()


def boundary():
    root = Path(__file__).resolve().parents[1] / 'src' / 'companion_ever'
    for file in (root / 'conversations').glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            imports = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(('companion_ever.entrypoints.web', 'fastapi', 'starlette')) for name in imports), file
    # Routing and socket delivery must not own a second generation executor.
    routes = (root / 'entrypoints' / 'web' / 'routes' / 'chat.py').read_text(encoding='utf-8')
    assert 'threading.Thread(' not in routes
    assert '.chat.generations' not in routes


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='conversation-tests-') as tmp:
        for case in (subscribers_and_scope, retry_and_preferences, recovery_and_queued_duplicate,
                     cancellation_retains_saved_messages):
            case(Path(tmp) / (case.__name__ + '.db'))
            print(case.__name__ + ': PASS')
    boundary()
    print('conversation execution without Web: PASS')
