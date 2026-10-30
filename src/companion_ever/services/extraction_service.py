"""记忆抽取的唯一实现：读取会话、验证模型输出、写入事实和画像。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from companion_ever.storage.memory_job_repo import MemoryJobRepo
from companion_ever.services.memory_draft import generate_draft
from companion_ever.services.memory_writer import MemoryWriter
from companion_ever.services.task_errors import can_retry, describe_error, failure_kind
from companion_ever.services.task_runtime import runtime_for, checkpoint, TaskInterrupted
from companion_ever.services.retrieval_service import RetrievalService
from companion_ever.services.memory_search import MemorySearch
from companion_ever.config import RetrievalConfig
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.trace import trace_scope,current_trace
from companion_ever.storage.task_repo import TaskRepo
import json


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
                  "notes_updated":0, "note_ids":{}, "errors": [], "extraction_failed": False}
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
                    return self._execute(result, claimed, token)
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

    def _execute(self, result, job, token):
        checkpoint('load_input')
        user_id, persona_id = job['user_id'], job['persona_id']
        messages = self.conversation_service.get_messages_between(
            user_id=user_id, persona_id=persona_id,
            since_id=job['after_message_id'], until_id=job['through_message_id'], limit=200)
        result['messages'] = messages
        retrieval = RetrievalService(self.note_service.note_repo,self.note_service.authorizer,RetrievalConfig())
        query = ' '.join(m.content for m in messages if m.role == 'user')[:2000]
        candidates = retrieval.search(user_id,persona_id,MemorySearch(
            query=query,purpose='consolidation',recent_limit=6,limit=12,max_hops=1)).notes
        bounded=[];chars=0
        for note in candidates:
            if bounded and chars+len(note.content)>4000:
                continue
            bounded.append(note);chars+=len(note.content)
        candidates=bounded
        snapshots = {n.id:n for n in candidates}
        reference = self.conversation_service.get_messages_between(
            user_id=user_id,persona_id=persona_id,until_id=job['after_message_id'],limit=6) if job['after_message_id'] else []
        bounded=[];chars=0
        for message in reversed(reference):
            if bounded and chars+len(message.content)>2000:
                break
            bounded.append(message);chars+=len(message.content)
        reference=list(reversed(bounded))
        checkpoint('input_ready',messages=len(messages),reference_messages=len(reference),candidate_notes=len(candidates),
                   source_chars=sum(len(m.content) for m in messages),reference_chars=chars)
        local_zone = timezone(timedelta(hours=self.tz_offset_hours))
        case = {'id':job['id'],'new_message_ids':[m.id for m in messages],
                'messages':[{'id':m.id,'role':m.role,'content':m.content,'created_at':m.created_at,
                             'local_time':parse_sqlite_ts(m.created_at).astimezone(local_zone).isoformat()}
                            for m in reference + messages],
                'existing_notes':[{'id':n.id,'kind':n.kind.value,'content':n.content,
                                   'reality':n.reality,'event_time':n.event_time} for n in candidates]}
        trace=current_trace()
        if trace:
            trace.artifact('input_case',json.dumps(case,ensure_ascii=False,indent=2))
        if any(m.role=='user' for m in messages):
            data = generate_draft(case,self.llm_client,timeout=self.timeout,max_retries=0,
                                  stream=True,total_timeout=self.timeout*3,repair_attempts=1,background_thinking=self.thinking).model_dump()
        else:
            data = {'proposals':[],'links':[],'skipped':[],'uncertainties':[]}
        result['llm_result'] = data
        checkpoint('write',proposals=len(data['proposals']))
        applied = MemoryWriter(self.note_service,self.jobs).apply(case,data,job,token,snapshots)
        if trace:
            trace.artifact('write_result',json.dumps(applied,ensure_ascii=False,indent=2))
            trace.step('db.commit',job_id=job['id'],facts_created=applied['facts_created'],notes_updated=applied['notes_updated'])
        result.update(applied,job_status='succeeded',extraction_failed=False)
        return result
