"""廉价不变量检查。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Violation:
    """一条被违反的不变量。"""

    rule: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message}


def strict_mode() -> bool:
    """严格模式：违反不变量时抛异常（开发期用）。"""
    return os.environ.get("COMPANION_TRACE_STRICT", "0").strip().lower() in (
        "1", "true", "on", "yes",
    )


def prompt_budget_chars() -> int:
    """prompt 字符预算上限（默认 40000 字符 ≈ 27K tokens）。"""
    raw = os.environ.get("COMPANION_PROMPT_BUDGET_CHARS", "40000")
    try:
        return int(raw)
    except ValueError:
        return 40000


# ============ 对话期不变量 ============

def check_prompt_invariants(
    messages: list[dict[str, Any]],
    system_prompt: str,
    retrieved_count: int = 0,
    last_db_message: Optional[str] = None,
    total_notes: int = 0,
) -> list[Violation]:
    """检查一次对话的 prompt 组装是否正常。

    Args:
        messages: 实际发给 LLM 的 messages 列表
        system_prompt: 渲染出的 system prompt
        retrieved_count: 本轮召回到的 note 数
        last_db_message: 本轮之前 DB 里最新一条消息的 content
            （用来验证历史是不是"最近"的；本轮输入不在 messages 里，
            所以不是 DB 里绝对最新的那条）
        total_notes: 该 persona 的记忆总数（0 = 还没有记忆，此时召回为空不算违规）

    Returns:
        违反的不变量列表（空 = 全部通过）
    """
    violations: list[Violation] = []

    # 1. 历史不能重复出现在 system prompt 里
    if "## 对话历史" in system_prompt:
        violations.append(Violation(
            rule="history_not_duplicated",
            message="system prompt 里出现了 '## 对话历史' —— 对话历史被送了两遍",
        ))

    # 2. 总长度在预算内
    total_chars = len(system_prompt) + sum(
        len(str(m.get("content", ""))) for m in messages[1:]
    )
    budget = prompt_budget_chars()
    if total_chars > budget:
        violations.append(Violation(
            rule="prompt_within_budget",
            message=f"prompt 共 {total_chars} 字符，超过预算 {budget}",
        ))

    # 3. 历史必须是"最近的"，而不是"最早的"
    #    —— 曾经的 bug：get_messages_between 无边界时 ORDER BY id ASC，
    #       取到的是最早的 N 条，模型完全看不到最近的对话
    #    注意：本轮输入不在 messages 里（由 system 的「当前这一轮」承载），
    #    所以比对对象是"本轮之前最新的那条"，不是 DB 里绝对最新的那条。
    if last_db_message:
        tail = "".join(
            str(m.get("content", "")) for m in messages[-3:]
        )
        probe = last_db_message[:60].strip()
        if probe and probe not in tail:
            violations.append(Violation(
                rule="history_is_recent",
                message=(
                    "prompt 里的最后几条历史不包含本轮之前最新的那条消息 —— "
                    "很可能取到了最早的 N 条而不是最近的 N 条"
                ),
            ))

    # Empty recall is valid for unrelated queries; retrieval errors must be reported explicitly.
    return violations


# ============ 抽取期不变量 ============

def check_extraction_invariants(
    messages_processed: int,
    facts_count: int,
    note_ids: list[str],
) -> list[Violation]:
    """检查一次抽取的结果是否正常。"""
    violations: list[Violation] = []

    # 5. 抽取的 facts 数量不应该爆炸（LLM 幻觉批量灌入）
    if messages_processed > 0 and facts_count > messages_processed * 2:
        violations.append(Violation(
            rule="facts_not_exploded",
            message=(
                f"{messages_processed} 条消息抽出了 {facts_count} 条 fact"
                f"（阈值 {messages_processed * 2}）—— 可能 LLM 在幻觉"
            ),
        ))

    # 6. 写入的 note id 不应重复
    if len(set(note_ids)) != len(note_ids):
        violations.append(Violation(
            rule="notes_unique",
            message="本次抽取写入了重复的 note id",
        ))

    return violations


__all__ = [
    "Violation",
    "check_prompt_invariants",
    "check_extraction_invariants",
    "strict_mode",
    "prompt_budget_chars",
]
