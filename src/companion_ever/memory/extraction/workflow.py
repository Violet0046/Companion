"""The memory workflow: bounded model/tool loop, validation and atomic commit."""
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from companion_ever.runtime.tasks import checkpoint


class ExtractionState(TypedDict, total=False):
    user_id: str
    persona_id: str
    conversation_id: int
    job_id: str
    job_status: str
    messages: list
    llm_result: dict
    facts_created: int
    notes_updated: int
    blocks_updated: int
    note_ids: dict
    errors: list
    extraction_failed: bool
    job: dict
    case: dict
    snapshots: dict
    tools: Any
    model_messages: list
    response: Any
    tool_requests: list
    validation_error: Any
    next: str
    deadline: float
    model_calls: int
    tool_rounds: int
    tool_calls: int
    repaired: bool
    search_required: bool


def build_extraction_graph(service):
    """Compile processing steps; service.run owns durable execution and leases."""
    graph = StateGraph(ExtractionState)
    for name in ('load_input', 'prefetch', 'model', 'validate', 'tools', 'repair', 'commit'):
        method = getattr(service, '_' + name)
        def node(state, method=method, name=name):
            checkpoint('graph.' + name)
            service.check_budget(state)
            result = method(state)
            return result
        graph.add_node(name, node)
    graph.add_edge(START, 'load_input')
    graph.add_conditional_edges('load_input', lambda state: state['next'], {'prefetch': 'prefetch', 'commit': 'commit'})
    graph.add_edge('prefetch', 'model')
    graph.add_edge('model', 'validate')
    graph.add_conditional_edges('validate', lambda state: state['next'],
                                {'tools': 'tools', 'repair': 'repair', 'commit': 'commit'})
    graph.add_edge('tools', 'model')
    graph.add_edge('repair', 'model')
    graph.add_edge('commit', END)
    return graph.compile()
