"""Summary workflow; SQLite jobs retain all execution ownership and recovery."""
from typing import Any,TypedDict
from langgraph.graph import StateGraph,START,END
from companion_ever.runtime.tasks import checkpoint


class SummaryState(TypedDict,total=False):
    job:dict
    deadline:float
    model_calls:int
    repaired:bool
    checkpoint:Any
    expected_cursor:int
    previous:dict
    referenced:set
    impacted:set
    available:dict
    before_blocks:dict
    events:list
    model_messages:list
    response:Any
    document:dict
    origin:str
    validation_error:Any
    next:str


def build_summary_graph(service):
    graph=StateGraph(SummaryState)
    for name in ('load_input','decide','reuse','clear','model','validate','repair','commit'):
        method=getattr(service,'_validate_output' if name=='validate' else '_'+name)
        def node(state,method=method,name=name):
            checkpoint('graph.'+name);service.check_budget(state)
            result=method(state)
            return result
        graph.add_node(name,node)
    graph.add_edge(START,'load_input');graph.add_edge('load_input','decide')
    graph.add_conditional_edges('decide',lambda state:state['next'],{name:name for name in ('reuse','clear','model')})
    for name in ('reuse','clear','model'):graph.add_edge(name,'validate')
    graph.add_conditional_edges('validate',lambda state:state['next'],{'repair':'repair','commit':'commit'})
    graph.add_edge('repair','model');graph.add_edge('commit',END)
    return graph.compile()
