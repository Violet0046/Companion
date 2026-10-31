"""共同宿主的恢复、中断、重启和资源所有权；模型全部为本地替身。"""
import ast
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_persona_service import context, expect_error
from fake_memory import no_memory
from companion_ever.conversations.chat import MESSAGE_BREAK
from companion_ever.infrastructure.llm.openai_client import OpenAICompatibleClient
from companion_ever.infrastructure.llm.traced_client import TracedLLMClient
from companion_ever.runtime.tasks import TaskRuntime, TaskInterrupted


def wait_until(predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('lifecycle condition did not settle')


def queued_recovery(path):
    ctx = context(path)
    ctx.lifecycle.interval = .02
    calls = []
    def stream(messages):
        calls.append(messages)
        yield 'recovered'
    ctx.llm.stream_chat = stream
    try:
        generation, _ = ctx.services.chat.receive('alice', 'one', 'before startup', 'first')
        ctx.lifecycle.start()
        first_thread = ctx.lifecycle._thread
        ctx.lifecycle.start()
        assert ctx.lifecycle._thread is first_thread
        wait_until(lambda: ctx.services.generation.state('alice', 'one', generation['id'])['status'] == 'completed')
        ctx.lifecycle.stop()
        assert not first_thread.is_alive() and len(calls) == 1
        generation, _ = ctx.services.generation.submit('alice', 'one', 'while stopped', 'second')
        assert ctx.services.generation.state('alice', 'one', generation['id'])['status'] == 'queued'
        assert len(calls) == 1
        ctx.lifecycle.start()
        wait_until(lambda: ctx.services.generation.state('alice', 'one', generation['id'])['status'] == 'completed')
        assert len(calls) == 2
    finally:
        ctx.close()


def late_reply_after_restart(path):
    ctx = context(path)
    ctx.lifecycle.interval = .02
    release, entered, exited = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def stream(messages):
        calls.append(messages)
        # 本轮输入不在 messages 里，由 system 的「当前这一轮」承载
        if '用户本轮说的话**：old' in messages[0]['content']:
            try:
                yield 'kept' + MESSAGE_BREAK + 'draft'
                entered.set()
                assert release.wait(5)
                yield ' late'
            finally:
                exited.set()
        else:
            yield 'new reply'
    ctx.llm.stream_chat = stream
    try:
        ctx.lifecycle.start()
        generation, _ = ctx.services.generation.submit('alice', 'one', 'old', 'old')
        assert entered.wait(5)
        before = time.monotonic()
        ctx.lifecycle.stop()
        assert time.monotonic() - before < 2.5
        snapshot = ctx.services.generation.snapshot('alice', 'one', generation['id'])
        assert snapshot['status'] == 'failed' and not snapshot['can_retry']
        assert [m['content'] for m in snapshot['messages']] == ['kept']
        ctx.lifecycle.start()
        release.set()
        assert exited.wait(5)
        wait_until(lambda: generation['id'] not in ctx.services.generation._executions)
        assert ctx.services.generation.snapshot('alice', 'one', generation['id']) == snapshot
        next_generation, _ = ctx.services.generation.submit('alice', 'one', 'new', 'new')
        wait_until(lambda: ctx.services.generation.state('alice', 'one', next_generation['id'])['status'] == 'completed')
        assert len(calls) == 2
    finally:
        release.set()
        ctx.close()


def late_memory_after_restart(path):
    ctx = context(path)
    ctx.lifecycle.interval = .02
    entered, release = threading.Event(), threading.Event()
    calls = []
    lock = threading.Lock()
    def reply(messages, **kwargs):
        with lock:
            calls.append(messages)
            number = len(calls)
        if number == 1:
            entered.set()
            assert release.wait(5)
        return no_memory(messages)
    ctx.llm.chat.side_effect = reply
    ctx.services.conversation.log_turn(user_id='alice', persona_id='one', role='user', turn_index=1, content='hello')
    worker = threading.Thread(target=lambda: ctx.services.extraction.run({'user_id':'alice','persona_id':'one'}))
    try:
        worker.start()
        assert entered.wait(5)
        job = ctx.services.session_close.recent_jobs('alice', 'one')[0]
        old_token = ctx.services.extraction.jobs.get(job['id'])['lease_token']
        ctx.lifecycle.stop()
        assert ctx.services.extraction.jobs.get(job['id'])['status'] == 'queued'
        ctx.lifecycle.start()
        wait_until(lambda: ctx.services.extraction.jobs.get(job['id'])['status'] == 'succeeded')
        fresh = ctx.services.extraction.jobs.get(job['id'])
        assert fresh['lease_token'] != old_token
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert ctx.services.extraction.jobs.get(job['id']) == fresh
        assert len(calls) == 2
        attempts = ctx.services.session_close.task_attempts('alice','one','memory',job['id'])
        assert [a['status'] for a in attempts] == ['succeeded', 'interrupted']
    finally:
        release.set()
        worker.join(5)
        ctx.close()


def waiting_old_slot(path):
    ctx = context(path)
    task = TaskRuntime(ctx.conn)
    entered, release = threading.Event(), threading.Event()
    result = []
    class OldGate:
        def acquire(self, timeout):
            entered.set()
            assert release.wait(5)
            return False
    task._slot = OldGate()
    def wait():
        try:
            with task.slot():
                result.append('incorrectly resumed')
        except TaskInterrupted:
            result.append('interrupted')
    worker = threading.Thread(target=wait)
    try:
        worker.start()
        assert entered.wait(5)
        task.shutdown()
        task.start()
        release.set()
        worker.join(5)
        assert result == ['interrupted']
    finally:
        release.set()
        worker.join(5)
        ctx.close()


def resource_ownership(path):
    ctx = context(path)
    with patch.object(ctx.conn, 'close', wraps=ctx.conn.close) as close:
        ctx.close()
        ctx.close()
        assert close.call_count == 1
    sdk = MagicMock()
    native = OpenAICompatibleClient('fake', 'http://127.0.0.1:9/v1', 'fake')
    wrapped = TracedLLMClient(native)
    with patch('openai.OpenAI', return_value=sdk) as factory:
        assert native.client is sdk and native.client is sdk
        wrapped.close()
        wrapped.close()
        assert factory.call_count == 1 and sdk.close.call_count == 1
        expect_error(RuntimeError, lambda: native.client)


def dependency_direction():
    root = Path(__file__).resolve().parents[1] / 'src' / 'companion_ever'
    for folder in ('characters', 'conversations', 'memory', 'runtime', 'infrastructure'):
        forbidden = ['companion_ever.bootstrap', 'companion_ever.entrypoints', 'evaluation']
        if folder in ('runtime', 'infrastructure'):
            forbidden += ['companion_ever.characters', 'companion_ever.conversations', 'companion_ever.memory']
        for file in (root / folder).rglob('*.py'):
            for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
                imports = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                           [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                assert not any(name.startswith(tuple(forbidden)) for name in imports), file


if __name__ == '__main__':
    dependency_direction()
    with tempfile.TemporaryDirectory(prefix='application-host-tests-') as tmp:
        for case in (queued_recovery, late_reply_after_restart, late_memory_after_restart,
                     waiting_old_slot, resource_ownership):
            case(Path(tmp) / (case.__name__ + '.db'))
            print(case.__name__ + ': PASS')
