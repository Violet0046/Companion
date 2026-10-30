"""专门更新 persona_basics 的 service。"""
from __future__ import annotations

import json

from pydantic import BaseModel

from companion_ever.services.llm_json import parse_json_response
from companion_ever.services.note_service import NoteService
from companion_ever.llm.llm_client import LLMClient
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
        self.note_service.set_persona_definition(
            user_id=user_id,
            persona_id=persona_id,
            content=new_content,
        )

        return new_fields

    def _get_current(self, user_id: str, persona_id: str) -> dict:
        """读当前 persona_basics fields（不存在时返回空 dict）."""
        row = self.note_service.get_persona_definition(user_id,persona_id)
        if row is None:
            return {}
        try:
            return json.loads(row['content'])
        except (ValueError,TypeError):
            return {'_raw': row['content']}

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