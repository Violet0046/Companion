"""LangGraph 适配层。抽取规则只存在于 ExtractionService。"""
from typing import Any, TypedDict
from langgraph.graph import END, START, StateGraph
from companion_ever.services.extraction_service import ExtractionService


class ExtractionState(TypedDict, total=False):
    user_id: str
    persona_id: str
    messages: list
    llm_result: dict[str, Any]
    facts_created: int
    blocks_updated: int
    errors: list[str]
    extraction_failed: bool
    job_id: str
    job_status: str


def build_extraction_graph(conversation_service, note_service, block_service, llm_client):
    service = ExtractionService(conversation_service, note_service, block_service, llm_client)
    workflow = StateGraph(ExtractionState)
    workflow.add_node("extract", service.run)
    workflow.add_edge(START, "extract")
    workflow.add_edge("extract", END)
    return workflow.compile()
