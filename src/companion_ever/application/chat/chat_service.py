"""application/chat/chat_service.py — 对话主流程。

职责：
- 拼装 system prompt + history（通过 prompt_service）
- 加上当前用户输入
- 调 LLM 出回复（剥掉 <think>...</think> 思考块）
- 记录对话

不做（由其他 service 做）：
- 实时抽取（后台 extraction_service）
- Wiki 检索（retrieval_service，已通过 prompt_service 集成）
- 人设更新（persona_edit_service）

变化：
- 使用 application.llm_json.strip_thinking 替代内联 THINK_PATTERN
- chat() 现在把 user_input 作为 query 传给 prompt_service.build（修复检索死路径）
- 全程包在 trace_scope("chat", ...) 里 —— 这一轮的链路细节会落盘
"""
from __future__ import annotations

import time as _time
from collections.abc import Iterator

from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.application.chat.assistant_reply import split_assistant_reply
from companion_ever.utils.text import strip_invisible
from companion_ever.application.chat.prompt_service import PromptService

from companion_ever.infrastructure.ai.llm_client import LLMClient
from companion_ever.infrastructure.trace import trace_scope


class ChatService:
    """对话主流程（单一职责：拼 prompt + 调 LLM + log）。"""

    def __init__(
        self,
        prompt_service: PromptService,
        conversation_service: ConversationService,
        llm_client: LLMClient,
    ):
        self.prompt_service = prompt_service
        self.conversation_service = conversation_service
        self.llm_client = llm_client

    def chat(self, user_id: str, persona_id: str, user_input: str) -> str:
        """单轮对话：拼 prompt + 调 LLM + log。

        Args:
            user_id: 用户 id
            persona_id: persona id
            user_input: 用户当前输入

        Returns:
            LLM 的回复文本（原始，包含 <think> 块）
            — 想看干净版用 application.llm_json.strip_thinking

        Raises:
            PermissionError: user 不拥有 persona（由 prompt_service.build 触发）
        """
        with trace_scope("chat", user_id, persona_id) as tr:
            # 1. 拼 prompt + 历史（把 user_input 作为 query 触发检索）
            result = self.prompt_service.build(user_id, persona_id, query=user_input)
            messages = list(result["messages"])  # 复制

            # 2. 算新的 turn_index（现有最大 + 1）
            next_turn = self._next_turn_index(user_id, persona_id)

            # 3. 加上用户当前输入
            messages.append({"role": "user", "content": user_input})

            # 4. 调 LLM
            response_text = self.llm_client.chat(messages)

            # 5. 记录对话（user + assistant 一轮）
            # 日志里剥掉 <think> 块（保持干净，便于后续 LLM 消费）
            _t0 = _time.perf_counter()
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
            tr.step(
                "db.log_turn",
                ms=(_time.perf_counter() - _t0) * 1000,
                turn_index=next_turn,
            )

            return response_text

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
            _t0 = _time.perf_counter()
            self.conversation_service.log_turn(
                user_id=user_id,
                persona_id=persona_id,
                turn_index=next_turn,
                role="user",
                content=user_input,
            )
            self.conversation_service.log_turn(
                user_id=user_id,
                persona_id=persona_id,
                turn_index=next_turn,
                role="assistant",
                content=_clean_assistant_reply(response_text),
            )
            tr.step(
                "db.log_turn",
                ms=(_time.perf_counter() - _t0) * 1000,
                turn_index=next_turn,
            )


__all__ = ["ChatService"]
