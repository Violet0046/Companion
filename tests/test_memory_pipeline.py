"""记忆重构回归：幂等、失败重试、召回排序、FTS 标识。无需网络。"""
import json
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from test_chat_workflow import _make_ctx
from companion_ever.domain.enums import NoteType


def main():
    with tempfile.TemporaryDirectory() as directory:
        ctx = _make_ctx(str(Path(directory) / 'test.db'), check_same_thread=False)
        try:
            ctx.repos.user.create('alice')
            for persona in ('one', 'two'):
                ctx.services.persona.create_persona('alice', persona, '{}')
            note = ctx.services.note
            def remember(_):
                return note.remember('alice', 'one', NoteType.PROFILE_FACT, '用户住在杭州')
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(remember, range(8)))
            assert len({r[0] for r in results}) == 1
            assert sum(r[1] for r in results) == 1
            assert note.remember('alice', 'two', NoteType.PROFILE_FACT, '用户住在杭州')[1]
            assert note.remember('alice', 'one', NoteType.EVENT, '用户住在杭州')[1]

            ctx.services.conversation.log_turn(user_id='alice', persona_id='one', turn_index=1, role='user', content='我在准备面试')
            ctx.llm.chat.return_value = 'invalid JSON'
            report = ctx.services.session_close.close_session('alice', 'one')
            assert report.marker_id > 0 and report.extraction.errors
            ctx.services.session_close.jobs.retry(report.extraction.job_id)
            ctx.llm.chat.return_value = json.dumps({'facts': [
                {'type': 'event', 'content': '用户正在准备面试', 'importance': .1}
            ], 'blocks': []})
            report = ctx.services.session_close.close_session('alice', 'one')
            assert report.marker_id > 0 and report.extraction.facts_created == 1
            again = ctx.services.extraction.extract_last_closed_session('alice', 'one')
            assert again.facts_created == 0 and not again.errors

            note.create_note('alice', 'one', NoteType.PROFILE_FACT, '用户喜欢猫', importance=1)
            found = ctx.services.retrieval.retrieve('alice', 'one', '准备面试', top_k=1)
            assert len(found) == 1 and '面试' in found[0].content
            assert ctx.services.retrieval.retrieve('alice', 'one', '面试', top_k=0) == []
            assert ctx.services.retrieval.retrieve('alice', 'one', '面试', top_k=-1) == []
            note.create_note('alice', 'one', NoteType.EVENT, '用户正在准备面试')
            found = ctx.services.retrieval.retrieve('alice', 'one', '准备面试')
            assert sum(n.content == '用户正在准备面试' for n in found) == 1

            # FTS rowid 与 note rowid 不应被假定为相等。
            ctx.conn.execute('UPDATE note_fts SET rowid=rowid+1000')
            target = results[0][0]
            # 仅索引包含这个词，LIKE 无法掩盖 FTS 连接失败。
            ctx.conn.execute('UPDATE note_fts SET content=? WHERE note_id=?',
                             ('独特索引词', target))
            found = ctx.repos.note.search('独特索引词', user_id='alice', persona_id='one')
            assert target in {n.id for n in found}
            # 图入口的轮次从数据库计算，不依赖调用方过期的状态。
            ctx.llm.chat.return_value = '好'
            before = ctx.services.conversation.get_max_turn_index('alice', 'one')
            state = ctx.workflows.chat.invoke({'user_id': 'alice', 'persona_id': 'one',
                                              'user_input': '继续', 'next_turn_index': 1})
            assert state['next_turn_index'] == before + 2
            from unittest.mock import patch
            markers_before = ctx.conn.execute(
                "SELECT count(*) FROM conversation_log WHERE role='session_close'"
            ).fetchone()[0]
            with patch.object(ctx.services.session_close.extraction_graph, 'invoke',
                              side_effect=RuntimeError('storage failed')):
                try:
                    ctx.services.session_close.close_session('alice', 'one')
                    raise AssertionError('must propagate storage failure')
                except RuntimeError:
                    pass
            assert ctx.conn.execute(
                "SELECT count(*) FROM conversation_log WHERE role='session_close'"
            ).fetchone()[0] == markers_before + 1
            print('[OK] memory pipeline regression')
        finally:
            ctx.conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
