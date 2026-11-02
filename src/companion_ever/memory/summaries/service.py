"""Incremental summaries derived from fact operations; never create or update notes."""
from __future__ import annotations
from companion_ever.infrastructure.tracing import call_context
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, ValidationError
from companion_ever.prompts import load
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.memory.summaries.repository import SummaryRepo, digest
from companion_ever.domain.enums import BlockType
from companion_ever.runtime.errors import can_retry, describe_error, RetryableTaskError, failure_kind
from companion_ever.runtime.structured import correction_messages
from companion_ever.infrastructure.llm.json_output import parse_json_response
from companion_ever.runtime.tasks import runtime_for, checkpoint as task_checkpoint, TaskInterrupted
from companion_ever.runtime.repository import TaskRepo
from companion_ever.memory.summaries.changes import coalesce_changes
from companion_ever.memory.extraction.jobs import MemoryJobRepo


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
    MAX_MODEL_CALLS = 2
    # 护栏，不是目标：真正的限制是下面各项与 change_window 的上限。
    # 护栏必须高于它们之和，否则会对合法输入硬失败——超预算是 ValueError，
    # can_retry 返回 False，这一批摘要就永远不会被演化。
    MAX_INPUT_CHARS = 100000
    MAX_PAYLOAD_CHARS = 90000
    # current_facts 的正文预算：引用最多 160 字，单条留够这个长度即可。
    MAX_FACT_CHARS = 24000
    MAX_FACT_CONTENT_CHARS = 400
    MIN_FACT_CONTENT_CHARS = 160

    def __init__(self,authorizer,note_repo,block_repo,llm_client,timeout=120,thinking=False):
        self.authorizer=authorizer;self.conn=note_repo.conn;self.blocks=block_repo
        self.note_repo=note_repo
        self.repo=SummaryRepo(self.conn);self.llm=llm_client;self.timeout=timeout
        self._active=set();self._lock=threading.Lock()
        self.on_updated=None
        self.runtime=runtime_for(self.conn)
        self.thinking=thinking
        from companion_ever.memory.summaries.workflow import build_summary_graph
        self.graph=build_summary_graph(self)

    def status(self,user,persona):
        self.authorizer.require_persona(user,persona)
        checkpoint=self.repo.checkpoint(user,persona)
        job=self.repo.pending(user,persona)
        cursor=checkpoint['last_change_id'] if checkpoint else 0
        count=self.repo.change_count(user,persona,cursor)
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

    def schedule(self, user, persona):
        """业务调度已判断到期；使用普通入队语义，不重置永久失败。"""
        self.authorizer.require_persona(user, persona)
        job = self.repo.enqueue(user, persona)
        if job:
            self._launch(user, persona)
        return job

    def recent_jobs(self, user, persona):
        self.authorizer.require_persona(user, persona)
        return self.repo.recent(user, persona)

    def fact_changes(self, user, persona, after_id=0, limit=50):
        self.authorizer.require_persona(user, persona)
        return self.repo.audit_changes(user, persona, after_id, limit)

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
        with call_context('summary',job['user_id'],job['persona_id'],job_id=jid,attempt_id=job['lease_token'],
                          attempt=TaskRepo(self.conn).sequence(job['lease_token'])):
            try:
                with self.runtime.execute('summary',self.repo,job):
                    self.authorizer.require_persona(job['user_id'],job['persona_id'])
                    self.graph.invoke({'job':job,'deadline':time.monotonic()+self.timeout*3,
                                       'model_calls':0,'repaired':False},config={'recursion_limit':24})
            except TaskInterrupted as exc:
                return self.repo.get(jid)
            except Exception as exc:
                self.repo.fail(job,describe_error(exc),retryable=can_retry(exc),failure_kind=failure_kind(exc))
        result=self.repo.get(jid)
        if self.on_updated and result['status'] in ('succeeded','failed'):
            try:
                self.on_updated(job['user_id'],job['persona_id'])
            except Exception:
                logging.getLogger(__name__).exception('摘要更新通知失败')
        return result

    def _load_input(self,state):
        job=state['job']
        task_checkpoint('load_input')
        user,persona=job['user_id'],job['persona_id']
        checkpoint=self.repo.checkpoint(user,persona)
        expected_cursor=checkpoint['last_change_id'] if checkpoint else 0
        if expected_cursor!=job['after_change_id']:
            raise ValueError('摘要进度已被其他任务推进')
        previous=json.loads(checkpoint['document_json']) if checkpoint else SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump()
        raw_events=self.repo.changes(job)
        events=coalesce_changes(raw_events)
        referenced={c['note_id'] for item in claims(previous) for c in item['citations']}
        impacted=self.repo.affected({e['note_id'] for e in events},user,persona)
        available={nid:fact for nid in sorted(referenced|impacted) if (fact:=self.repo.fact(nid,user,persona)) is not None}
        payload={'persona_id':persona,'previous_summaries':previous,
                 'changes':[{k:e[k] for k in ('id','first_change_id','note_id','operation','before','after','relation_changes','source_job_id')} for e in events],
                 # Original chat evidence stays in storage/the operation journal.
                 # This projection has one unambiguous citation text: fact.content.
                 'current_facts':self._bounded_facts([{k:v for k,v in fact.items() if k!='evidence'}
                                                    for fact in available.values()]),
                 'unavailable_note_ids':sorted((referenced|impacted)-available.keys())}
        schema=SummaryDocument.model_json_schema()
        payload['output_limits']={block:{name:spec['maxItems'] for name,spec in schema['$defs'][model.__name__]['properties'].items()}
                                 for block,model in (('user_profile',Profile),('relationship_state',Relationship))}
        before_blocks=self.repo.blocks(user,persona)
        task_checkpoint('input_ready',changes=len(events),journal_events=len(raw_events),current_facts=len(available),input_chars=len(json.dumps(payload,ensure_ascii=False)))
        if len(json.dumps(payload,ensure_ascii=False))>self.MAX_PAYLOAD_CHARS:
            raise ValueError('本批摘要输入超过预算，未截断或跳过操作；请精简过长事实后重试')
        return {'checkpoint':checkpoint,'expected_cursor':expected_cursor,'previous':previous,
                'referenced':referenced,'impacted':impacted,'available':available,'before_blocks':before_blocks,
                'events':events,'model_messages':[{'role':'system','content':load('summary_system.txt')+'\n输出 JSON Schema：\n'+json.dumps(schema,ensure_ascii=False)},
                                                {'role':'user','content':json.dumps(payload,ensure_ascii=False)}]}

    def check_budget(self,state):
        if time.monotonic()>=state['deadline']:
            raise TimeoutError('摘要生成及纠错超过本次任务的总时限')

    def _decide(self,state):
        reuse=bool(state['checkpoint'] and not state['events'])
        if reuse:
            try:
                self._validate(state['previous'],state['available'])
            except ValueError:
                reuse=False
        return {'next':'reuse' if reuse else 'model' if state['available'] else 'clear'}

    def _reuse(self,state):
        return {'document':SummaryDocument.model_validate(state['previous']).model_dump(),'origin':'reuse'}

    def _clear(self,state):
        return {'document':SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump(),'origin':'clear'}

    @classmethod
    def _bounded_facts(cls,facts):
        """current_facts 的正文预算。

        引用的 quote 最多 160 字，所以每条至少留够这个长度；总量封顶。
        截断只取正文前缀——前缀仍是原文的连续片段，引用校验照常通过。
        """
        if not facts:
            return []
        per=min(cls.MAX_FACT_CONTENT_CHARS,
                max(cls.MIN_FACT_CONTENT_CHARS,cls.MAX_FACT_CHARS//len(facts)))
        return [{**fact,'content':(fact.get('content') or '')[:per]} for fact in facts]

    def _model(self,state):
        if state['model_calls']>=self.MAX_MODEL_CALLS:
            raise ValueError('摘要模型调用预算已用尽')
        if sum(len(message['content']) for message in state['model_messages'])>self.MAX_INPUT_CHARS:
            raise ValueError('摘要完整输入超过预算，未截断来源或提交结果')
        remaining=state['deadline']-time.monotonic()
        task_checkpoint('model_request',request_number=state['model_calls']+1)
        raw=self.llm.chat(state['model_messages'],timeout=min(self.timeout,remaining),max_retries=0,stream=True,
                          total_timeout=remaining,background_thinking=self.thinking or state['repaired'])
        return {'response':parse_json_response(raw),'model_calls':state['model_calls']+1,'origin':'model'}

    def _validate_output(self,state):
        data=state['response'] if state['origin']=='model' else state['document']
        try:
            try:
                document=SummaryDocument.model_validate(data).model_dump()
            except ValidationError as exc:
                structural=['.'.join(map(str,e['loc']))+': '+e['msg'] for e in exc.errors(include_input=False,include_url=False)]
                raise ValueError('; '.join(structural+self._citation_errors(data,state['available']))) from exc
            self._validate(document,state['available'])
            return {'document':document,'next':'commit'}
        except ValueError as exc:
            if state['origin']!='model' or state['repaired'] or state['model_calls']>=2:raise
            return {'validation_error':exc,'next':'repair'}

    def _repair(self,state):
        task_checkpoint('repair',error=describe_error(state['validation_error']))
        return {'model_messages':state['model_messages']+correction_messages(state['response'],state['validation_error'],load('repair_citation.txt')),
                'repaired':True}

    def _commit(self,state):
        job=state['job'];user,persona=job['user_id'],job['persona_id']
        document=state['document'];available=state['available'];expected_cursor=state['expected_cursor']
        referenced,impacted=state['referenced'],state['impacted'];before_blocks=state['before_blocks']
        self._validate(document,available)
        fields=fields_for(document)
        task_checkpoint('write',claims=sum(1 for _ in claims(document)))
        with transaction(self.conn):
            self.authorizer.require_persona(user,persona)
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
            self.repo.save_result(job,document,fields,before_blocks,generated)
        return {}

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

    def prompt_text(self,user,persona,*,conversation_id=None,relationship_only=False):
        self.authorizer.require_persona(user,persona)
        checkpoint=self.repo.checkpoint(user,persona)
        # Sync progress is diagnostic; pending work must not remove the last
        # successfully committed profile and relationship context from chat.
        if checkpoint is None:
            return ''
        document=json.loads(checkpoint['document_json'])
        visible=self.note_repo.visible_ids(user,persona,conversation_id) if conversation_id is not None else None
        def allowed(item):
            if visible is None:return True
            return bool(item['citations']) and all(c['note_id'] in visible and self.repo.fact(c['note_id'],user,persona) is not None for c in item['citations'])
        lines=[]
        for block,label in (('user_profile','用户画像'),('relationship_state','关系概况')):
            if relationship_only and block!='relationship_state':continue
            entries=[f"- [{i['reality']}] {i['text']}" for values in document[block].values() for i in values if allowed(i)]
            if entries:
                lines.append(label+'（上次成功整理）：\n'+'\n'.join(entries))
        if not lines:
            return ''
        # 只给数据。"旧摘要不能否定用户最新陈述"这条规则在 prompts/chat_system.txt 里——
        # 那是聊天侧该管的事，不该由摘要服务塞进正文。
        return '\n\n'.join(lines)

    def _launch(self,user,persona):
        if self._memory_busy(user,persona):
            return
        key=(user,persona)
        with self._lock:
            if key in self._active:
                return
            self._active.add(key)
        drain_through=self.repo.latest_change(user,persona)
        def worker():
            try:
                while not self.runtime.closing.is_set() and not self._memory_busy(user,persona) and (job:=self.repo.pending(user,persona)) is not None:
                    out=self.run(job['id'])
                    if out['status']!='succeeded':
                        break
                    if self.repo.enqueue(user,persona,until_id=drain_through) is None:
                        break
            except Exception:
                logging.getLogger(__name__).exception('摘要任务失败')
            finally:
                self.conn.close_current()
                with self._lock:
                    self._active.discard(key)
        threading.Thread(target=worker,daemon=True,name='summary-worker').start()

    def _memory_busy(self,user,persona):
        return MemoryJobRepo(self.conn).busy(user,persona)

    def resume_pending(self,auto_evolve=True):
        # Only initialized scopes auto-evolve. Legacy summaries require one explicit initialization.
        scopes=self.repo.recovery_scopes(auto_evolve)
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
