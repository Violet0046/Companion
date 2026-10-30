"""config — 配置层。

统一的 dataclass 配置入口，替代散落在各处的硬编码。
通过 AppConfig.from_env() 从环境变量构造（便于测试与部署）。

支持灵活构造：
- AppConfig(db_path, llm=LLMConfig(...), retrieval=RetrievalConfig(...))
- AppConfig(db_path, llm={"api_key":...}, retrieval={"top_k":...})  # dict 也可
- AppConfig.from_env()  # 从环境变量

环境变量：
- COMPANION_DB_PATH             默认 ./companion.db
- COMPANION_TZ_OFFSET_HOURS      默认 8
- COMPANION_RECENT_MESSAGE_LIMIT 默认 20
- OPENAI_API_KEY                 必填（LLM）
- OPENAI_BASE_URL                 必填（LLM）
- OPENAI_MODEL                   必填（LLM）
- OPENAI_TIMEOUT                 默认 60 秒
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence



@dataclass(frozen=True)
class LLMConfig:
    """LLM 调用配置。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    timeout: float = 60.0
    provider: str = "openai_compat"

    @classmethod
    def from_any(cls, value: "LLMConfig | dict | None") -> "LLMConfig":
        """从 dict / LLMConfig / None 构造。"""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})
        raise TypeError(f"LLMConfig.from_any: 不支持 {type(value)}")


def _default_retrieval_weights() -> dict[str, float]:
    return {
        "importance": 0.5,
        "recency": 0.2,
        "fts": 0.2,
    }


@dataclass(frozen=True)
class RetrievalConfig:
    """Wiki 检索配置。"""

    weights: dict[str, float] = field(default_factory=_default_retrieval_weights)
    top_k: int = 10

    @classmethod
    def from_any(cls, value: "RetrievalConfig | dict | None") -> "RetrievalConfig":
        """从 dict / RetrievalConfig / None 构造。"""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            data = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
            return cls(**data)
        raise TypeError(f"RetrievalConfig.from_any: 不支持 {type(value)}")


@dataclass(frozen=True)
class WebConfig:
    """Web / PWA 前端配置（手机端）。"""

    host: str = "0.0.0.0"
    port: int = 8000
    pin: str = ""
    user: str = ""
    persona: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WebConfig":
        """从环境变量构造。

        环境变量：
        - COMPANION_WEB_HOST   默认 0.0.0.0（允许局域网内手机访问）
        - COMPANION_WEB_PORT   默认 8000
        - COMPANION_WEB_PIN    默认空 = 不鉴权（仅建议本机/内网使用）
        - COMPANION_WEB_USER   默认空 = 自动取库里第一个用户
        - COMPANION_WEB_PERSONA 默认空 = 自动取该用户的第一个 persona
        """
        src = env if env is not None else os.environ
        return cls(
            host=src.get("COMPANION_WEB_HOST", "0.0.0.0"),
            port=int(src.get("COMPANION_WEB_PORT", "8000")),
            pin=src.get("COMPANION_WEB_PIN", ""),
            user=src.get("COMPANION_WEB_USER", ""),
            persona=src.get("COMPANION_WEB_PERSONA", ""),
        )


@dataclass(frozen=True)
class BackgroundConfig:
    enabled: bool = True
    memory_message_threshold: int = 6
    memory_idle_seconds: int = 180
    summary_fact_threshold: int = 5
    summary_interval_seconds: int = 900
    failure_cooldown_seconds: int = 900
    model_thinking: bool = False

    def __post_init__(self):
        if min(self.memory_message_threshold,self.memory_idle_seconds,self.summary_fact_threshold,self.summary_interval_seconds,self.failure_cooldown_seconds)<1:
            raise ValueError('后台触发阈值必须为正数')


class AppConfig:
    """全局应用配置（不可变）。

    支持灵活构造：
    - AppConfig(db_path, llm=LLMConfig(...), retrieval=RetrievalConfig(...))
    - AppConfig(db_path, llm={"api_key":...}, retrieval={"top_k":...})  # dict 也可
    - AppConfig.from_env()  # 从环境变量
    """

    __slots__ = (
        "db_path", "tz_offset_hours", "recent_message_limit",
        "history_max_messages", "llm", "retrieval", "check_same_thread",
        "idle_session_minutes",
        "extraction_timeout",
        "background",
    )

    def __init__(
        self,
        db_path: Path | str,
        tz_offset_hours: int = 8,
        recent_message_limit: int = 20,
        history_max_messages: int = 50,
        llm: LLMConfig | dict | None = None,
        retrieval: RetrievalConfig | dict | None = None,
        check_same_thread: bool = False,
        idle_session_minutes: int = 120,
        extraction_timeout: float = 120.0,
        background: BackgroundConfig | dict | None = None,
    ):
        self.db_path = Path(db_path)
        self.tz_offset_hours = tz_offset_hours
        self.recent_message_limit = recent_message_limit
        # Chat LLM 拼 prompt 时取的最大消息条数（防止 context 无限增长）
        self.history_max_messages = history_max_messages
        self.llm = LLMConfig.from_any(llm)
        self.retrieval = RetrievalConfig.from_any(retrieval)
        # 跨线程共用同一条连接（会话自动关闭的后台线程也要用）。
        # SQLite 本身是 serialized 模式（threadsafety=3），跨线程安全。
        self.check_same_thread = check_same_thread
        # 空闲多久自动判定为"上一个 session 已结束"（分钟）。
        # 0 = 关闭自动判定（只能手动 /quit）。
        self.idle_session_minutes = idle_session_minutes
        if not 0 < extraction_timeout < float('inf'):
            raise ValueError('extraction_timeout 必须为有限正数')
        self.extraction_timeout = extraction_timeout
        self.background=background if isinstance(background,BackgroundConfig) else BackgroundConfig(**(background or {}))

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | str | None = ".env",
    ) -> "AppConfig":
        """从环境变量构造 AppConfig（自动加载 .env 文件）。

        流程：
        1. 如果存在 .env，用 python-dotenv 加载（不覆盖已设置的 os.environ）
        2. 从 os.environ 读取配置

        Args:
            env: 自定义 env 映射（默认 os.environ），便于测试
            dotenv_path: .env 文件路径。传 None 跳过加载；传 ".env" 是默认。

        Returns:
            AppConfig 实例
        """
        # 1. 自动加载 .env（如果存在 + python-dotenv 可用）
        if dotenv_path is not None:
            try:
                from dotenv import load_dotenv
                load_dotenv(dotenv_path=dotenv_path, override=False)
            except ImportError:
                # python-dotenv 未安装——降级到只用 os.environ
                pass

        # 2. 读 os.environ
        env_source = env if env is not None else os.environ
        # 默认 DB 路径放在 ./memory/ 子目录（gitignored），便于集中管理运行时产物
        return cls(
            db_path=Path(env_source.get("COMPANION_DB_PATH", "./memory/companion.db")),
            tz_offset_hours=int(env_source.get("COMPANION_TZ_OFFSET_HOURS", "8")),
            recent_message_limit=int(env_source.get("COMPANION_RECENT_MESSAGE_LIMIT", "20")),
            history_max_messages=int(env_source.get("COMPANION_HISTORY_MAX", "50")),
            idle_session_minutes=int(env_source.get("COMPANION_IDLE_MINUTES", "120")),
            extraction_timeout=float(env_source.get("COMPANION_EXTRACTION_TIMEOUT", "120")),
            background=BackgroundConfig(
                enabled=env_source.get('COMPANION_AUTO_MEMORY','1').lower() not in ('0','false','off'),
                memory_message_threshold=int(env_source.get('COMPANION_MEMORY_TRIGGER_MESSAGES','6')),
                memory_idle_seconds=int(env_source.get('COMPANION_MEMORY_IDLE_SECONDS','180')),
                summary_fact_threshold=int(env_source.get('COMPANION_SUMMARY_TRIGGER_FACTS','5')),
                summary_interval_seconds=int(env_source.get('COMPANION_SUMMARY_INTERVAL_SECONDS','900')),
                failure_cooldown_seconds=int(env_source.get('COMPANION_TASK_RETRY_COOLDOWN_SECONDS','900')),
                model_thinking=env_source.get('COMPANION_BACKGROUND_THINKING','0').lower() in ('1','true','on')),
            llm=LLMConfig(
                api_key=env_source.get("OPENAI_API_KEY", ""),
                base_url=env_source.get("OPENAI_BASE_URL", ""),
                # 模型名优先用 COMPANION_LLM_MODEL，回退到 OPENAI_MODEL（兼容）
                model=env_source.get("COMPANION_LLM_MODEL") or env_source.get("OPENAI_MODEL", ""),
                timeout=float(env_source.get("OPENAI_TIMEOUT", "60")),
            ),
        )

    def __repr__(self) -> str:
        return (
            f"AppConfig(db_path={self.db_path}, tz_offset_hours={self.tz_offset_hours}, "
            f"recent_message_limit={self.recent_message_limit}, llm={self.llm}, retrieval={self.retrieval})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AppConfig):
            return NotImplemented
        return (
            self.db_path == other.db_path
            and self.tz_offset_hours == other.tz_offset_hours
            and self.recent_message_limit == other.recent_message_limit
            and self.llm == other.llm
            and self.retrieval == other.retrieval
            and self.check_same_thread == other.check_same_thread
        )


__all__ = ["LLMConfig", "RetrievalConfig", "WebConfig", "BackgroundConfig", "AppConfig"]
