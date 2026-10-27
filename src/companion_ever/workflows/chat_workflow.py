"""workflows/chat_workflow.py — 单轮对话 + 关闭触发的 LangGraph StateGraph。

LangGraph 设计：
- StateGraph(ChatState) — 状态机定义
- chat 节点：拼 prompt + 调 LLM + log conversation_log
- should_close 路由函数：判断用户输入 /quit → 走 close 节点，否则 END
- close 节点：写 marker + 抽取（SessionCloseService）

调用方：
    graph = build_chat_graph(prompt_svc, conversation_svc, llm, session_close_svc)
    state = graph.invoke({"user_id": "alice", "persona_id": "舒舒", "user_input": "..."})
"""
from __future__ import annotations

import time as _time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from companion_ever.application.chat.conversation_service import ConversationService
from companion_ever.application.chat.prompt_service import PromptService
from companion_ever.application.chat.session_close_service import SessionCloseService
from companion_ever.infrastructure.ai.llm_client import LLMClient
from companion_ever.infrastructure.trace import trace_scope


class ChatState(TypedDict, total=False):
    """Chat workflow 的 state schema。"""

    user_id: str
    persona_id: str
    user_input: str
    response: str
    next_turn_index: int
    conversations_count: int
    close_report: object  # SessionCloseReport


def _make_chat_node(prompt_service: PromptService, conversation_service: ConversationService, llm_client: LLMClient):
    """构造 chat 节点。"""
    def chat_node(state: ChatState) -> ChatState:
        user_id = state["user_id"]
        persona_id = state["persona_id"]
        user_input = state["user_input"]

        with trace_scope("chat", user_id, persona_id) as tr:
            # 1. 拼 system prompt（自动只取当前 session 的对话历史——见 PromptService）
            _t0 = _time.perf_counter()
            prompt_result = prompt_service.build(
                user_id=user_id, persona_id=persona_id, query=user_input,
            )

            # 2. 拼最终 messages：system + 当前 session 历史 + 当前用户输入
            messages = list(prompt_result["messages"])
            messages.append({"role": "user", "content": user_input})

            # 3. 调 Chat LLM（仅生成回复，不做任何抽取/更新）
            response = llm_client.chat(messages)

            # 4. 写 conversation_log（user + assistant 各一行）
            next_turn_index = state.get("next_turn_index", 1)
            _t1 = _time.perf_counter()
            conversation_service.log_turn(
                user_id=user_id, persona_id=persona_id,
                turn_index=next_turn_index, role="user", content=user_input,
            )
            conversation_service.log_turn(
                user_id=user_id, persona_id=persona_id,
                turn_index=next_turn_index, role="assistant", content=response,
            )
            tr.step(
                "db.log_turn",
                ms=(_time.perf_counter() - _t1) * 1000,
                turn_index=next_turn_index,
            )

            return {
                **state,
                "response": response,
                "next_turn_index": next_turn_index + 1,
                "conversations_count": state.get("conversations_count", 0) + 1,
            }
    return chat_node


def _should_close(state: ChatState) -> str:
    """路由函数：用户输入 /quit 走 close 节点，否则 END。"""
    if state.get("user_input") == "/quit":
        return "close"
    return END


def _make_close_node(session_close_service: SessionCloseService):
    """构造 close 节点。"""
    def close_node(state: ChatState) -> ChatState:
        # 抽取链路的 trace 在 SessionCloseService.close_session 内部开启
        close_report = session_close_service.close_session(
            user_id=state["user_id"],
            persona_id=state["persona_id"],
        )
        return {
            **state,
            "close_report": close_report,
        }
    return close_node


def build_chat_graph(
    prompt_service: PromptService,
    conversation_service: ConversationService,
    llm_client: LLMClient,
    session_close_service: SessionCloseService,
):
    """构建 LangGraph StateGraph 并 compile。

    拓扑：
        START → chat → (should_close 路由) → close → END
                                       ↓
                                       END（普通对话结束）
    """
    workflow = StateGraph(ChatState)

    workflow.add_node("chat", _make_chat_node(prompt_service, conversation_service, llm_client))
    workflow.add_node("close", _make_close_node(session_close_service))

    workflow.add_edge(START, "chat")
    workflow.add_edge("close", END)

    workflow.add_conditional_edges(
        "chat",
        _should_close,
        {
            "close": "close",
            END: END,
        },
    )

    return workflow.compile()


__all__ = ["ChatState", "build_chat_graph"]
