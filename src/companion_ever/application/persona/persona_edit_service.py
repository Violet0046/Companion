"""application/persona/persona_edit_service.py — 专门更新 persona_basics 的 service。

职责：
- 读当前 persona_basics JSON
- 调 LLM 根据用户指令重写
- 写回（idempotent）

触发场景：
- 用户主动命令（CLI: companion-persona-edit）
- 未来 web 端的"编辑人设"按钮

设计要点：
- 不动 notes 表和 memory_blocks（那些由 extraction_service 处理）
- 只管 persona_basics JSON
- LLM 调用规则严格：只根据用户指令修改 persona_basics

变化：
- PERSONA_EDIT_SYSTEM_PROMPT → 从 prompts.PERSONA_EDIT_SYSTEM 加载
- 内联 _clean_response → application.llm_json.parse_json_response
"""
from __future__ import annotations

import json

from pydantic import BaseModel

from companion_ever.application.llm_json import parse_json_response
from companion_ever.application.memory.note_service import NoteService
from companion_ever.domain.enums import NoteType
from companion_ever.infrastructure.ai.llm_client import LLMClient
from companion_ever.prompts import PERSONA_EDIT_SYSTEM


# === LLM 输出 Schema ===

class PersonaBasicsUpdate(BaseModel):
    """LLM 重新生成的 persona_basics 内容."""

    fields: dict  # 完整的 fields dict
    reasoning: str = ""


class PersonaEditService:
    """专门更新 persona_basics 的 service。"""

    def __init__(
        self,
        note_service: NoteService,
        llm_client: LLMClient,
    ):
        self.note_service = note_service
        self.llm_client = llm_client

    def edit(
        self,
        *,
        user_id: str,
        persona_id: str,
        instruction: str,
    ) -> dict:
        """根据用户指令更新 persona_basics。

        Args:
            user_id, persona_id: 用户 + 人设
            instruction: 用户的修改指令（如"让她更直接一点，不要撒娇"）

        Returns:
            更新后的完整 fields dict（同时写入 DB）
        """
        # 1. 读当前 persona_basics
        current = self._get_current(user_id, persona_id)

        # 2. 调 LLM 生成新版
        new_fields = self._call_llm_edit(current, instruction)

        # 3. 写回（idempotent）
        new_content = json.dumps(new_fields, ensure_ascii=False, indent=2)
        self.note_service.create_persona_basics_note(
            user_id=user_id,
            persona_id=persona_id,
            content=new_content,
        )

        return new_fields

    def _get_current(self, user_id: str, persona_id: str) -> dict:
        """读当前 persona_basics fields（不存在时返回空 dict）."""
        notes = self.note_service.find_notes(
            user_id=user_id,
            persona_id=persona_id,
            types=[NoteType.PERSONA_BASICS],
        )
        if not notes:
            return {}
        content = notes[0].content
        if isinstance(content, dict):
            return content
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}

    def _call_llm_edit(
        self,
        current: dict,
        instruction: str,
    ) -> dict:
        """调 LLM 生成新的 persona_basics fields."""
        prompt = PERSONA_EDIT_SYSTEM.format(
            current=json.dumps(current, ensure_ascii=False, indent=2),
            instruction=instruction,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "按上面的指令修改人设"},
        ]

        response_text = self.llm_client.chat(messages)
        data = parse_json_response(response_text)
        if data is None:
            return current
        return data.get("fields", current)


__all__ = ["PersonaBasicsUpdate", "PersonaEditService"]