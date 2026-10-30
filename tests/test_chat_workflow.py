"""chat_workflow LangGraph 端到端测试。

验证：
1. graph.invoke() 跑通单轮对话
2. conversation_log 写入正确（user + assistant 各一行）
3. /quit 触发 close_node（写 marker + 抽取）
4. 多轮 turn_index 单调递增
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_ctx(db_path: str, llm_response: str = "回复内容", *, check_same_thread: bool = True):
    """构造一个 mock 过的 ctx。"""
    from companion_ever.config import AppConfig, RetrievalConfig
    from companion_ever.services.authz import Authorizer
    from companion_ever.services.chat_service import ChatService
    from companion_ever.services.conversation_service import ConversationService
    from companion_ever.services.extraction_service import ExtractionService
    from companion_ever.services.prompt_service import PromptService
    from companion_ever.services.retrieval_service import RetrievalService
    from companion_ever.services.session_close_service import (
        SessionCloseReport,
        SessionCloseService,
    )
    from companion_ever.services.note_service import NoteService
    from companion_ever.services.block_service import BlockService
    from companion_ever.services.persona_service import PersonaService
    from companion_ever.services.setting_service import SettingService
    from companion_ever.storage.block_repo import BlockRepo
    from companion_ever.storage.conversation_repo import ConversationRepo
    from companion_ever.storage.db import init_db
    from companion_ever.storage.note_repo import NoteRepo
    from companion_ever.storage.setting_repo import SettingRepo
    from companion_ever.storage.user_repo import UserRepo
    from companion_ever.app import Repos, Services, Workflows, AppContext
    from companion_ever.workflows import build_chat_graph

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
    authorizer = Authorizer(user_repo, note_repo)
    note_svc = NoteService(authorizer, note_repo)
    block_svc = BlockService(authorizer, block_repo)
    persona_svc = PersonaService(user_repo, block_repo, note_svc, authorizer)
    conv_svc = ConversationService(authorizer, conv_repo)
    retrieval_svc = RetrievalService(note_repo, authorizer, cfg.retrieval)
    settings_svc = SettingService(authorizer, setting_repo)
    prompt_svc = PromptService(
        authorizer, conv_svc, block_svc, note_svc, retrieval_svc,
        cfg.tz_offset_hours, cfg.recent_message_limit,
    )

    llm = MagicMock()
    llm.chat.return_value = llm_response

    chat_svc = ChatService(prompt_svc, conv_svc, llm)
    extraction_svc = ExtractionService(conv_svc, note_svc, block_svc, llm)
    close_svc = SessionCloseService(authorizer, conv_svc, extraction_svc)
    chat_graph = build_chat_graph(
        prompt_service=prompt_svc,
        conversation_service=conv_svc,
        llm_client=llm,
        session_close_service=close_svc,
    )

    repos = Repos(user=user_repo, note=note_repo, block=block_repo, conversation=conv_repo)
    services = Services(
        note=note_svc, block=block_svc, persona=persona_svc,
        persona_edit=None, conversation=conv_svc, retrieval=retrieval_svc,
        prompt=prompt_svc, chat=chat_svc, extraction=extraction_svc,
        session_close=close_svc,
        settings=settings_svc,
    )
    workflows = Workflows(chat=chat_graph, extraction=extraction_svc.graph)
    return AppContext(
        cfg=cfg, conn=conn, llm=llm,
        repos=repos, services=services, workflows=workflows,
    )


def test_graph_invoke_single_chat():
    """图执行单轮对话：input → graph → response + log。"""
    from companion_ever.workflows.chat_workflow import build_chat_graph, ChatState

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path, llm_response="你好呀！")
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="舒舒",
            definition='{"name": "舒舒"}',
        )

        graph = ctx.workflows.chat

        # 用 LangGraph invoke
        result: ChatState = graph.invoke({
            "user_id": "alice",
            "persona_id": "舒舒",
            "user_input": "你好，我叫周艺科",
        })

        # 验证 response
        assert result["response"] == "你好呀！"

        # 验证 conversation_log 写入
        recent = ctx.services.conversation.get_recent_messages("alice", "舒舒")
        assert len(recent) == 2
        roles = {m.role for m in recent}
        assert roles == {"user", "assistant"}

        # 验证 turn_index 递增
        assert result["next_turn_index"] == 2
        assert result["conversations_count"] == 1

        # 因为 user_input 不是 /quit，不会触发 close
        assert "close_report" not in result or result.get("close_report") is None

        print("[OK] graph.invoke 单轮对话")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_graph_invoke_quit_triggers_close():
    """输入 /quit 时图路由到 close 节点。"""
    from companion_ever.workflows.chat_workflow import build_chat_graph, ChatState

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path, llm_response="再见！")
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="舒舒",
            definition='{"name": "舒舒"}',
        )

        graph = ctx.workflows.chat

        result = graph.invoke({
            "user_id": "alice",
            "persona_id": "舒舒",
            "user_input": "/quit",
        })

        # 验证 close_report 存在
        assert result.get("close_report") is not None, "close 节点应被触发"
        from companion_ever.services.session_close_service import SessionCloseReport
        assert isinstance(result["close_report"], SessionCloseReport)
        assert result["close_report"].marker_id > 0

        # 验证 session_close marker 已写入
        markers = ctx.services.conversation.get_last_n_session_closes(
            user_id="alice", persona_id="舒舒", n=1,
        )
        assert len(markers) == 1
        assert markers[0].role == "session_close"

        print("[OK] graph.invoke /quit 触发 close")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_graph_invoke_multiple_chat():
    """多轮 chat：turn_index 单调递增。"""
    from companion_ever.workflows.chat_workflow import build_chat_graph

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        ctx = _make_ctx(db_path, llm_response="回复")
        ctx.repos.user.create("alice")
        ctx.services.persona.create_persona(
            user_id="alice", persona_id="舒舒",
            definition='{"name": "舒舒"}',
        )

        graph = ctx.workflows.chat

        # 3 轮对话
        next_turn = 1
        for i in range(3):
            result = graph.invoke({
                "user_id": "alice",
                "persona_id": "舒舒",
                "user_input": f"第 {i+1} 轮",
                "next_turn_index": next_turn,
            })
            # /quit 才触发 close，其他轮只 chat
            if i < 2:
                assert result.get("close_report") is None
            next_turn = result["next_turn_index"]

        # 验证 conversation_log：6 条（3 user + 3 assistant）
        recent = ctx.services.conversation.get_recent_messages("alice", "舒舒")
        assert len(recent) == 6
        turn_indices = sorted([m.turn_index for m in recent])
        assert turn_indices == [1, 1, 2, 2, 3, 3]

        print("[OK] 多轮 graph.invoke")
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    test_graph_invoke_single_chat()
    test_graph_invoke_quit_triggers_close()
    test_graph_invoke_multiple_chat()
    print("[ALL] chat_workflow LangGraph 测试通过")
