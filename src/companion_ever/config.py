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

from companion_ever.domain.enums import NoteType


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
        "type": 0.1,
    }


def _default_type_bonus() -> dict[str, float]:
    return {
        "profile_fact": 0.10,
        "preference": 0.09,
        "promise": 0.09,
        "milestone": 0.07,
        "event": 0.05,
        "observation": 0.02,
    }


def _default_always_include_types() -> tuple[NoteType, ...]:
    return (
        NoteType.PROFILE_FACT,
        NoteType.PREFERENCE,
        NoteType.PROMISE,
        NoteType.MILESTONE,
        NoteType.INSIDE_JOKE,
    )


@dataclass(frozen=True)
class RetrievalConfig:
    """Wiki 检索配置。"""

    weights: dict[str, float] = field(default_factory=_default_retrieval_weights)
    type_bonus: dict[str, float] = field(default_factory=_default_type_bonus)
    top_k: int = 10
    always_include_types: tuple[NoteType, ...] = field(default_factory=_default_always_include_types)

    @classmethod
    def from_any(cls, value: "RetrievalConfig | dict | None") -> "RetrievalConfig":
        """从 dict / RetrievalConfig / None 构造；自动转换 always_include_types 字符串→NoteType。"""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            data = {k: v for k, v in value.items() if k in cls.__dataclass_fields__}
            # 自动转换 always_include_types：list[str] / Sequence → tuple[NoteType, ...]
            if "always_include_types" in data:
                data["always_include_types"] = _coerce_note_types(data["always_include_types"])
            return cls(**data)
        raise TypeError(f"RetrievalConfig.from_any: 不支持 {type(value)}")


def _coerce_note_types(value: Sequence[Any]) -> tuple[NoteType, ...]:
    """list[str] / Sequence[NoteType] → tuple[NoteType, ...]"""
    out: list[NoteType] = []
    for v in value:
        if isinstance(v, NoteType):
            out.append(v)
        elif isinstance(v, str):
            out.append(NoteType(v))
        else:
            raise TypeError(f"无法转换为 NoteType: {v!r}")
    return tuple(out)


class AppConfig:
    """全局应用配置（不可变）。

    支持灵活构造：
    - AppConfig(db_path, llm=LLMConfig(...), retrieval=RetrievalConfig(...))
    - AppConfig(db_path, llm={"api_key":...}, retrieval={"top_k":...})  # dict 也可
    - AppConfig.from_env()  # 从环境变量
    """

    __slots__ = (
        "db_path", "tz_offset_hours", "recent_message_limit",
        "history_max_messages", "llm", "retrieval",
    )

    def __init__(
        self,
        db_path: Path | str,
        tz_offset_hours: int = 8,
        recent_message_limit: int = 20,
        history_max_messages: int = 50,
        llm: LLMConfig | dict | None = None,
        retrieval: RetrievalConfig | dict | None = None,
    ):
        self.db_path = Path(db_path)
        self.tz_offset_hours = tz_offset_hours
        self.recent_message_limit = recent_message_limit
        # Chat LLM 拼 prompt 时取的最大消息条数（防止 context 无限增长）
        self.history_max_messages = history_max_messages
        self.llm = LLMConfig.from_any(llm)
        self.retrieval = RetrievalConfig.from_any(retrieval)

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
        )


__all__ = ["LLMConfig", "RetrievalConfig", "AppConfig"]