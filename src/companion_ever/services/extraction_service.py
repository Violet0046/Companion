"""Durable memory execution and the business operations used by its LangGraph."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from companion_ever.storage.memory_job_repo import MemoryJobRepo
from companion_ever.services.memory_draft import Draft, draft_messages, validate_draft
from companion_ever.services.memory_tools import MemoryTools, ToolRequest, note_payload, tool_specs
from companion_ever.services.llm_json import parse_json_response
from companion_ever.services.structured_generation import correction_messages, record_validation
from companion_ever.services.memory_writer import MemoryWriter
from companion_ever.services.task_errors import can_retry, describe_error, failure_kind, RetryableTaskError
from companion_ever.services.task_runtime import runtime_for, checkpoint, TaskInterrupted
from companion_ever.services.retrieval_service import RetrievalService
from companion_ever.services.memory_search import MemorySearch
from companion_ever.config import RetrievalConfig
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.trace import trace_scope,current_trace
from companion_ever.storage.task_repo import TaskRepo
import json
import time
from pydantic import TypeAdapter


@dataclass
class ExtractionReport:
    facts_created: int = 0
    notes_updated: int = 0
    blocks_updated: int = 0
    conversations_processed: int = 0
    session_start: datetime | None = None
    session_end: datetime | None = None
    errors: list[str] = field(default_factory=list)
    job_id: str | None = None
    job_status: str | None = None


def report_from_state(state: dict) -> ExtractionReport:
    messages = state.get("messages") or []
    return ExtractionReport(
        facts_created=state.get("facts_created", 0),
        notes_updated=state.get('notes_updated',0),
        blocks_updated=state.get("blocks_updated", 0),
        conversations_processed=len(messages),
        session_start=parse_sqlite_ts(messages[0].created_at) if messages else None,
        session_end=parse_sqlite_ts(messages[-1].created_at) if messages else None,
        errors=list(state.get("errors") or []),
        job_id=state.get("job_id"), job_status=state.get("job_status"),
    )


class ExtractionService:
    MAX_MODEL_CALLS = 4
    MAX_TOOL_ROUNDS = 2
    MAX_TOOL_CALLS = 4
    MAX_CANDIDATES = 24
    MAX_CANDIDATE_CHARS = 8000
    MAX_TOOL_RESULT_CHARS = 16000
    MAX_INPUT_CHARS = 60000

    def __init__(self, conversation_service, note_service, block_service, llm_client, timeout=120.0, tz_offset_hours=8,thinking=False):
        self.conversation_service = conversation_service
        self.note_service = note_service
        self.block_service = block_service
        self.llm_client = llm_client
        self.timeout = timeout
        self.tz_offset_hours = tz_offset_hours
        self.conn = note_service.note_repo.conn
        self.jobs = MemoryJobRepo(self.conn)
        self.runtime=runtime_for(self.conn)
        self.thinking=thinking
        self.retrieval=RetrievalService(self.note_service.note_repo,self.note_service.authorizer,RetrievalConfig())
        self.response_schema=TypeAdapter(Draft | ToolRequest).json_schema()
        from companion_ever.workflows.extraction_workflow import build_extraction_graph
        self.graph=build_extraction_graph(self)

    def extract_last_closed_session(self, user_id: str, persona_id: str) -> ExtractionReport:
        return report_from_state(self.run({"user_id": user_id, "persona_id": persona_id}))

    def run(self, state: dict) -> dict:
        try:
            with self.runtime.slot():
                return self._run(state)
        except TaskInterrupted:
            return {**state,'messages':[],'facts_created':0,'notes_updated':0,'blocks_updated':0,'errors':[],
                    'job_status':'queued','extraction_failed':False}

    def _run(self,state):
        user_id, persona_id = state["user_id"], state["persona_id"]
        result = {**state, "messages": [], "facts_created": 0, "blocks_updated": 0,
                  "notes_updated":0, "note_ids":{}, "errors": [], "extraction_failed": False,
                  'model_calls':0,'tool_rounds':0,'tool_calls':0}
        job = self.jobs.enqueue(user_id, persona_id)
        if not job:
            return result
        result.update(job_id=job['id'], job_status=job['status'])
        claimed = self.jobs.claim(job['id'],owner_id=self.runtime.owner_id,lease_seconds=self.runtime.lease_seconds)
        if not claimed:
            job = self.jobs.get(job['id'])
            result['job_status'] = job['status']
            result['attempt_trace_id']=TaskRepo(self.conn).latest_trace('memory',job['id'])
            if job['last_error']:
                result['errors'] = [job['last_error']]
            return result
        token = claimed['lease_token']
        parent=current_trace()
        with trace_scope('memory_attempt',user_id,persona_id,job_id=job['id'],attempt_id=token,
                         parent_trace_id=state.get('parent_trace_id') or (parent.trace_id if parent else None)) as trace:
            result['attempt_trace_id']=trace.trace_id
            if getattr(trace,'_store',None):
                TaskRepo(self.conn).event(token,'trace',{'trace_id':trace.trace_id})
            try:
                with self.runtime.execute('memory',self.jobs,claimed):
                    initial={**result,'job':claimed,'deadline':time.monotonic()+self.timeout*3,
                             'model_calls':0,'tool_rounds':0,'tool_calls':0,'repaired':False}
                    public=set(result)|{'llm_result','model_calls','tool_rounds','tool_calls'}
                    for update in self.graph.stream(initial,stream_mode='updates',config={'recursion_limit':32}):
                        for values in update.values():
                            result.update({k:v for k,v in values.items() if k in public})
                    return result
            except TaskInterrupted as exc:
                current=self.jobs.get(job['id'])
                trace.finish('interrupted',str(exc))
                result.update(job_status=current['status'],errors=[],extraction_failed=False)
                return result
            except Exception as exc:
                error=describe_error(exc)
                trace.finish('error',error)
                self.jobs.fail(job['id'], token, error, retryable=can_retry(exc),failure_kind=failure_kind(exc))
                result.update(facts_created=0, notes_updated=0, note_ids={}, blocks_updated=0, extraction_failed=True, job_status='failed', errors=[error])
                return result

    def check_budget(self,state):
        if time.monotonic()>=state['deadline']:
            raise TimeoutError('记忆整理及工具交互超过本次任务的总时限')

    def _load_input(self,state):
        checkpoint('load_input')
        job=state['job']
        user_id, persona_id = job['user_id'], job['persona_id']
        messages = self.conversation_service.get_messages_between(
            user_id=user_id, persona_id=persona_id,
            since_id=job['after_message_id'], until_id=job['through_message_id'], limit=200)
        reference = self.conversation_service.get_messages_between(
            user_id=user_id,persona_id=persona_id,until_id=job['after_message_id'],limit=6) if job['after_message_id'] else []
        bounded=[];chars=0
        for message in reversed(reference):
            if bounded and chars+len(message.content)>2000:
                break
            bounded.append(message);chars+=len(message.content)
        reference=list(reversed(bounded))
        local_zone = timezone(timedelta(hours=self.tz_offset_hours))
        case = {'id':job['id'],'new_message_ids':[m.id for m in messages],
                'messages':[{'id':m.id,'role':m.role,'content':m.content,'created_at':m.created_at,
                             'local_time':parse_sqlite_ts(m.created_at).astimezone(local_zone).isoformat()}
                            for m in reference + messages],
                'existing_notes':[]}
        return {'messages':messages,'case':case,'snapshots':{},
                'llm_result':{'proposals':[],'links':[],'skipped':[],'uncertainties':[]},
                'tools':MemoryTools(self.note_service,self.retrieval,user_id,persona_id),
                'next':'prefetch' if any(m.role=='user' for m in messages) else 'commit'}

    def _prefetch(self,state):
        query=' '.join(m.content for m in state['messages'] if m.role=='user')[:2000]
        candidates=self.retrieval.search(state['user_id'],state['persona_id'],MemorySearch(
            query=query,purpose='consolidation',recent_limit=6,limit=12,max_hops=1)).notes
        snapshots={};chars=0
        for note in candidates:
            if snapshots and chars+len(note.content)>4000:
                continue
            snapshots[note.id]=note;chars+=len(note.content)
        case={**state['case'],'existing_notes':[note_payload(n) for n in snapshots.values()]}
        checkpoint('input_ready',messages=len(state['messages']),candidate_notes=len(snapshots),
                   source_chars=sum(len(m.content) for m in state['messages']))
        trace=current_trace()
        if trace:
            trace.artifact('input_case',json.dumps(case,ensure_ascii=False,indent=2))
        messages=draft_messages(case,response_schema=self.response_schema,tools=tool_specs())
        payload=json.loads(messages[-1]['content'])
        payload['tool_budget']={'max_rounds':self.MAX_TOOL_ROUNDS,'max_calls':self.MAX_TOOL_CALLS,
                                'max_model_calls_including_repair':self.MAX_MODEL_CALLS}
        # An empty keyword result is not proof that the memory store is empty.
        notes_exist=bool(self.conn.execute('SELECT 1 FROM note WHERE user_id=? AND persona_id=? AND is_archived=0 LIMIT 1',
                                         (state['user_id'],state['persona_id'])).fetchone())
        search_required=notes_exist and not snapshots
        payload['requires_search_before_write']=search_required
        messages[-1]['content']=json.dumps(payload,ensure_ascii=False)
        return {'case':case,'snapshots':snapshots,'model_messages':messages,'search_required':search_required}

    def _model(self,state):
        if state['model_calls']>=self.MAX_MODEL_CALLS:
            raise ValueError('记忆整理模型调用预算已用尽，未提交任何修改')
        messages=state['model_messages']
        chars=sum(len(m['content']) for m in messages)
        if chars>self.MAX_INPUT_CHARS:
            raise ValueError('本次整理输入超过预算，未截断来源或提交结果')
        remaining=state['deadline']-time.monotonic()
        checkpoint('model_request',request_number=state['model_calls']+1,input_chars=chars)
        raw=self.llm_client.chat(messages,timeout=min(self.timeout,remaining),max_retries=0,stream=True,
                                 total_timeout=remaining,background_thinking=self.thinking or state['repaired'])
        return {'response':parse_json_response(raw),'model_calls':state['model_calls']+1}

    def _validate(self,state):
        checkpoint('validate',request_number=state['model_calls'])
        data=state['response']
        try:
            if isinstance(data,dict) and 'tool_calls' in data:
                request=ToolRequest.model_validate(data)
                record_validation(data,state['model_calls'])
                return {'tool_requests':request.tool_calls,'next':'tools'}
            draft=validate_draft(state['case'],data)
            if draft.proposals and state['search_required']:
                raise ValueError('当前角色已有笔记但初次召回为空；提交建议前必须先调用 search_notes 核对旧记忆，不能将空候选视为没有旧记忆')
            record_validation(data,state['model_calls'])
            return {'llm_result':draft.model_dump(),'next':'commit'}
        except ValueError as exc:
            record_validation(data,state['model_calls'],exc)
            if state['repaired'] or state['model_calls']>=self.MAX_MODEL_CALLS:
                raise
            return {'validation_error':exc,'next':'repair'}

    def _repair(self,state):
        checkpoint('repair',error=describe_error(state['validation_error']))
        return {'model_messages':state['model_messages']+correction_messages(state['response'],state['validation_error']),
                'repaired':True}

    def _tools(self,state):
        requests=state['tool_requests']
        if state['tool_rounds']>=self.MAX_TOOL_ROUNDS or state['tool_calls']+len(requests)>self.MAX_TOOL_CALLS:
            raise ValueError('只读工具调用预算已用尽，未提交任何修改')
        if state['model_calls']>=self.MAX_MODEL_CALLS:
            raise ValueError('没有剩余模型预算处理工具结果，未提交任何修改')
        snapshots=dict(state['snapshots']);results=[];search_required=state['search_required']
        for index,call in enumerate(requests,state['tool_calls']+1):
            self.check_budget(state)
            checkpoint('tool.'+call.name,call_number=index)
            trace=current_trace()
            if trace:
                trace.artifact(f'tool-{index:02d}.request',call.model_dump_json(indent=2))
            try:
                data,notes=state['tools'].execute(call)
            except ValueError as exc:
                data,notes={'error':'invalid_request','message':describe_error(exc)},{}
            for nid,note in notes.items():
                old=snapshots.get(nid)
                if old and any(getattr(old,key)!=getattr(note,key) for key in
                               ('revision','is_archived','content','kind','reality','event_time','time_basis','user_id','persona_id')):
                    raise RetryableTaskError('工具读取期间候选记忆已改变，请重新整理')
            merged={**snapshots,**notes}
            if (len(json.dumps(data,ensure_ascii=False))>self.MAX_TOOL_RESULT_CHARS or
                len(merged)>self.MAX_CANDIDATES or
                (bool(notes.keys()-snapshots.keys()) and sum(len(n.content) for n in merged.values())>self.MAX_CANDIDATE_CHARS)):
                data={'error':'result_budget_exceeded','message':'工具结果超过本次候选/文本预算，未截断证据；请缩小检索范围。'}
            else:
                snapshots=merged
                if call.name=='search_notes' and not data.get('error'):
                    search_required=False
            if trace:
                trace.artifact(f'tool-{index:02d}.result',json.dumps(data,ensure_ascii=False,indent=2))
                trace.step('tool.result',call_number=index,tool_name=call.name,error=data.get('error'),candidate_notes=len(snapshots))
            results.append({'call_id':f'tool-{index:02d}','name':call.name,'arguments':call.arguments.model_dump(),'result':data})
        payload={'tool_results':results,'remaining_tool_rounds':self.MAX_TOOL_ROUNDS-state['tool_rounds']-1,
                 'remaining_tool_calls':self.MAX_TOOL_CALLS-state['tool_calls']-len(requests),
                 'requires_search_before_write':search_required,
                 'instruction':'依据最初的新消息和本次只读材料继续；候选充分则返回完整 Draft。旧笔记证据不能替代本批用户原话。'}
        messages=state['model_messages']+[
            {'role':'assistant','content':json.dumps(state['response'],ensure_ascii=False)},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        return {'case':{**state['case'],'existing_notes':[note_payload(n) for n in snapshots.values()]},
                'snapshots':snapshots,'model_messages':messages,
                'search_required':search_required,
                'tool_rounds':state['tool_rounds']+1,'tool_calls':state['tool_calls']+len(requests)}

    def _commit(self,state):
        data=state['llm_result'];job=state['job']
        checkpoint('write',proposals=len(data['proposals']))
        applied=MemoryWriter(self.note_service,self.jobs).apply(state['case'],data,job,job['lease_token'],state['snapshots'])
        trace=current_trace()
        if trace:
            trace.artifact('input_case.final',json.dumps(state['case'],ensure_ascii=False,indent=2))
            trace.artifact('write_result',json.dumps(applied,ensure_ascii=False,indent=2))
            trace.step('db.commit',job_id=job['id'],facts_created=applied['facts_created'],notes_updated=applied['notes_updated'])
        return {**applied,'job_status':'succeeded','extraction_failed':False}
