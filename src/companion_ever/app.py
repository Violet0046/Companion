"""app.py — 组合根（composition root）。

唯一的"装配"入口——所有 service、repo、workflow 的依赖关系都在这里按拓扑序构造。
调用方：build_app(AppConfig) → AppContext，从中取出 .services.* / .workflows.* 调用。

依赖 DAG：
    repos/llm/authorizer (leaves)
        ↓
    note_svc, block_svc, conversation_svc, retrieval_svc (mid)
        ↓
    persona_svc, persona_edit_svc, prompt_svc (composite)
        ↓
    chat_svc, extraction_svc (orchestrators)
        ↓
    extraction_workflow (LangGraph)
        ↓
    session_close_svc (uses extraction_workflow)
        ↓
    chat_workflow (uses session_close_svc)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from companion_ever.application.authz import Authorizer
from companion_ever.application.chat.chat_service import ChatService
from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.application.chat.extraction_service import ExtractionService
from companion_ever.application.chat.prompt_service import PromptService
from companion_ever.application.chat.retrieval_service import RetrievalService
from companion_ever.application.chat.session_close_service import SessionCloseService
from companion_ever.application.memory.note_service import NoteService
from companion_ever.application.persona.block_service import BlockService
from companion_ever.application.persona.persona_edit_service import PersonaEditService
from companion_ever.application.persona.persona_service import PersonaService
from companion_ever.application.setting_service import SettingService
from companion_ever.config import AppConfig
from companion_ever.infrastructure.ai.llm_client import LLMClient, create_llm_client
from companion_ever.infrastructure.persistence.block_repo import BlockRepo
from companion_ever.infrastructure.persistence.conversation_repo import ConversationRepo
from companion_ever.infrastructure.persistence.db import init_db
from companion_ever.infrastructure.persistence.note_repo import NoteRepo
from companion_ever.infrastructure.persistence.setting_repo import SettingRepo
from companion_ever.infrastructure.persistence.user_repo import UserRepo
from companion_ever.infrastructure.trace import TraceStore, default_trace_dir, set_default_store
from companion_ever.workflows import build_chat_graph, build_extraction_graph


@dataclass
class Repos:
    """持久化层 4 个 repo。"""

    user: UserRepo
    note: NoteRepo
    block: BlockRepo
    conversation: ConversationRepo


@dataclass
class Services:
    """应用层 10 个 service。"""

    note: NoteService
    block: BlockService
    persona: PersonaService
    persona_edit: PersonaEditService
    conversation: ConversationService
    retrieval: RetrievalService
    settings: SettingService
    prompt: PromptService
    chat: ChatService
    extraction: ExtractionService
    session_close: SessionCloseService


@dataclass
class Workflows:
    """LangGraph 工作流。"""

    chat: object  # CompiledGraph
    extraction: object  # CompiledGraph


@dataclass
class AppContext:
    """应用上下文——持有 cfg / connection / llm / repos / services / workflows。"""

    cfg: AppConfig
    conn: sqlite3.Connection
    llm: LLMClient
    repos: Repos
    services: Services
    workflows: Workflows


def build_app(cfg: AppConfig) -> AppContext:
    """根据 cfg 构造完整的 AppContext。"""
    # 1. Connection
    conn = init_db(cfg.db_path, check_same_thread=cfg.check_same_thread)

    # 1b. trace 存储（诊断数据落文件，默认 <db 同级>/traces/）
    set_default_store(TraceStore(default_trace_dir(cfg.db_path)))

    # 2. Repos（无依赖）
    repos = Repos(
        user=UserRepo(conn),
        note=NoteRepo(conn),
        block=BlockRepo(conn),
        conversation=ConversationRepo(conn),
    )
    setting_repo = SettingRepo(conn)

    # 3. LLM client
    llm = create_llm_client(cfg.llm)

    # 4. Authorizer
    authorizer = Authorizer(repos.user, repos.note)

    # 5. Services — 拓扑序
    note_svc = NoteService(authorizer, repos.note)
    block_svc = BlockService(authorizer, repos.block)
    persona_svc = PersonaService(repos.user, repos.block, note_svc, authorizer)
    persona_edit_svc = PersonaEditService(note_svc, llm)
    conversation_svc = ConversationService(authorizer, repos.conversation)
    retrieval_svc = RetrievalService(repos.note, authorizer, cfg.retrieval)
    setting_svc = SettingService(authorizer, setting_repo)
    prompt_svc = PromptService(
        authorizer,
        conversation_svc,
        block_svc,
        note_svc,
        retrieval_svc,
        cfg.tz_offset_hours,
        cfg.recent_message_limit,
    )
    # session_close_svc 在下面构造，这里先占位，后面回填
    chat_svc = ChatService(
        prompt_svc, conversation_svc, llm,
        session_close_service=None, idle_session_minutes=cfg.idle_session_minutes,
    )
    extraction_svc = ExtractionService(
        conversation_svc, note_svc, block_svc, llm,
    )

    # 6. LangGraph workflows
    # extraction_graph 在 session_close 之前建好
    extraction_graph = build_extraction_graph(
        conversation_service=conversation_svc,
        note_service=note_svc,
        block_service=block_svc,
        llm_client=llm,
    )
    session_close_svc = SessionCloseService(
        authorizer, conversation_svc, extraction_graph,
    )
    # 回填：chat 路径也要能自动关 session
    chat_svc.session_close_service = session_close_svc
    chat_graph = build_chat_graph(
        prompt_service=prompt_svc,
        conversation_service=conversation_svc,
        llm_client=llm,
        session_close_service=session_close_svc,
        idle_session_minutes=cfg.idle_session_minutes,
    )

    services = Services(
        note=note_svc,
        block=block_svc,
        persona=persona_svc,
        persona_edit=persona_edit_svc,
        conversation=conversation_svc,
        retrieval=retrieval_svc,
        settings=setting_svc,
        prompt=prompt_svc,
        chat=chat_svc,
        extraction=extraction_svc,
        session_close=session_close_svc,
    )
    workflows = Workflows(chat=chat_graph, extraction=extraction_graph)

    return AppContext(
        cfg=cfg, conn=conn, llm=llm,
        repos=repos, services=services, workflows=workflows,
    )


__all__ = ["AppContext", "Repos", "Services", "Workflows", "build_app"]