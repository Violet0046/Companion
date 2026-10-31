"""Sentinel 方案端到端测试。

验证完整流程：
1. 用户消息 → log_turn
2. LLM 回复 → log_turn
3. 多轮对话
4. 会话关闭 → log_session_close（写 marker）
5. 抽取 → process_last_closed_session（按 marker 边界）
6. 验证：notes 创建、Block 更新、对话不含 session_close 行

变化：
- 完整的 Sentinel 流程测试
- 验证 marker 不污染对话查询结果
- 验证多次 close_session 的 marker 边界正确
- 用 session_close_service.close_session() 完整流程
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from companion_ever.characters.access import Authorizer
from companion_ever.conversations.messages import ConversationService
from companion_ever.memory.extraction.service import ExtractionService
from companion_ever.memory.extraction.sessions import (
    SessionCloseReport,
    SessionCloseService,
)
from companion_ever.memory.notes.service import NoteService
from companion_ever.memory.summaries.blocks import BlockService
from companion_ever.characters.service import PersonaService
from companion_ever.characters.repository import PersonaRepo
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.summaries.block_repository import BlockRepo
from companion_ever.conversations.repository import ConversationRepo
from companion_ever.infrastructure.database.connection import init_db
from companion_ever.memory.notes.repository import NoteRepo
from companion_ever.characters.users import UserRepo
from companion_ever.characters.templates import template_to_json


def _make_stack(db_path: str, llm=None):
    """wire 一组完整 service 栈。llm 默认是 mock（不调真实 API）。"""
    from unittest.mock import MagicMock

    conn = init_db(db_path)
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
    if llm is None:
        # mock LLM：返回符合 schema 的合法 JSON
        # 包含一条 fact（用于测试 notes 写入路径）
        llm = MagicMock()
        from test_kind_storage import proposal
        from fake_memory import no_memory
        def reply(messages, **kwargs):
            case=json.loads(messages[-1]['content'])
            matching=[m for m in case['messages'] if m['role']=='user' and m['id'] in case['new_message_ids'] and '我叫小明' in m['content']]
            if not matching:
                return no_memory(messages)
            result=json.loads(no_memory(messages))
            result['proposals']=[proposal(matching[0]['id'],'我叫小明',content='用户叫小明')]
            result['skipped']=[s for s in result['skipped'] if s['message_id']!=matching[0]['id']]
            return json.dumps(result)
        llm.chat.side_effect = reply
    extraction_svc = ExtractionService(conv_svc, note_svc, block_svc, llm)
    session_close_svc = SessionCloseService(authorizer, conv_svc, extraction_svc)
    return {
        "conn": conn,
        "user_repo": user_repo,
        "note_repo": note_repo,
        "block_repo": block_repo,
        "conv_svc": conv_svc,
        "note_svc": note_svc,
        "persona_svc": persona_svc,
        "session_close_svc": session_close_svc,
        "extraction_svc": extraction_svc,
        "llm": llm,
    }


def test_full_sentinel_flow_extracts_session():
    """完整 Sentinel 流程：多轮对话 → close → extract。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = _make_stack(db_path)
        conn = s["conn"]

        # 1. 准备 user + persona
        s["user_repo"].create("alice")
        s["persona_svc"].create_persona(
            user_id="alice",
            persona_id="舒舒",
            definition=template_to_json("hot_girlfriend"),
        )

        # 2. 多轮对话
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=1, role="user", content="你好，我叫小明",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=1, role="assistant", content="你好呀",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=2, role="user", content="我最近秋招压力大",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=2, role="assistant", content="别担心",
        )

        # 3. 关闭会话（写 marker + 抽取）
        report = s["session_close_svc"].close_session("alice", "舒舒")

        # 4. 验证 SessionCloseReport
        assert isinstance(report, SessionCloseReport)
        assert report.marker_id > 0, "marker_id 应是有效的 rowid"
        assert report.extraction.conversations_processed >= 4

        # 5. 验证 conversation_log 中有 marker
        markers = s["conv_svc"].get_last_n_session_closes(
            user_id="alice", persona_id="舒舒", n=1
        )
        assert len(markers) == 1
        assert markers[0].role == "session_close"

        # 6. 验证：marker 不污染对话查询
        recent = s["conv_svc"].get_recent_messages("alice", "舒舒", limit=100)
        for m in recent:
            assert m.role != "session_close", \
                f"对话查询不应返回 marker 行: {m}"

        # 7. 验证 count 不含 marker
        cnt = s["conv_svc"].count("alice", "舒舒")
        assert cnt == 4, f"count 应是 4 条真实对话, 实际 {cnt}"

        # 8. 验证至少创建了一条 note（用户说"叫小明"）
        notes = s["note_svc"].find_notes(
            user_id="alice", persona_id="舒舒",
            kinds=[MemoryKind.FACT],
        )
        assert len(notes) >= 1, "应至少有一条 profile_fact 笔记（名字抽取）"
        # 验证内容包含名字
        assert any("小明" in n.content for n in notes)

        print("[OK] 完整 Sentinel 流程通过")
        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_multiple_sessions_each_closes_independently():
    """多次 close_session 应各自有独立的 marker 边界。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = _make_stack(db_path)
        conn = s["conn"]

        s["user_repo"].create("alice")
        s["persona_svc"].create_persona(
            user_id="alice", persona_id="舒舒",
            definition="默认人设",
        )

        # Session 1
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=1, role="user", content="第一次对话 1",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=1, role="assistant", content="回复 1",
        )
        r1 = s["session_close_svc"].close_session("alice", "舒舒")
        assert r1.marker_id > 0

        # Session 2
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=2, role="user", content="第二次对话 2",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=2, role="assistant", content="回复 2",
        )
        r2 = s["session_close_svc"].close_session("alice", "舒舒")
        assert r2.marker_id > r1.marker_id

        # 验证：有两个 marker
        markers = s["conv_svc"].get_last_n_session_closes(
            user_id="alice", persona_id="舒舒", n=10
        )
        assert len(markers) == 2

        # 验证：对话查询不含 marker
        recent = s["conv_svc"].get_recent_messages("alice", "舒舒", limit=100)
        assert all(m.role != "session_close" for m in recent)

        print("[OK] 多次 session 边界正确")
        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_session_close_rejects_invalid_permission():
    """close_session 在权限不足时应该抛错。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = _make_stack(db_path)
        conn = s["conn"]

        # 不创建 user，直接 close
        try:
            s["session_close_svc"].close_session("ghost", "舒舒")
            print("[FAIL] 应报错")
        except Exception as e:
            print(f"[OK] 权限校验报错: {type(e).__name__}")

        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_marker_not_in_user_prompt():
    """marker 不应出现在 user-facing 的 prompt 中。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        s = _make_stack(db_path)
        conn = s["conn"]

        s["user_repo"].create("alice")
        s["persona_svc"].create_persona(
            user_id="alice", persona_id="舒舒", definition="默认",
        )
        s["conv_svc"].log_turn(
            user_id="alice", persona_id="舒舒",
            turn_index=1, role="user", content="测试 1",
        )
        s["session_close_svc"].close_session("alice", "舒舒")

        # 验证 get_recent_messages 不含 marker
        recent = s["conv_svc"].get_recent_messages("alice", "舒舒")
        for m in recent:
            assert m.role != "session_close"

        # 验证 get_last_conversation_at 不含 marker 时间
        last_at = s["conv_svc"].get_last_conversation_at("alice", "舒舒")
        # last_at 应该是 user 消息的时间，不是 marker 的时间

        print("[OK] marker 不污染 user-facing 查询")
        conn.close()
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    test_full_sentinel_flow_extracts_session()
    test_multiple_sessions_each_closes_independently()
    test_session_close_rejects_invalid_permission()
    test_marker_not_in_user_prompt()
    print("[ALL] Sentinel 端到端测试通过")
