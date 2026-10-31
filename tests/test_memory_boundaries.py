"""记忆模块边界、诊断查询隔离和原有 HTTP 维护接口的兼容性。"""
import ast
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_persona_service import context, expect_error
from fake_memory import no_memory
from companion_ever.entrypoints.web.server import create_app
from companion_ever.config import WebConfig
from companion_ever.domain.enums import MemoryKind


def module_boundaries():
    root = Path(__file__).resolve().parents[1] / 'src' / 'companion_ever'
    forbidden = ('companion_ever.bootstrap.app', 'companion_ever.entrypoints.web', 'evaluation', 'fastapi', 'starlette')
    for file in (root / 'memory').rglob('*.py'):
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            imports = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(forbidden) for name in imports), file
    # These entrypoints orchestrate; repositories and writers own SQL.
    for name in ('memory/notes/service.py', 'memory/retrieval/service.py',
                 'memory/extraction/service.py', 'memory/extraction/sessions.py',
                 'memory/summaries/service.py', 'memory/scheduling.py',
                 'entrypoints/web/routes/memory.py'):
        tree = ast.parse((root / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('execute', 'executemany', 'executescript') and node.args:
                    sql = node.args[0]
                    assert not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)
                                and sql.value.lstrip().upper().startswith(('SELECT ', 'UPDATE ', 'INSERT ', 'DELETE '))), name
    scheduler = (root / 'memory/scheduling.py').read_text(encoding='utf-8')
    assert 'self.ctx' not in scheduler and 'AppContext' not in scheduler
    routes = (root / 'entrypoints/web/routes/memory.py').read_text(encoding='utf-8')
    assert 'SummaryService(' not in routes and 'ctx.conn' not in routes and 'ctx.repos' not in routes


def queries_retry_and_routes(path):
    ctx = context(path)
    sessions = ctx.services.session_close
    summaries = ctx.services.summary
    try:
        ctx.services.persona.create_persona('alice', 'two', '{}')
        ctx.services.conversation.log_turn(user_id='alice', persona_id='one', turn_index=1,
                                           role='user', content='你好')
        job = sessions.jobs.enqueue('alice', 'one')
        assert sessions.recent_jobs('alice', 'one')[0]['id'] == job['id']
        assert sessions.recent_jobs('alice', 'two') == []
        expect_error(LookupError, lambda: sessions.retry_job('alice', 'two', job['id']))
        expect_error(ValueError, lambda: sessions.retry_job('alice', 'one', job['id']))
        claimed = sessions.jobs.claim(job['id'])
        sessions.jobs.fail(job['id'], claimed['lease_token'], 'fixture failure', retryable=False)
        assert len(sessions.task_attempts('alice', 'one', 'memory', job['id'])) == 1
        assert sessions.task_attempts('alice', 'two', 'memory', job['id']) == []

        one = ctx.services.note.create_note('alice', 'one', MemoryKind.FACT, '用户喜欢茶')
        two = ctx.services.note.create_note('alice', 'two', MemoryKind.FACT, '另一个角色的事实')
        changes = summaries.fact_changes('alice', 'one')
        assert len(changes) == 1 and changes[0]['note_id'] == one
        assert changes[0]['before'] is None and changes[0]['after']['content'] == '用户喜欢茶'
        assert summaries.fact_changes('alice', 'one', changes[0]['id']) == []
        assert summaries.fact_changes('alice', 'two')[0]['note_id'] == two
        summary_job = summaries.repo.enqueue('alice', 'one')
        assert summaries.recent_jobs('alice', 'one')[0]['id'] == summary_job['id']
        assert summaries.recent_jobs('alice', 'two') == []
        # Keep this queued summary out of the host's startup recovery while
        # inspecting read-only diagnostic routes.
        claimed_summary = summaries.repo.claim(summary_job['id'])
        summaries.repo.fail(claimed_summary, 'fixture failure', retryable=False)
        graph = summaries.graph
        ctx.llm.chat.side_effect = no_memory

        with TestClient(create_app(ctx, WebConfig(pin='1234'))) as client:
            base = '/api/persona/alice/one'
            assert client.get(base + '/memory-jobs').status_code == 401
            client.headers['X-Pin'] = '1234'
            assert client.get(base + '/memory-jobs').json()[0]['id'] == job['id']
            assert client.get(base + '/summary-jobs').json()[0]['id'] == summary_job['id']
            assert client.get(base + '/fact-changes').json()[0]['note_id'] == one
            assert client.get('/api/persona/alice/two/memory-jobs').json() == []
            assert client.post('/api/persona/alice/two/memory-jobs/' + job['id'] + '/retry').status_code == 404
            assert ctx.services.summary is summaries and ctx.workflows.summary is graph
            result = client.post(base + '/memory-jobs/' + job['id'] + '/retry')
            assert result.status_code == 200 and result.json() == {'job_id': job['id'], 'queued': True}, result.text
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (sessions.is_closing('alice', 'one') or
                                                    sessions.jobs.get(job['id'])['status'] != 'succeeded'):
                time.sleep(.01)
            assert sessions.jobs.get(job['id'])['status'] == 'succeeded'
            assert ctx.llm.chat.call_count == 1
            assert client.post(base + '/memory-jobs/' + job['id'] + '/retry').status_code == 409
            overview = client.get(base).json()
            assert overview['last_record_at'] and overview['memory_meta']['status'] == 'succeeded'
            assert ctx.services.summary is summaries and summaries.graph is graph
            ctx.services.persona.deactivate('alice', 'one')
            for endpoint in ('memory-jobs', 'summary-jobs', 'fact-changes', 'task-attempts', 'summary-status'):
                assert client.get(base + '/' + endpoint).status_code == 403, endpoint
    finally:
        ctx.services.extraction.runtime.shutdown()
        ctx.conn.close()


if __name__ == '__main__':
    module_boundaries()
    with tempfile.TemporaryDirectory(prefix='memory-boundaries-') as tmp:
        queries_retry_and_routes(Path(tmp) / 'memory.db')
    print('memory boundaries, scoped diagnostics and retry routes: PASS')
