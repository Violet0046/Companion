"""Incremental summaries derived from fact operations; never create or update notes."""
from __future__ import annotations
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, ValidationError
from companion_ever.prompts import load
from companion_ever.storage.db import transaction
from companion_ever.storage.summary_repo import SummaryRepo, digest
from companion_ever.domain.enums import BlockType
from companion_ever.services.task_errors import can_retry, describe_error, RetryableTaskError, failure_kind
from companion_ever.services.structured_generation import checked_json
from companion_ever.services.task_runtime import runtime_for, checkpoint as task_checkpoint, TaskInterrupted
from companion_ever.storage.task_repo import TaskRepo
from companion_ever.trace import trace_scope,current_trace


class Citation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    note_id:str=Field(min_length=1)
    quote:str=Field(min_length=1,max_length=160)
    revision:StrictInt | None=None


class Claim(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text:str=Field(min_length=1,max_length=200)
    reality:Literal['user_report','shared_fiction','uncertain']
    citations:list[Citation]=Field(min_length=1,max_length=2)

    @field_validator('text')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():
            raise ValueError('摘要内容不能为空白')
        return value


class Profile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    identity:list[Claim]=Field(default_factory=list,max_length=8)
    personality:list[Claim]=Field(default_factory=list,max_length=6)
    preferences:list[Claim]=Field(default_factory=list,max_length=8)
    communication_style_hint:list[Claim]=Field(default_factory=list,max_length=6)


class Relationship(BaseModel):
    model_config=ConfigDict(extra='forbid')
    overview:list[Claim]=Field(default_factory=list,max_length=6)
    agreements:list[Claim]=Field(default_factory=list,max_length=6)
    key_moments:list[Claim]=Field(default_factory=list,max_length=6)


class SummaryDocument(BaseModel):
    model_config=ConfigDict(extra='forbid')
    user_profile:Profile
    relationship_state:Relationship
    uncertainties:list[str]=Field(default_factory=list,max_length=8)


def claims(document):
    for block in ('user_profile','relationship_state'):
        for items in document.get(block,{}).values():
            yield from items


def fields_for(document):
    def display(item):
        prefix={'shared_fiction':'共同想象：','uncertain':'待核实：','user_report':''}[item['reality']]
        return prefix+item['text']
    return {block:{field:[display(item) for item in items] for field,items in document[block].items()}
            for block in ('user_profile','relationship_state')}


class SummaryService:
    def __init__(self,authorizer,note_repo,block_repo,llm_client,timeout=120,thinking=False):
        self.authorizer=authorizer;self.conn=note_repo.conn;self.blocks=block_repo
        self.repo=SummaryRepo(self.conn);self.llm=llm_client;self.timeout=timeout
        self._active=set();self._lock=threading.Lock()
        self.on_updated=None
        self.runtime=runtime_for(self.conn)
        self.thinking=thinking

    def status(self,user,persona):
        self.authorizer.require_persona(user,persona)
        checkpoint=self.repo.checkpoint(user,persona)
        job=self.repo.pending(user,persona)
        cursor=checkpoint['last_change_id'] if checkpoint else 0
        count=self.conn.execute('SELECT count(*) FROM fact_change WHERE user_id=? AND persona_id=? AND id>?',(user,persona,cursor)).fetchone()[0]
        current=bool(checkpoint and not count and checkpoint['blocks_hash']==digest(self.repo.blocks(user,persona)))
        return {'current':current,'stale':bool(checkpoint and not current),'legacy':checkpoint is None,
                'generated_at':checkpoint['generated_at'] if checkpoint else None,'pending_changes':count,
                'last_change_id':cursor,'job_id':job['id'] if job else None,
                'job_status':job['status'] if job else None,'last_error':job['last_error'] if job else None,
                'retry_scheduled':bool(job and job['retry_at'] is not None),
                'document':json.loads(checkpoint['document_json']) if checkpoint else None}

    def request(self,user,persona,background=True):
        self.authorizer.require_persona(user,persona)
        job=self.repo.enqueue(user,persona,manual=True)
        if job is None:
            return {'status':'unchanged','job_id':None}
        if background:
            self._launch(user,persona)
        return {'status':job['status'],'job_id':job['id']}

    def run(self,jid):
        try:
            with self.runtime.slot():
                return self._run(jid)
        except TaskInterrupted:
            return self.repo.get(jid)

    def _run(self,jid):
        job=self.repo.claim(jid,owner_id=self.runtime.owner_id,lease_seconds=self.runtime.lease_seconds)
        if job is None:
            return self.repo.get(jid)
        with trace_scope('summary',job['user_id'],job['persona_id'],job_id=jid,attempt_id=job['lease_token']) as trace:
            if getattr(trace,'_store',None):
                TaskRepo(self.conn).event(job['lease_token'],'trace',{'trace_id':trace.trace_id})
            try:
                with self.runtime.execute('summary',self.repo,job):
                    self.authorizer.require_persona(job['user_id'],job['persona_id'])
                    self._execute(job)
            except TaskInterrupted as exc:
                trace.finish('interrupted',str(exc))
                return self.repo.get(jid)
            except Exception as exc:
                trace.finish('error',describe_error(exc))
                self.repo.fail(job,describe_error(exc),retryable=can_retry(exc),failure_kind=failure_kind(exc))
        result=self.repo.get(jid)
        if self.on_updated and result['status'] in ('succeeded','failed'):
            try:
                self.on_updated(job['user_id'],job['persona_id'])
            except Exception:
                logging.getLogger(__name__).exception('摘要更新通知失败')
        return result

    def _execute(self,job):
        task_checkpoint('load_input')
        user,persona=job['user_id'],job['persona_id']
        checkpoint=self.repo.checkpoint(user,persona)
        expected_cursor=checkpoint['last_change_id'] if checkpoint else 0
        if expected_cursor!=job['after_change_id']:
            raise ValueError('摘要进度已被其他任务推进')
        previous=json.loads(checkpoint['document_json']) if checkpoint else SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump()
        events=self.repo.changes(job)
        referenced={c['note_id'] for item in claims(previous) for c in item['citations']}
        impacted=self.repo.affected({e['note_id'] for e in events},user,persona)
        available={nid:fact for nid in referenced|impacted if (fact:=self.repo.fact(nid,user,persona)) is not None}
        payload={'previous_summaries':previous,
                 'changes':[{k:e[k] for k in ('id','note_id','operation','before','after','source_job_id')} for e in events],
                 # Original chat evidence stays in storage/the operation journal.
                 # This projection has one unambiguous citation text: fact.content.
                 'current_facts':[{k:v for k,v in fact.items() if k!='evidence'} for fact in available.values()],
                 'unavailable_note_ids':sorted((referenced|impacted)-available.keys())}
        schema=SummaryDocument.model_json_schema()
        payload['output_limits']={block:{name:spec['maxItems'] for name,spec in schema['$defs'][model.__name__]['properties'].items()}
                                 for block,model in (('user_profile',Profile),('relationship_state',Relationship))}
        before_blocks=self.repo.blocks(user,persona)
        trace=current_trace()
        if trace:
            trace.artifact('input_case',json.dumps(payload,ensure_ascii=False,indent=2))
        task_checkpoint('input_ready',changes=len(events),current_facts=len(available),input_chars=len(json.dumps(payload,ensure_ascii=False)))
        if len(json.dumps(payload,ensure_ascii=False))>60000:
            raise ValueError('本批摘要输入超过预算，未截断或跳过操作；请精简过长事实后重试')
        reuse=bool(checkpoint and events and all(e['operation'] in ('evidence_added','evidence_removed') for e in events))
        if reuse:
            try:
                self._validate(previous,available)
            except ValueError:
                reuse=False
        if reuse:
            document=SummaryDocument.model_validate(previous).model_dump()
        elif available:
            def validate(data):
                try:
                    document=SummaryDocument.model_validate(data).model_dump()
                except ValidationError as exc:
                    structural=['.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False,include_url=False)]
                    # A capacity/type error must not hide independent quote errors
                    # from the only corrective call. Acceptance still needs both.
                    raise ValueError('; '.join(structural+self._citation_errors(data,available))) from exc
                self._validate(document,available)
                return document
            document=checked_json(self.llm,[{'role':'system','content':load('summary_system.txt')+'\nJSON Schema:\n'+json.dumps(SummaryDocument.model_json_schema(),ensure_ascii=False)},
                               {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],timeout=self.timeout,max_retries=0,
                               stream=True,total_timeout=self.timeout*3,validate=validate,repair_attempts=1,background_thinking=self.thinking)
        else:
            # Last fact deleted: clear both summaries without spending a model call.
            document=SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump()
        self._validate(document,available)
        fields=fields_for(document)
        task_checkpoint('write',claims=sum(1 for _ in claims(document)))
        with transaction(self.conn):
            self.repo.require_owner(job)
            latest_checkpoint=self.repo.checkpoint(user,persona)
            if (latest_checkpoint['last_change_id'] if latest_checkpoint else 0)!=expected_cursor:
                raise ValueError('摘要进度已改变，拒绝旧结果')
            if digest(self.repo.blocks(user,persona))!=digest(before_blocks):
                raise RetryableTaskError('摘要已被其他操作修改')
            # Re-evaluate every referenced/affected fact, including disappearance/reactivation.
            now={nid:fact for nid in referenced|impacted if (fact:=self.repo.fact(nid,user,persona)) is not None}
            if digest(now)!=digest(available):
                raise RetryableTaskError('生成期间事实已改变，等待重试')
            for item in claims(document):
                for citation in item['citations']:
                    citation['revision']=now[citation['note_id']]['revision']
            for block,values in fields.items():
                self.blocks.upsert(user,persona,BlockType(block),values)  # Replace; no monotonic/list-union merge.
            generated=datetime.now(timezone.utc).isoformat()
            self.conn.execute('INSERT INTO summary_checkpoint(user_id,persona_id,last_change_id,job_id,document_json,blocks_hash,generated_at) '
                'VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,persona_id) DO UPDATE SET last_change_id=excluded.last_change_id,'
                'job_id=excluded.job_id,document_json=excluded.document_json,blocks_hash=excluded.blocks_hash,generated_at=excluded.generated_at',
                (user,persona,job['through_change_id'],job['id'],json.dumps(document,ensure_ascii=False),digest(fields),generated))
            self.repo.require_owner(job)
            self.conn.execute("UPDATE summary_job SET status='succeeded',completed_at=?,lease_expires_at=NULL,last_error=NULL,result_json=?,previous_blocks_json=? WHERE id=? AND lease_token=?",
                (generated,json.dumps(document,ensure_ascii=False),json.dumps(before_blocks,ensure_ascii=False),job['id'],job['lease_token']))
            TaskRepo(self.conn).finish(job['lease_token'],'succeeded')
        if trace:
            trace.step('db.commit',job_id=job['id'],last_change_id=job['through_change_id'],
                       claims=sum(1 for _ in claims(document)))

    @staticmethod
    def _citation_errors(document,available):
        errors=[]
        if not isinstance(document,dict):
            return errors
        for block in ('user_profile','relationship_state'):
            fields=document.get(block)
            if not isinstance(fields,dict):
                continue
            for field,items in fields.items():
                if not isinstance(items,list):
                    continue
                for index,item in enumerate(items):
                    if not isinstance(item,dict) or not isinstance(item.get('citations'),list):
                        continue
                    seen=set()
                    for ci,citation in enumerate(item['citations']):
                        if not isinstance(citation,dict) or not isinstance(citation.get('note_id'),str) or not isinstance(citation.get('quote'),str):
                            continue  # The schema validator reports malformed citation fields.
                        nid=citation['note_id'];fact=available.get(nid)
                        location=f'{block}.{field}[{index}].citations[{ci}].note_id={nid}'
                        if fact is None:
                            errors.append(location+'：不是当前可引用的有效 fact，不能引用删除/归档/被取代事实')
                            continue
                        if not citation['quote'].strip() or citation['quote'] not in fact['content']:
                            errors.append(location+'：quote 必须是 current_facts 中该 note_id 的 content 连续逐字原文；不能从原始聊天、changes 或旧摘要复制引文')
                        if nid in seen or item.get('reality')!=fact['reality']:
                            errors.append(location+'：摘要引用重复或改变了事实的现实情境')
                        seen.add(nid)
        return errors

    @classmethod
    def _validate(cls,document,available):
        errors=cls._citation_errors(document,available)
        if errors:
            raise ValueError('; '.join(errors))

    def prompt_text(self,user,persona):
        status=self.status(user,persona)
        if not status['current']:
            return ''
        lines=[]
        for block,label in (('user_profile','用户画像'),('relationship_state','关系概况')):
            entries=[f"- [{i['reality']}] {i['text']}" for values in status['document'][block].values() for i in values]
            if entries:
                lines.append(label+'（由有效事实整理，原始事实优先）：\n'+'\n'.join(entries))
        return '\n\n'.join(lines)

    def _launch(self,user,persona):
        if self._memory_busy(user,persona):
            return
        key=(user,persona)
        with self._lock:
            if key in self._active:
                return
            self._active.add(key)
        def worker():
            try:
                while not self.runtime.closing.is_set() and not self._memory_busy(user,persona) and (job:=self.repo.pending(user,persona)) is not None:
                    out=self.run(job['id'])
                    if out['status']!='succeeded':
                        break
                    if self.repo.enqueue(user,persona) is None:
                        break
            except Exception:
                logging.getLogger(__name__).exception('摘要任务失败')
            finally:
                with self._lock:
                    self._active.discard(key)
                self.conn.close_current()
        threading.Thread(target=worker,daemon=True,name='summary-worker').start()

    def _memory_busy(self,user,persona):
        return bool(self.conn.execute("SELECT 1 FROM memory_job j JOIN conversation c ON c.id=j.conversation_id "
            "WHERE c.user_id=? AND c.persona_id=? AND j.status IN ('queued','running') LIMIT 1",(user,persona)).fetchone())

    def resume_pending(self,auto_evolve=True):
        # Only initialized scopes auto-evolve. Legacy summaries require one explicit initialization.
        scopes={(r['user_id'],r['persona_id']) for r in self.conn.execute('SELECT user_id,persona_id FROM summary_checkpoint')} if auto_evolve else set()
        scopes.update((r['user_id'],r['persona_id']) for r in self.conn.execute("SELECT user_id,persona_id FROM summary_job WHERE status!='succeeded'"))
        for user,persona in scopes:
            try:
                self.authorizer.require_persona(user,persona)
                job=self.repo.enqueue(user,persona) if auto_evolve else self.repo.pending(user,persona)
                ready=job and (job['status']=='queued' or
                    (job['status']=='running' and job['lease_expires_at']<=time.time()) or
                    (job['status']=='failed' and job['retry_at'] is not None and job['retry_at']<=time.time()))
                if ready:
                    self._launch(user,persona)
            except PermissionError:
                continue
            except Exception:
                logging.getLogger(__name__).exception('恢复角色摘要任务失败')
