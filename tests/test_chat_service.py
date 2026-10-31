"""普通聊天与调试终端契约；输入文本不包含退出或整理命令。"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_ctx(db_path: str, llm_response: str = "回复内容", *, check_same_thread: bool = True):
    """构造一个 mock 过的 ctx。"""
    from companion_ever.config import AppConfig
    from companion_ever.characters.access import Authorizer
    from companion_ever.conversations.chat import ChatService
    from companion_ever.conversations.execution import GenerationService
    from companion_ever.conversations.messages import ConversationService
    from companion_ever.memory.extraction.service import ExtractionService
    from companion_ever.conversations.context import PromptService
    from companion_ever.memory.retrieval.service import RetrievalService
    from companion_ever.memory.extraction.sessions import (
        SessionCloseService,
    )
    from companion_ever.memory.notes.service import NoteService
    from companion_ever.memory.summaries.blocks import BlockService
    from companion_ever.memory.summaries.service import SummaryService
    from companion_ever.memory.scheduling import MemoryScheduler
    from companion_ever.characters.service import PersonaService
    from companion_ever.characters.repository import PersonaRepo
    from companion_ever.conversations.settings import SettingService
    from companion_ever.memory.summaries.block_repository import BlockRepo
    from companion_ever.conversations.repository import ConversationRepo
    from companion_ever.infrastructure.database.connection import init_db
    from companion_ever.memory.notes.repository import NoteRepo
    from companion_ever.conversations.settings_repository import SettingRepo
    from companion_ever.characters.users import UserRepo
    from companion_ever.bootstrap.app import Repos, Services, Workflows, AppContext

    cfg = AppConfig(
        db_path=db_path,
        llm={"provider": "openai_compat", "api_key": "sk-test",
             "base_url": "https://x", "model": "test"},
        tz_offset_hours=8,
        recent_message_limit=20,
        retrieval={"top_k": 10},
        background={'enabled':False},
    )

    conn = init_db(db_path, check_same_thread=check_same_thread)
    setting_repo = SettingRepo(conn)
    user_repo = UserRepo(conn)
    note_repo = NoteRepo(conn)
    block_repo = BlockRepo(conn)
    conv_repo = ConversationRepo(conn)
    authorizer = Authorizer(user_repo)
    note_svc = NoteService(authorizer, note_repo)
    block_svc = BlockService(authorizer, block_repo)
    persona_svc = PersonaService(user_repo, PersonaRepo(conn), authorizer,
                                 on_created=(block_repo.initialize_persona,))
    conv_svc = ConversationService(authorizer, conv_repo)
    retrieval_svc = RetrievalService(note_repo, authorizer, cfg.retrieval)
    settings_svc = SettingService(authorizer, setting_repo)
    llm = MagicMock()
    llm.chat.return_value = llm_response
    summary_svc = SummaryService(authorizer, note_repo, block_repo, llm)
    prompt_svc = PromptService(
        authorizer, conv_svc, block_svc, note_svc, retrieval_svc, persona_svc,
        cfg.tz_offset_hours, cfg.recent_message_limit,
        summary_service=summary_svc,
    )

    extraction_svc = ExtractionService(conv_svc, note_svc, block_svc, llm)
    close_svc = SessionCloseService(authorizer, conv_svc, extraction_svc)
    chat_svc = ChatService(prompt_svc, conv_svc, llm, session_close_service=close_svc)

    repos = Repos(user=user_repo, note=note_repo, block=block_repo, conversation=conv_repo)
    services = Services(
        note=note_svc, block=block_svc, persona=persona_svc,
        conversation=conv_svc, retrieval=retrieval_svc,
        prompt=prompt_svc, chat=chat_svc, extraction=extraction_svc,
        generation=GenerationService(chat_svc, settings_svc),
        session_close=close_svc,
        settings=settings_svc,
        summary=summary_svc,
        background=MemoryScheduler(cfg.background,authorizer,conv_svc,close_svc,summary_svc),
    )
    workflows = Workflows(extraction=extraction_svc.graph, summary=summary_svc.graph)
    return AppContext(
        cfg=cfg, conn=conn, llm=llm,
        repos=repos, services=services, workflows=workflows,
    )


def test_single_and_multiple_chat():
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        ctx = _make_ctx(str(Path(tmp) / 'chat.db'), llm_response='reply')
        try:
            ctx.repos.user.create('alice')
            ctx.services.persona.create_persona('alice', 'one', '{}')
            for text in ('hello', 'next', 'again'):
                assert ctx.services.chat.chat('alice', 'one', text) == 'reply'
            messages = ctx.services.conversation.get_recent_messages('alice', 'one')
            assert [m.turn_index for m in messages] == [1, 1, 2, 2, 3, 3]
            assert [m.role for m in messages] == ['user', 'assistant'] * 3
            assert ctx.llm.chat.call_count == 3
            assert ctx.services.conversation.get_last_n_session_closes('alice', 'one') == []
        finally:
            ctx.conn.close()


def test_cli_inputs_are_plain_chat(termination=EOFError):
    from argparse import Namespace
    from contextlib import redirect_stdout
    from io import StringIO
    from pathlib import Path
    from unittest.mock import patch
    from companion_ever.entrypoints.cli import cmd_chat
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'cli.db'
        ctx = _make_ctx(str(path), llm_response='reply')
        try:
            ctx.repos.user.create('alice')
            ctx.services.persona.create_persona('alice', 'one', '{}')
            ctx.services.chat.idle_session_minutes = 0
            inputs = ['/quit', '/exit', 'still here', termination()]
            with patch('companion_ever.entrypoints.cli._build_app', return_value=ctx), \
                 patch('builtins.input', side_effect=inputs), \
                 patch.object(ctx.conn, 'close') as close, \
                 patch.object(ctx.services.session_close, 'close_session') as force_close, \
                 patch.object(ctx.services.session_close, 'close_session_background') as force_background, \
                 redirect_stdout(StringIO()):
                assert cmd_chat(Namespace(db=path, user='alice', persona='one')) == 0
                close.assert_called_once()
                force_close.assert_not_called()
                force_background.assert_not_called()
            messages = ctx.services.conversation.get_recent_messages('alice', 'one')
            assert [m.content for m in messages if m.role == 'user'] == inputs[:3]
            assert ctx.llm.chat.call_count == 3
            assert ctx.services.conversation.get_last_n_session_closes('alice', 'one') == []
            assert ctx.conn.execute('SELECT count(*) FROM memory_job').fetchone()[0] == 0
        finally:
            ctx.conn.close()


if __name__ == '__main__':
    test_single_and_multiple_chat()
    for termination in (EOFError, KeyboardInterrupt):
        test_cli_inputs_are_plain_chat(termination)
    print('[OK] plain chat, persisted turns, literal slash inputs and CLI EOF')

