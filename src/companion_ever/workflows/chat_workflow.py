"""workflows/chat_workflow.py — 单轮对话 + 关闭触发的 LangGraph StateGraph。

LangGraph 设计：
- StateGraph(ChatState) — 状态机定义
- **先路由**：`/quit` 直接进 close 节点，**不经过 chat 节点**（不调 LLM）
- chat 节点：拼 prompt + 调 LLM + log conversation_log
- should_close 路由函数：判断用户输入 /quit → close，否则 → chat
- close 节点：写 marker + 抽取（SessionCloseService）

调用方：
    graph = build_chat_graph(prompt_svc, conversation_svc, llm, session_close_svc)
    state = graph.invoke({"user_id": "alice", "persona_id": "舒舒", "user_input": "..."})
"""
from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from companion_ever.services.conversation_service import ConversationService
from companion_ever.services.prompt_service import PromptService
from companion_ever.services.session_close_service import SessionCloseService
from companion_ever.llm.llm_client import LLMClient


class ChatState(TypedDict, total=False):
    """Chat workflow 的 state schema。"""

    user_id: str
    persona_id: str
    user_input: str
    response: str
    next_turn_index: int
    conversations_count: int
    close_report: object  # SessionCloseReport


def _make_chat_node(
    prompt_service: PromptService,
    conversation_service: ConversationService,
    llm_client: LLMClient,
    session_close_service: SessionCloseService | None = None,
    idle_session_minutes: int = 120,
):
    """图仅负责路由；聊天逻辑由 ChatService 维护。"""
    from companion_ever.services.chat_service import ChatService
    service = ChatService(prompt_service, conversation_service, llm_client,
                          session_close_service, idle_session_minutes)

    def chat_node(state: ChatState) -> ChatState:
        response = service.chat(state["user_id"], state["persona_id"], state["user_input"])
        return {
            **state,
            "response": response,
            "next_turn_index": service._next_turn_index(state["user_id"], state["persona_id"]),
            "conversations_count": state.get("conversations_count", 0) + 1,
        }
    return chat_node


def _should_close(state: ChatState) -> str:
    """路由函数：用户输入 /quit 走 close 节点，否则 END。"""
    if state.get("user_input") == "/quit":
        return "close"
    return "chat"


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
    idle_session_minutes: int = 120,
):
    """构建 LangGraph StateGraph 并 compile。

    拓扑：
        START → (should_close 路由) → chat → END
                              ↓
                              close → END（/quit，不调 LLM）
    """
    workflow = StateGraph(ChatState)

    workflow.add_node(
        "chat",
        _make_chat_node(
            prompt_service, conversation_service, llm_client,
            session_close_service, idle_session_minutes,
        ),
    )
    workflow.add_node("close", _make_close_node(session_close_service))

    # 先路由：/quit 直接进 close 节点，不经过 chat（因此不会调 LLM）
    workflow.add_conditional_edges(
        START,
        _should_close,
        {"close": "close", "chat": "chat"},
    )

    workflow.add_edge("chat", END)
    workflow.add_edge("close", END)

    return workflow.compile()


__all__ = ["ChatState", "build_chat_graph"]
