"""记忆抽取的唯一实现：读取会话、验证模型输出、写入事实和画像。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from companion_ever.services.llm_json import parse_json_response
from companion_ever.domain.enums import BlockType, NoteType
from companion_ever.prompts import EXTRACTION_SYSTEM
from companion_ever.utils.time import parse_sqlite_ts


@dataclass
class ExtractionReport:
    facts_created: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    session_start: datetime | None = None
    session_end: datetime | None = None
    errors: list[str] = field(default_factory=list)


def report_from_state(state: dict) -> ExtractionReport:
    messages = state.get("messages") or []
    return ExtractionReport(
        facts_created=state.get("facts_created", 0),
        blocks_updated=state.get("blocks_updated", 0),
        conversations_processed=len(messages),
        session_start=parse_sqlite_ts(messages[0].created_at) if messages else None,
        session_end=parse_sqlite_ts(messages[-1].created_at) if messages else None,
        errors=list(state.get("errors") or []),
    )


class ExtractionService:
    def __init__(self, conversation_service, note_service, block_service, llm_client):
        self.conversation_service = conversation_service
        self.note_service = note_service
        self.block_service = block_service
        self.llm_client = llm_client

    def extract_last_closed_session(self, user_id: str, persona_id: str) -> ExtractionReport:
        return report_from_state(self.run({"user_id": user_id, "persona_id": persona_id}))

    def run(self, state: dict) -> dict:
        user_id, persona_id = state["user_id"], state["persona_id"]
        result = {**state, "messages": [], "facts_created": 0, "blocks_updated": 0,
                  "errors": [], "extraction_failed": False}
        markers = self.conversation_service.get_last_n_session_closes(
            user_id=user_id, persona_id=persona_id, n=2)
        if not markers:
            return result
        messages = self.conversation_service.get_messages_between(
            user_id=user_id, persona_id=persona_id,
            since_id=markers[-2].id if len(markers) > 1 else None,
            until_id=markers[-1].id, limit=1000)
        result["messages"] = messages
        if not messages:
            return result
        blocks = self.block_service.get_blocks(user_id=user_id, persona_id=persona_id)
        existing = {b.block_type.value: b.fields for b in blocks
                    if b.block_type != BlockType.PERSONA_RUNTIME}
        transcript = "\n".join(f"[{m.created_at or ''}] {m.role}: {m.content}" for m in messages)
        prompt = [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content":
                "## 现有状态（上次抽取的结果）\n" + json.dumps(existing, ensure_ascii=False)
                + f"\n\n## 待处理的对话（{len(messages)} 条）\n{transcript}"},
        ]
        try:
            data = parse_json_response(self.llm_client.chat(prompt))
            if not isinstance(data, dict) or not all(
                isinstance(data.get(k), list) for k in ("facts", "blocks")
            ):
                raise ValueError("抽取输出必须包含 facts 和 blocks 数组")
        except Exception as exc:
            result["errors"].append(f"抽取失败: {type(exc).__name__}: {exc}")
            result["extraction_failed"] = True
            return result
        result["llm_result"] = data
        for fact in data["facts"]:
            try:
                note_type = NoteType(fact["type"])
                if note_type.value.startswith("persona_"):
                    raise ValueError("会话抽取不能修改角色设定")
                _, created = self.note_service.remember(
                    user_id, persona_id, note_type, fact["content"],
                    source_quote=fact.get("source_quote"),
                    importance=float(fact.get("importance", 0.5)))
                result["facts_created"] += int(created)
            except (ValueError, TypeError, KeyError) as exc:
                result["errors"].append(f"fact 无效: {exc}")
        for update in data["blocks"]:
            try:
                block_type = BlockType(update["block_type"])
                fields = dict(update["fields"])
                if block_type == BlockType.PERSONA_RUNTIME:
                    fields = {k: v for k, v in fields.items() if k == "current_topic"}
                if fields:
                    self.block_service.update_block_fields(user_id, persona_id, block_type, fields)
                    result["blocks_updated"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                result["errors"].append(f"block 无效: {exc}")
        # 部分失败也保留重试机会；重复事实由 remember 幂等写入。
        result["extraction_failed"] = bool(result["errors"])
        return result
