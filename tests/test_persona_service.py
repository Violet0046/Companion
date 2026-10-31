"""角色边界、事务回滚、有效列表撤权及停用/恢复后的迟到结果保护。"""
import ast
import json
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from companion_ever.bootstrap.app import build_app
from companion_ever.config import AppConfig
from companion_ever.characters.repository import PersonaRepo
from companion_ever.domain.enums import BlockType, MemoryKind
from companion_ever.runtime.errors import LeaseLostError
from companion_ever.infrastructure.database.connection import _read_schema, apply_schema, connect
from companion_ever.runtime.repository import TaskRepo


def expect_error(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


def context(path):
    cfg = AppConfig(db_path=path, llm={'api_key': 'fake', 'model': 'fake'},
                    background={'enabled': False})
    model = MagicMock()
    model.last_reasoning = None
    with patch('companion_ever.bootstrap.app.create_llm_client', return_value=model):
        ctx = build_app(cfg)
    ctx.services.chat.idle_session_minutes = 0
    ctx.repos.user.create('alice')
    ctx.services.persona.create_persona('alice', 'one', '{"name":"小伴"}')
    return ctx


def creation_and_rollback(path):
    ctx = context(path)
    try:
        svc = ctx.services.persona
        assert svc.list_user_personas('alice') == ['one']
        assert len(ctx.repos.block.get_all('alice', 'one')) == 2
        assert not ctx.repos.note.find(user_id='alice', persona_id='one')
        expect_error(ValueError, lambda: svc.create_persona('alice', 'one', 'duplicate'))
        expect_error(ValueError, lambda: svc.create_persona('missing', 'one', '{}'))
        for persona in ('', '  ', 'one,two'):
            expect_error(ValueError, lambda: svc.create_persona('alice', persona, '{}'))
        with patch.object(ctx.repos.block, 'upsert', side_effect=RuntimeError('storage failure')):
            expect_error(RuntimeError, lambda: svc.create_persona('alice', 'rollback', '{}'))
        assert svc.list_user_personas('alice') == ['one']
        assert PersonaRepo(ctx.conn).get('alice', 'rollback') is None
        assert ctx.repos.block.get_all('alice', 'rollback') == []
        expect_error(PermissionError, lambda: svc.get_definition('bob', 'one'))
        expect_error(ValueError, lambda: svc.restore('bob', 'one'))
        # Role-list removal never falls back to a retained definition.
        ctx.repos.user.remove_persona('alice', 'one')
        expect_error(PermissionError, lambda: svc.get_definition('alice', 'one'))
        expect_error(ValueError, lambda: svc.create_persona('alice', 'one', 'overwrite'))
        svc.restore('alice', 'one')
        assert json.loads(svc.get_definition('alice', 'one')['content'])['name'] == '小伴'
    finally:
        ctx.conn.close()


def lifecycle_and_fences(path):
    ctx = context(path)
    try:
        svc = ctx.services.persona
        svc.create_persona('alice', 'two', '{}')
        ctx.repos.user.create('bob')
        svc.create_persona('bob', 'one', '{}')
        note = ctx.services.note.create_note('alice', 'one', MemoryKind.FACT, '用户喜欢散步')
        ctx.repos.block.upsert('alice', 'one', BlockType.USER_PROFILE, {'kept': ['原摘要']})
        before_blocks = ctx.services.summary.repo.blocks('alice', 'one')
        generation, _ = ctx.services.chat.receive('alice', 'one', '你好', 'first')
        assert ctx.services.chat.generations.claim(generation['id'])
        saved = ctx.services.chat.generations.save_message(generation['id'], 0, '已完成的一条回复')
        other, _ = ctx.services.chat.receive('alice', 'two', '你好', 'other')
        memory = ctx.services.extraction.jobs
        job = memory.claim(memory.enqueue('alice', 'one')['id'])
        summaries = ctx.services.summary.repo
        summary = summaries.claim(summaries.enqueue('alice', 'one')['id'])
        revision = svc.get_definition('alice', 'one')['revision']

        svc.deactivate('alice', 'one')
        assert svc.list_user_personas('alice') == ['two']
        assert svc.exists('bob', 'one')
        assert ctx.services.chat.generations.get(other['id'])['status'] == 'queued'
        for action in (
            lambda: svc.get_definition('alice', 'one'),
            lambda: svc.set_definition('alice', 'one', 'changed'),
            lambda: ctx.services.note.get_note(note, 'alice'),
            lambda: ctx.services.note.update_note(note, 'alice', content='changed'),
            lambda: ctx.services.note.archive_note(note, 'alice'),
            lambda: ctx.services.retrieval.retrieve('alice', 'one', '散步'),
            lambda: ctx.services.prompt.build('alice', 'one'),
            lambda: ctx.services.chat.receive('alice', 'one', 'new'),
        ):
            expect_error(PermissionError, action)
        assert ctx.llm.chat.call_count == 0
        assert ctx.services.chat.generations.save_message(generation['id'], 1, '迟到回复') is None
        assert ctx.services.chat.generations.get(generation['id'])['status'] == 'cancelled'
        expect_error(LeaseLostError, lambda: memory.require_owner(job['id'], job['lease_token']))
        expect_error(LeaseLostError, lambda: summaries.require_owner(summary))
        for kind, old in (('memory', job), ('summary', summary)):
            attempt = TaskRepo(ctx.conn).recent('alice', 'one', kind, old['id'])[0]
            assert attempt['status'] == 'failed' and attempt['failure_kind'] == 'permission'

        svc.restore('alice', 'one')
        assert svc.get_definition('alice', 'one')['revision'] > revision
        assert summaries.blocks('alice', 'one') == before_blocks
        assert ctx.repos.note.get(note).content == '用户喜欢散步'
        assert ctx.services.chat.generations.outputs(generation['id'])[0]['id'] == saved['id']
        assert not ctx.services.chat.generations.claim(generation['id'])
        fresh_job = memory.claim(job['id'])
        fresh_summary = summaries.claim(summary['id'])
        assert fresh_job['lease_token'] != job['lease_token']
        assert fresh_summary['lease_token'] != summary['lease_token']
        expect_error(LeaseLostError, lambda: memory.require_owner(job['id'], job['lease_token']))
        expect_error(LeaseLostError, lambda: summaries.require_owner(summary))
        memory.interrupt(job['id'], job['lease_token'], 'late interruption')
        summaries.interrupt(summary, 'late interruption')
        assert memory.get(job['id'])['lease_token'] == fresh_job['lease_token']
        assert summaries.get(summary['id'])['lease_token'] == fresh_summary['lease_token']
    finally:
        ctx.conn.close()


def deactivate_rollback(path):
    ctx = context(path)
    try:
        svc = ctx.services.persona
        generation, _ = ctx.services.chat.receive('alice', 'one', '你好')
        before = svc.get_definition('alice', 'one')
        def fail(user, persona):
            raise RuntimeError('invalidation failed')
        svc._on_deactivated = (*svc._on_deactivated, fail)
        expect_error(RuntimeError, lambda: svc.deactivate('alice', 'one'))
        assert svc.get_definition('alice', 'one') == before
        assert ctx.services.chat.generations.get(generation['id'])['status'] == 'queued'
    finally:
        ctx.conn.close()


def concurrent_creation(path):
    ctx = context(path)
    try:
        barrier = threading.Barrier(2)
        def create(persona):
            try:
                barrier.wait(timeout=5)
                ctx.services.persona.create_persona('alice', persona, '{}')
                return 'created'
            except ValueError:
                return 'duplicate'
            finally:
                ctx.conn.close_current()
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(create, ('left', 'right'))) == ['created', 'created']
        assert set(ctx.services.persona.list_user_personas('alice')) == {'one', 'left', 'right'}
        barrier.reset()
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(create, ('same', 'same'))) == ['created', 'duplicate']
        assert len(ctx.repos.block.get_all('alice', 'same')) == 2
        assert ctx.conn.execute("SELECT count(*) FROM persona_definition WHERE user_id='alice' AND persona_id='same'").fetchone()[0] == 1
    finally:
        ctx.conn.close()


def terminal_failures_preserved(path):
    ctx = context(path)
    try:
        ctx.services.chat.receive('alice', 'one', '你好')
        memory = ctx.services.extraction.jobs
        job = memory.claim(memory.enqueue('alice', 'one')['id'])
        memory.fail(job['id'], job['lease_token'], 'invalid draft', retryable=False)
        summaries = ctx.services.summary.repo
        summary = summaries.claim(summaries.enqueue('alice', 'one')['id'])
        summaries.fail(summary, 'invalid summary', retryable=False)
        before = (memory.get(job['id']), summaries.get(summary['id']))
        ctx.services.persona.deactivate('alice', 'one')
        ctx.services.persona.restore('alice', 'one')
        assert (memory.get(job['id']), summaries.get(summary['id'])) == before
    finally:
        ctx.conn.close()


def late_model_results(path):
    from test_kind_storage import draft, proposal
    from test_summary_evolution import empty, item
    ctx = context(path)
    try:
        svc = ctx.services.persona
        def cycle():
            svc.deactivate('alice', 'one')
            svc.restore('alice', 'one')

        # 手动编辑同样受 revision 保护：停用→恢复后再提交旧版本的编辑必须被拒绝
        stale = svc.get_definition('alice', 'one')['revision']
        cycle()
        expect_error(ValueError, lambda: svc.set_definition(
            'alice', 'one', '{"name":"迟到的编辑"}', expected_revision=stale))
        assert json.loads(svc.get_definition('alice', 'one')['content'])['name'] == '小伴'

        generation, _ = ctx.services.chat.receive('alice', 'one', '你好')
        def chat_reply(*args, **kwargs):
            cycle()
            return '迟到的聊天回复'
        ctx.llm.chat.side_effect = chat_reply
        list(ctx.services.chat.reply_events(generation, stream=False))
        assert ctx.services.chat.generations.outputs(generation['id']) == []

        mid = ctx.services.conversation.log_turn(user_id='alice', persona_id='one', turn_index=2,
                                               role='user', content='我喜欢散步')
        # Skip the earlier greeting, which has no memory to extract.
        cid = ctx.repos.conversation.ensure_conversation('alice', 'one')
        ctx.conn.execute('UPDATE conversation SET memory_start_message_id=? WHERE id=?', (mid - 1, cid))
        def memory_reply(*args, **kwargs):
            cycle()
            return json.dumps(draft([proposal(mid, '我喜欢散步')]), ensure_ascii=False)
        ctx.llm.chat.side_effect = memory_reply
        state = ctx.services.extraction.run({'user_id': 'alice', 'persona_id': 'one'})
        assert ctx.repos.note.find(user_id='alice', persona_id='one') == []
        ctx.llm.chat.side_effect = None
        ctx.llm.chat.return_value = json.dumps(draft([proposal(mid, '我喜欢散步')]), ensure_ascii=False)
        state = ctx.services.extraction.run({'user_id': 'alice', 'persona_id': 'one'})
        assert state['job_status'] == 'succeeded', state

        job = ctx.services.summary.request('alice', 'one', background=False)
        def summary_reply(messages, **kwargs):
            data = json.loads(messages[-1]['content'])
            document = empty()
            document['user_profile']['identity'] = [item(f) for f in data['current_facts']]
            return json.dumps(document, ensure_ascii=False)
        def late_summary_reply(messages, **kwargs):
            result = summary_reply(messages, **kwargs)
            cycle()
            return result
        before = ctx.services.summary.repo.blocks('alice', 'one')
        ctx.llm.chat.side_effect = late_summary_reply
        ctx.services.summary.run(job['job_id'])
        assert ctx.services.summary.repo.blocks('alice', 'one') == before
        ctx.llm.chat.side_effect = summary_reply
        result = ctx.services.summary.run(job['job_id'])
        assert result['status'] == 'succeeded', result
        assert ctx.services.summary.repo.blocks('alice', 'one') != before
    finally:
        ctx.conn.close()


def migration_constraints():
    unique = ('CREATE UNIQUE INDEX IF NOT EXISTS uq_persona_definition_active\n'
              ' ON persona_definition(user_id,persona_id) WHERE is_archived=0;')
    schema = _read_schema().replace(unique, '').replace(
        ' is_archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1\n',
        ' is_archived INTEGER NOT NULL DEFAULT 0\n')
    for duplicate in (False, True):
        conn = connect(':memory:')
        try:
            conn.executescript(schema + '\nPRAGMA user_version=5;')
            conn.execute("INSERT INTO user(id,persona_ids) VALUES ('alice','one')")
            conn.execute("INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES ('a','alice','one','original')")
            if duplicate:
                conn.execute("INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES ('b','alice','one','other')")
                expect_error(ValueError, lambda: apply_schema(conn))
                assert conn.execute('PRAGMA user_version').fetchone()[0] == 5
                assert conn.execute('SELECT count(*) FROM persona_definition').fetchone()[0] == 2
                assert 'revision' not in {r['name'] for r in conn.execute('PRAGMA table_info(persona_definition)')}
                conn.execute("UPDATE persona_definition SET is_archived=1 WHERE id='b'")
            apply_schema(conn)
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 6
            assert PersonaRepo(conn).get('alice', 'one')['content'] == 'original'
            assert PersonaRepo(conn).get('alice', 'one')['revision'] == 1
            expect_error(sqlite3.IntegrityError, lambda: conn.execute(
                "INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES ('c','alice','one','duplicate')"))
            conn.execute("INSERT INTO persona_definition(id,user_id,persona_id,content,is_archived) VALUES ('d','alice','one','archive',1)")
            apply_schema(conn)
            assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        finally:
            conn.close()


def module_boundary():
    root = Path(__file__).resolve().parents[1] / 'src' / 'companion_ever'
    forbidden = ('companion_ever.bootstrap', 'companion_ever.entrypoints', 'companion_ever.memory')
    for file in (root / 'characters').glob('*.py'):
        for node in ast.walk(ast.parse(file.read_text(encoding='utf-8'))):
            imports = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                       [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            assert not any(name.startswith(forbidden) for name in imports), file


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='character-tests-') as tmp:
        for case in (creation_and_rollback, lifecycle_and_fences, deactivate_rollback,
                     concurrent_creation, terminal_failures_preserved, late_model_results):
            case(Path(tmp) / (case.__name__ + '.db'))
            print(case.__name__ + ': PASS')
    migration_constraints()
    module_boundary()
    print('persona lifecycle, migration and module boundaries: PASS')
