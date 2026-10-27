"""domain/enums.py — 域枚举类型。

NoteType: 14 种笔记分类（9 用户侧 + 5 人设侧）
BlockType: 3 种 memory block 类型
"""
from __future__ import annotations

from enum import Enum


class NoteType(str, Enum):
    """笔记的 14 种类型。

    用户侧（9 种）—— 关于用户的观察/事实:
        PROFILE_FACT, EVENT, PREFERENCE, PROMISE, MILESTONE,
        INSIDE_JOKE, CALLBACK, SENSITIVE, OBSERVATION

    人设侧（5 种）—— 关于助手人设的演化:
        PERSONA_FACT, PERSONA_BASICS, PERSONA_PREFERENCE,
        PERSONA_MEMORY, PERSONA_ARC
    """

    # 用户侧
    PROFILE_FACT = "profile_fact"
    EVENT = "event"
    PREFERENCE = "preference"
    PROMISE = "promise"
    MILESTONE = "milestone"
    INSIDE_JOKE = "inside_joke"
    CALLBACK = "callback"
    SENSITIVE = "sensitive"
    OBSERVATION = "observation"

    # 人设侧
    PERSONA_FACT = "persona_fact"
    PERSONA_BASICS = "persona_basics"
    PERSONA_PREFERENCE = "persona_preference"
    PERSONA_MEMORY = "persona_memory"
    PERSONA_ARC = "persona_arc"


class BlockType(str, Enum):
    """3 种 memory block 类型（注入 system prompt 的运行时状态）。"""

    PERSONA_RUNTIME = "persona_runtime"
    USER_PROFILE = "user_profile"
    RELATIONSHIP_STATE = "relationship_state"


__all__ = ["NoteType", "BlockType"]