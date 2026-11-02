"""装配入口：build_app(AppConfig) → AppContext。

构造仓库、客户端、业务服务、调用适配和后台调度；依赖关系集中在此。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from companion_ever.characters.access import Authorizer
from companion_ever.conversations.chat import ChatService
from companion_ever.conversations.execution import GenerationService
from companion_ever.conversations.messages import ConversationService
from companion_ever.memory.extraction.service import ExtractionService
from companion_ever.conversations.context import PromptService
from companion_ever.memory.retrieval.service import RetrievalService
from companion_ever.memory.extraction.sessions import SessionCloseService
from companion_ever.memory.notes.service import NoteService
from companion_ever.memory.summaries.blocks import BlockService
from companion_ever.memory.summaries.service import SummaryService
from companion_ever.memory.scheduling import MemoryScheduler
from companion_ever.characters.service import PersonaService
from companion_ever.conversations.settings import SettingService
from companion_ever.config import AppConfig
from companion_ever.infrastructure.llm.llm_client import LLMClient, create_llm_client
from companion_ever.memory.summaries.block_repository import BlockRepo
from companion_ever.conversations.repository import ConversationRepo
from companion_ever.infrastructure.database.connection import init_db
from companion_ever.memory.notes.repository import NoteRepo
from companion_ever.conversations.settings_repository import SettingRepo
from companion_ever.characters.users import UserRepo
from companion_ever.characters.repository import PersonaRepo
from companion_ever.conversations.generation_repository import GenerationRepo
from companion_ever.memory.extraction.jobs import MemoryJobRepo
from companion_ever.memory.summaries.repository import SummaryRepo
from companion_ever.infrastructure.tracing import CallLogStore, default_call_dir
from companion_ever.infrastructure.llm.traced_client import TracedLLMClient
from companion_ever.bootstrap.lifecycle import ApplicationLifecycle, build_lifecycle
from companion_ever.notifications.repository import NotificationRepo
from companion_ever.notifications.service import NotificationService
from companion_ever.infrastructure.web_push import WebPushSender
from companion_ever.conversations.proactive import ProactiveService
from companion_ever.accounts.usage import AccountUsage
from companion_ever.conversations.groups import GroupService


@dataclass
class Repos:
    """共享仓库。"""

    user: UserRepo
    note: NoteRepo
    block: BlockRepo
    conversation: ConversationRepo


@dataclass
class Services:
    """应用服务。"""

    note: NoteService
    block: BlockService
    persona: PersonaService
    conversation: ConversationService
    retrieval: RetrievalService
    settings: SettingService
    prompt: PromptService
    chat: ChatService
    generation: GenerationService
    extraction: ExtractionService
    session_close: SessionCloseService
    summary: SummaryService
    background: MemoryScheduler
    notifications: NotificationService | None = None
    proactive: ProactiveService | None = None
    usage: AccountUsage | None = None
    groups: GroupService | None = None


@dataclass
class Workflows:
    """LangGraph 工作流。"""

    extraction: object  # CompiledGraph
    summary: object = None  # CompiledGraph


@dataclass
class AppContext:
    """应用上下文——持有 cfg / connection / llm / repos / services / workflows。"""

    cfg: AppConfig
    conn: Any   # ThreadLocalConnection（每线程一条连接）
    llm: LLMClient
    repos: Repos
    services: Services
    workflows: Workflows
    lifecycle: ApplicationLifecycle | None = None
    call_logs: CallLogStore | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        if self.call_logs is None:
            self.call_logs = CallLogStore(default_call_dir(self.cfg.db_path), tz_offset_hours=self.cfg.tz_offset_hours)
        if isinstance(self.llm, TracedLLMClient):
            self.llm.store = self.call_logs
        if self.services.usage is None:
            self.services.usage = AccountUsage(self.conn, self.cfg.account_limits,
                tz_offset_hours=self.cfg.tz_offset_hours, call_logs=self.call_logs)
        self.services.chat.generations.usage = self.services.usage
        if self.services.groups is None:
            self.services.groups=GroupService(self.services.chat.generations,self.services.conversation.authorizer,self.services.prompt,self.llm)
        self.services.generation.groups=self.services.groups
        self.services.groups.notifications=self.services.notifications
        self.services.groups.on_member_left=self.services.session_close.jobs.revoke_group
        if self.services.proactive is not None:self.services.proactive.groups=self.services.groups
        if self.services.notifications is not None:self.services.notifications.group_delivery_allowed=self.services.groups.notification_allowed
        if self.lifecycle is None:
            self.lifecycle = build_lifecycle(self.cfg, self.conn, self.services)

    def close(self):
        """拥有该应用的宿主释放资源；外部注入的 Web 上下文只停止生命周期。"""
        if self._closed:
            return
        self._closed = True
        try:
            self.lifecycle.stop()
            close_model = getattr(self.llm, 'close', None)
            if callable(close_model):
                close_model()
        finally:
            self.conn.close()


def build_app(cfg: AppConfig) -> AppContext:
    """根据 cfg 构造完整的 AppContext。"""
    # 1. Connection
    conn = init_db(cfg.db_path, check_same_thread=cfg.check_same_thread)

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
    authorizer = Authorizer(repos.user)

    # 5. Services — 拓扑序
    note_svc = NoteService(authorizer, repos.note)
    block_svc = BlockService(authorizer, repos.block)
    summary_svc = SummaryService(authorizer,repos.note,repos.block,llm,timeout=cfg.extraction_timeout,thinking=cfg.background.model_thinking)
    memory_jobs = MemoryJobRepo(conn)
    summary_jobs = SummaryRepo(conn)
    persona_svc = PersonaService(
        repos.user, PersonaRepo(conn), authorizer,
        on_created=(repos.block.initialize_persona,),
        on_deactivated=(GenerationRepo(conn).revoke_persona,
                        memory_jobs.revoke_persona, summary_jobs.revoke_persona),
        on_restored=(memory_jobs.restore_persona, summary_jobs.restore_persona),
    )
    conversation_svc = ConversationService(authorizer, repos.conversation)
    retrieval_svc = RetrievalService(repos.note, authorizer, cfg.retrieval)
    setting_svc = SettingService(authorizer, setting_repo)
    prompt_svc = PromptService(
        authorizer,
        conversation_svc,
        block_svc,
        note_svc,
        retrieval_svc,
        persona_svc,
        cfg.tz_offset_hours,
        cfg.recent_message_limit,
        cfg.history_max_messages,
        summary_service=summary_svc,
    )
    extraction_svc = ExtractionService(
        conversation_svc, note_svc, block_svc, llm, timeout=cfg.extraction_timeout, tz_offset_hours=cfg.tz_offset_hours,thinking=cfg.background.model_thinking,
    )

    # 6. LangGraph workflows
    session_close_svc = SessionCloseService(
        authorizer, conversation_svc, extraction_svc,
    )
    session_close_svc.automatic=cfg.background.enabled
    chat_svc = ChatService(
        prompt_svc, conversation_svc, llm,
        session_close_service=session_close_svc, idle_session_minutes=cfg.idle_session_minutes,
    )
    background_svc = MemoryScheduler(cfg.background, authorizer, conversation_svc, session_close_svc, summary_svc)
    session_close_svc.on_memory_settled = background_svc.memory_settled

    notifications_svc=NotificationService(NotificationRepo(conn), authorizer, WebPushSender(), cfg.push_subject,
                                          delivery_allowed=conversation_svc.should_notify)
    proactive_svc=ProactiveService(chat_svc.generations, authorizer, setting_repo, prompt_svc, llm,
                                    notifications_svc, tz_offset_hours=cfg.tz_offset_hours)
    services = Services(
        note=note_svc,
        block=block_svc,
        persona=persona_svc,
        conversation=conversation_svc,
        retrieval=retrieval_svc,
        settings=setting_svc,
        prompt=prompt_svc,
        chat=chat_svc,
        generation=GenerationService(chat_svc, setting_svc, proactive=proactive_svc),
        extraction=extraction_svc,
        session_close=session_close_svc,
        summary=summary_svc,
        background=background_svc,
        notifications=notifications_svc,
        proactive=proactive_svc,
    )
    workflows = Workflows(extraction=extraction_svc.graph,summary=summary_svc.graph)

    ctx=AppContext(
        cfg=cfg, conn=conn, llm=llm,
        repos=repos, services=services, workflows=workflows,
    )
    return ctx


__all__ = ["AppContext", "Repos", "Services", "Workflows", "build_app"]
