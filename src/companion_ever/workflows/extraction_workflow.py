"""workflows/extraction_workflow.py — 抽取 LangGraph StateGraph。

3 个节点的 StateGraph：
- load: 读最近一个已关闭 session 的对话
- extract: 调 LLM 拿到 facts + block_updates
- persist: 写 notes + 更新 blocks（per-fact/per-block 容错）

调用方：
    graph = build_extraction_graph(conversation_svc, note_svc, block_svc, llm_client)
    state = graph.invoke({"user_id": "alice", "persona_id": "舒舒"})
"""
from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.application.memory.note_service import NoteService
from companion_ever.application.persona.block_service import BlockService
from companion_ever.domain.enums import BlockType, NoteType
from companion_ever.infrastructure.ai.llm_client import LLMClient
from companion_ever.prompts import EXTRACTION_SYSTEM


class ExtractionState(TypedDict, total=False):
    """extraction workflow 的 state schema。"""

    user_id: str
    persona_id: str
    messages: list  # list[ConversationMessage]
    llm_result: dict[str, Any]  # {"facts": [...], "blocks": [...], "reasoning": "..."}
    facts_created: int
    blocks_updated: int
    errors: list[str]
    extraction_failed: bool  # LLM 调用就失败了（不是单条 fact 失败）


def _make_load_node(conversation_svc: ConversationService):
    """load 节点：读最近一个 session_close marker 之前的对话。"""
    def load_node(state: ExtractionState) -> ExtractionState:
        user_id = state["user_id"]
        persona_id = state["persona_id"]
        errors: list[str] = []

        markers = conversation_svc.get_last_n_session_closes(
            user_id=user_id, persona_id=persona_id, n=2,
        )
        if not markers:
            errors.append("no session_close marker found")
            return {**state, "messages": [], "errors": errors}

        last_marker = markers[-1]
        prev_marker = markers[-2] if len(markers) >= 2 else None

        messages = conversation_svc.get_messages_between(
            user_id=user_id,
            persona_id=persona_id,
            since_id=prev_marker.id if prev_marker else None,
            until_id=last_marker.id,
            limit=1000,
        )

        return {**state, "messages": messages, "errors": errors}
    return load_node


# persona_runtime 里允许 LLM 更新的字段。
# 其余的是自我强化的产物，不再让抽取维护。
_ALLOWED_RUNTIME_FIELDS = {"current_topic"}


def _render_existing_blocks(blocks: list) -> str:
    """把现有的 Block 渲染成供 LLM 参考的文本。

    没有现有内容时返回一个明确的空占位（不要留空白，否则 LLM 会当成
    "没事发生过"）。
    """
    if not blocks:
        return "\（还没有任何记录，这是首次抽取）"

    # 只告诉 LLM 需要"维护累积"的那两个 block。
    # persona_runtime 不给它看 —— 里面的 stable_mood_baseline / speech_style_reminder
    # 是从助手自己的输出里总结出来的（自我强化闭环），
    # 让 LLM 看到只会原样回归。
    labels = {
        "user_profile": "用户画像",
        "relationship_state": "关系进展",
    }
    parts: list[str] = []
    for b in blocks:
        label = labels.get(b.block_type.value)
        if label is None:
            continue
        fields = json.dumps(b.fields, ensure_ascii=False, indent=2)
        parts.append(f"### {label}\n```json\n{fields}\n```")
    if not parts:
        return "\（还没有任何记录，这是首次抽取）"
    return "\n\n".join(parts)


def _make_extract_node(llm_client: LLMClient, block_service: BlockService):
    """extract 节点：调 LLM 拿 facts + block_updates。"""
    def extract_node(state: ExtractionState) -> ExtractionState:
        messages = state.get("messages") or []
        if not messages:
            return {
                **state,
                "llm_result": {"facts": [], "blocks": [], "reasoning": ""},
            }

        from companion_ever.application.llm_json import parse_json_response

        # 【现有状态】——上次抽取的结果，必须跟 LLM 看到。
        # 否则 LLM 只会写"这次会话观察到的"，把之前学到的画像全部冲掉。
        existing_blocks = block_service.get_blocks(
            user_id=state["user_id"], persona_id=state["persona_id"],
        )
        existing_text = _render_existing_blocks(existing_blocks)

        formatted = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"## 现有状态（上次抽取的结果）\n\n"
                    f"{existing_text}\n\n"
                    f"## 待处理的对话（{len(messages)} 条）\n\n"
                    + _format_messages(messages)
                ),
            },
        ]
        # LLM 调用失败（超时 / 网络问题）不应该把异常抛给用户。
        # 这里记录错误并返回空结果，由上层决定怎么回滚。
        try:
            response_text = llm_client.chat(formatted)
        except Exception as e:  # noqa: BLE001
            return {
                **state,
                "llm_result": {"facts": [], "blocks": [], "reasoning": ""},
                "errors": [
                    *list(state.get("errors") or []),
                    f"LLM 调用失败: {type(e).__name__}: {e}",
                ],
                "extraction_failed": True,
            }

        data = parse_json_response(response_text) or {}
        return {
            **state,
            "llm_result": {
                "facts": data.get("facts", []),
                "blocks": data.get("blocks", []),
                "reasoning": data.get("reasoning", ""),
            },
        }
    return extract_node


def _make_persist_node(note_svc: NoteService, block_svc: BlockService):
    """persist 节点：写 notes + 更新 blocks。"""
    def persist_node(state: ExtractionState) -> ExtractionState:
        user_id = state["user_id"]
        persona_id = state["persona_id"]
        llm_result = state.get("llm_result") or {}
        errors: list[str] = list(state.get("errors") or [])

        facts_created = 0
        blocks_updated = 0

        for fact in llm_result.get("facts", []):
            try:
                note_svc.create_note(
                    user_id=user_id,
                    persona_id=persona_id,
                    type=NoteType(fact["type"]),
                    content=fact["content"],
                    source_quote=fact.get("source_quote"),
                    importance=float(fact.get("importance", 0.5)),
                )
                facts_created += 1
            except Exception as e:
                errors.append(f"create_note failed: {e}")

        for block_update in llm_result.get("blocks", []):
            try:
                block_type = BlockType(block_update["block_type"])
                fields = dict(block_update["fields"] or {})

                # persona_runtime 只保留 current_topic。
                # stable_mood_baseline / speech_style_reminder 是从助手自己的输出里
                # 总结出来的——那是自我强化闭环，开发上正应该从人设入设面
                # 派生，而不是从对话里学。
                if block_type == BlockType.PERSONA_RUNTIME:
                    fields = {
                        k: v for k, v in fields.items()
                        if k in _ALLOWED_RUNTIME_FIELDS
                    }
                    if not fields:
                        continue

                block_svc.update_block_fields(
                    user_id=user_id,
                    persona_id=persona_id,
                    block_type=block_type,
                    fields=fields,
                )
                blocks_updated += 1
            except Exception as e:
                errors.append(f"update_block failed: {e}")

        return {
            **state,
            "facts_created": facts_created,
            "blocks_updated": blocks_updated,
            "errors": errors,
        }
    return persist_node


def _has_messages(state: ExtractionState) -> str:
    """路由函数：有 messages 走 extract，否则 END。"""
    return "extract" if state.get("messages") else END


def _format_messages(messages: list) -> str:
    """把对话消息格式化成 LLM 可读文本。"""
    lines = []
    for m in messages:
        ts = m.created_at or ""
        lines.append(f"[{ts}] {m.role}: {m.content}")
    return "\n".join(lines)


def build_extraction_graph(
    conversation_service: ConversationService,
    note_service: NoteService,
    block_service: BlockService,
    llm_client: LLMClient,
):
    """构建 LangGraph StateGraph 并 compile。

    Returns:
        CompiledGraph

    拓扑：
        START → load → (有 messages ?) → extract → persist → END
                          ↓ (无 messages)
                          END
    """
    workflow = StateGraph(ExtractionState)

    workflow.add_node("load", _make_load_node(conversation_service))
    workflow.add_node("extract", _make_extract_node(llm_client, block_service))
    workflow.add_node("persist", _make_persist_node(note_service, block_service))

    workflow.add_edge(START, "load")

    workflow.add_conditional_edges(
        "load",
        _has_messages,
        {"extract": "extract", END: END},
    )

    workflow.add_edge("extract", "persist")
    workflow.add_edge("persist", END)

    return workflow.compile()


__all__ = ["ExtractionState", "build_extraction_graph"]