"""对话主流程。"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from companion_ever.services.conversation_service import ConversationService
from companion_ever.services.assistant_reply import split_assistant_reply
from companion_ever.utils.text import strip_invisible
from companion_ever.services.prompt_service import PromptService

from companion_ever.llm.llm_client import LLMClient
from companion_ever.trace import trace_scope, current_trace


class ChatService:
    """对话主流程（单一职责：拼 prompt + 调 LLM + log）。"""

    def __init__(
        self,
        prompt_service: PromptService,
        conversation_service: ConversationService,
        llm_client: LLMClient,
        session_close_service=None,
        idle_session_minutes: int = 120,
    ):
        self.prompt_service = prompt_service
        self.conversation_service = conversation_service
        self.llm_client = llm_client
        # 自动管理 session 边界（可选，为不提供时保持兼容）
        self.session_close_service = session_close_service
        self.idle_session_minutes = idle_session_minutes

    def chat(self, user_id: str, persona_id: str, user_input: str) -> str:
        """单轮对话：拼 prompt + 调 LLM + log。

        Args:
            user_id: 用户 id
            persona_id: persona id
            user_input: 用户当前输入

        Returns:
            LLM 的回复文本（原始，包含 <think> 块）
            — 想看干净版用 services.llm_json.strip_thinking

        Raises:
            PermissionError: user 不拥有 persona（由 prompt_service.build 触发）
        """
        with trace_scope("chat", user_id, persona_id) as tr:
            # 0. 空闲超时：先自动关上一个 session（不依赖用户按 /quit）
            self._maybe_close_stale(user_id, persona_id)

            # 1. 拼 prompt + 历史（把 user_input 作为 query 触发检索）
            result = self.prompt_service.build(user_id, persona_id, query=user_input)
            messages = list(result["messages"])  # 复制

            # 2. 算新的 turn_index（现有最大 + 1）
            next_turn = self._next_turn_index(user_id, persona_id)

            # 3. 加上用户当前输入
            messages.append({"role": "user", "content": user_input})

            # 4. 调 LLM
            response_text = self.llm_client.chat(messages)

            self._save_reply(user_id, persona_id, user_input, response_text, next_turn)
            return response_text

    def _save_reply(self, user_id: str, persona_id: str, user_input: str,
                    response_text: str, next_turn: int) -> None:
        """同步与流式回复共用同一个写入入口。"""
        self.conversation_service.log_turn(
            user_id=user_id,
            persona_id=persona_id,
            turn_index=next_turn,
            role="user",
            content=strip_invisible(user_input),
        )
        body, think = split_assistant_reply(
            response_text,
            getattr(self.llm_client, "last_reasoning", None),
        )
        msg_id = self.conversation_service.log_turn(
            user_id=user_id,
            persona_id=persona_id,
            turn_index=next_turn,
            role="assistant",
            content=body,
        )
        if think:
            # 思考过程另存到 thinking_log（给用户看，不进 prompt）
            self.conversation_service.log_thinking(
                message_id=msg_id,
                user_id=user_id,
                persona_id=persona_id,
                turn_index=next_turn,
                content=think,
            )
        tr = current_trace()
        if tr is not None:
            tr.step("db.log_turn", turn_index=next_turn)

    def _maybe_close_stale(self, user_id: str, persona_id: str) -> None:
        """空闲超时就自动关上一个 session。"""
        if self.session_close_service is None:
            return
        try:
            self.session_close_service.close_if_stale(
                user_id, persona_id, self.idle_session_minutes,
            )
        except Exception:
            logging.getLogger(__name__).exception("自动关闭旧会话失败")

    def _next_turn_index(self, user_id: str, persona_id: str) -> int:
        """算下一个 turn_index（现有最大 + 1）。

        用 SQL 的 MAX(turn_index) 而不是"拉全部消息再取 max"——
        对话几千条之后后者会明显拖慢每一轮。
        """
        return self.conversation_service.get_max_turn_index(user_id, persona_id) + 1

    def stream_chat(
        self,
        user_id: str,
        persona_id: str,
        user_input: str,
    ) -> Iterator[str]:
        """流式对话：返回 LLM 的增量输出（含 <think> 块，用户可见）。"""
        with trace_scope("chat", user_id, persona_id) as tr:
            self._maybe_close_stale(user_id, persona_id)
            result = self.prompt_service.build(user_id, persona_id, query=user_input)
            messages = list(result["messages"])
            messages.append({"role": "user", "content": user_input})

            next_turn = self._next_turn_index(user_id, persona_id)

            # 流式收集（包含 <think> 块）
            response_parts = []
            for chunk in self.llm_client.stream_chat(messages):
                response_parts.append(chunk)
                yield chunk

            # 流结束后 log 对话
            response_text = "".join(response_parts)
            self._save_reply(user_id, persona_id, user_input, response_text, next_turn)


__all__ = ["ChatService"]
