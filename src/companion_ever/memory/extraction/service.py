"""Durable memory execution and the business operations used by its LangGraph."""
from __future__ import annotations
from companion_ever.infrastructure.tracing import call_context

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from companion_ever.memory.extraction.jobs import MemoryJobRepo
from companion_ever.memory.extraction.draft import (
    Draft, draft_messages, note_projection, step_payload, validate_draft)
from companion_ever.memory.extraction.tools import MemoryTools, ToolRequest, note_payload, tool_specs
from companion_ever.infrastructure.llm.json_output import parse_json_response
from companion_ever.prompts import load
from companion_ever.runtime.structured import correction_messages
from companion_ever.memory.extraction.writer import MemoryWriter
from companion_ever.runtime.errors import can_retry, describe_error, failure_kind, RetryableTaskError
from companion_ever.runtime.tasks import runtime_for, checkpoint, TaskInterrupted
from companion_ever.memory.retrieval.service import RetrievalService
from companion_ever.memory.retrieval.query import MemorySearch
from companion_ever.config import RetrievalConfig
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.runtime.repository import TaskRepo
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
    # 这是护栏，不是目标：真正的限制是上面的批次、候选、工具结果上限。
    # 护栏必须高于它们之和，否则会对合法输入硬失败——而超预算是 ValueError，
    # can_retry 返回 False，这一批消息就永远不会被整理，留下记忆空洞。
    # 各上限允许的最坏情况约 9.5 万字符（见 tests/test_memory_payload_contract.py）。
    MAX_INPUT_CHARS = 100000

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
        from companion_ever.memory.extraction.workflow import build_extraction_graph
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
        scope_options={'conversation_id':state['conversation_id']} if state.get('conversation_id') is not None else {}
        job = self.jobs.enqueue(user_id, persona_id,**scope_options)
        if not job:
            return result
        result.update(job_id=job['id'], job_status=job['status'])
        claimed = self.jobs.claim(job['id'],owner_id=self.runtime.owner_id,lease_seconds=self.runtime.lease_seconds)
        if not claimed:
            job = self.jobs.get(job['id'])
            result['job_status'] = job['status']
            if job['last_error']:
                result['errors'] = [job['last_error']]
            return result
        token = claimed['lease_token']
        with call_context('extraction',user_id,persona_id,job_id=job['id'],attempt_id=token,
                          attempt=TaskRepo(self.conn).sequence(token)):
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
                result.update(job_status=current['status'],errors=[],extraction_failed=False)
                return result
            except Exception as exc:
                error=describe_error(exc)
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
        messages = self.conversation_service.memory_messages(user_id,persona_id,job['conversation_id'],
            since_id=job['after_message_id'],until_id=job['through_message_id'],limit=200)
        reference = self.conversation_service.memory_messages(user_id,persona_id,job['conversation_id'],
            until_id=job['after_message_id'],limit=6) if job['after_message_id'] else []
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
        group_id=job['conversation_id'] if job['conversation_kind']=='group' else None
        if group_id is not None:
            case['conversation']={'id':group_id,'kind':'group','self_persona_id':persona_id}
            case['membership_events']=self.conversation_service.membership_events(user_id,persona_id,group_id,
                reference[0].id if reference else job['after_message_id'],job['through_message_id'])
            for data,message in zip(case['messages'],reference+messages):
                data.update(sender_kind=message.sender_kind,sender_id=message.sender_id)
        return {'messages':messages,'case':case,'snapshots':{},
                'llm_result':{'proposals':[],'links':[],'skipped':[],'uncertainties':[]},
                'tools':MemoryTools(self.note_service,self.retrieval,user_id,persona_id,conversation_id=group_id),
                'next':'prefetch' if any(m.role=='user' for m in messages) else 'commit'}

    def _prefetch(self,state):
        query=' '.join(m.content for m in state['messages'] if m.role=='user')[:2000]
        group_id=state['case'].get('conversation',{}).get('id')
        candidates=self.retrieval.search(state['user_id'],state['persona_id'],MemorySearch(
            query=query,purpose='consolidation',recent_limit=6,limit=12,max_hops=1,conversation_id=group_id)).notes
        snapshots={};chars=0
        for note in candidates:
            if snapshots and chars+len(note.content)>4000:
                continue
            snapshots[note.id]=note;chars+=len(note.content)
        case={**state['case'],'existing_notes':[note_payload(n) for n in snapshots.values()]}
        checkpoint('input_ready',messages=len(state['messages']),candidate_notes=len(snapshots),
                   source_chars=sum(len(m.content) for m in state['messages']))
        # An empty keyword result is not proof that the memory store is empty.
        visible=self.note_service.note_repo.visible_ids(state['user_id'],state['persona_id'],group_id) if group_id is not None else None
        notes_exist=self.note_service.note_repo.exists_active(state['user_id'],state['persona_id'],allowed_ids=visible)
        search_required=notes_exist and not snapshots
        messages=draft_messages(case, response_schema=self.response_schema, tools=tool_specs(),
                                requires_search_before_write=search_required,
                                remaining_tool_rounds=self.MAX_TOOL_ROUNDS,
                                remaining_tool_calls=self.MAX_TOOL_CALLS)
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
                return {'tool_requests':request.tool_calls,'next':'tools'}
            draft=validate_draft(state['case'],data)
            if draft.proposals and state['search_required']:
                raise ValueError('当前角色已有笔记但初次召回为空；提交建议前必须先调用 search_notes 核对旧记忆，不能将空候选视为没有旧记忆')
            return {'llm_result':draft.model_dump(),'next':'commit'}
        except ValueError as exc:
            if state['repaired'] or state['model_calls']>=self.MAX_MODEL_CALLS:
                raise
            return {'validation_error':exc,'next':'repair'}

    def _repair(self,state):
        checkpoint('repair',error=describe_error(state['validation_error']))
        return {'model_messages':state['model_messages']+correction_messages(state['response'],state['validation_error'],load('repair_evidence.txt')),
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
                sum(len(n.content) for n in merged.values())>self.MAX_CANDIDATE_CHARS):
                data={'error':'result_budget_exceeded','message':'工具结果超过本次候选/文本预算，未截断证据；请缩小检索范围。'}
            else:
                snapshots=merged
                if call.name=='search_notes' and not data.get('error'):
                    search_required=False
            results.append({'call_id':f'tool-{index:02d}','name':call.name,'arguments':call.arguments.model_dump(),'result':data})
        # 候选集每轮完整回灌：模型在每一轮都要能看到全部候选，
        # 否则 target_id 只能从工具返回里反推，confirm/append 几乎无法做对。
        updated_case={**state['case'],'existing_notes':[note_payload(n) for n in snapshots.values()]}
        payload=step_payload(round_no=state['model_calls']+1,
                             existing_notes=note_projection(updated_case['existing_notes']),
                             tool_results=results,
                             requires_search_before_write=search_required,
                             remaining_tool_rounds=self.MAX_TOOL_ROUNDS-state['tool_rounds']-1,
                             remaining_tool_calls=self.MAX_TOOL_CALLS-state['tool_calls']-len(requests),
                             tools=tool_specs())
        messages=state['model_messages']+[
            {'role':'assistant','content':json.dumps(state['response'],ensure_ascii=False)},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]
        return {'case':updated_case,
                'snapshots':snapshots,'model_messages':messages,
                'search_required':search_required,
                'tool_rounds':state['tool_rounds']+1,'tool_calls':state['tool_calls']+len(requests)}

    def _commit(self,state):
        data=state['llm_result'];job=state['job']
        checkpoint('write',proposals=len(data['proposals']))
        applied=MemoryWriter(self.note_service,self.jobs).apply(state['case'],data,job,job['lease_token'],state['snapshots'])
        return {**applied,'job_status':'succeeded','extraction_failed':False}
