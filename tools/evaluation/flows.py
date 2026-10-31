"""Small cross-turn contract runner over production services and temporary databases."""
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from companion_ever.bootstrap.app import build_app
from companion_ever.infrastructure.llm.traced_client import TracedLLMClient,summarize_calls
from evaluation.memory import fingerprint
from companion_ever.memory.summaries.service import SummaryDocument,Profile,Relationship,claims
from companion_ever.infrastructure.tracing import TraceStore,set_default_store,trace_enabled


def resolve(value,aliases):
    if isinstance(value,str) and value.startswith('@'):
        return aliases[value[1:]]
    if isinstance(value,list):return [resolve(item,aliases) for item in value]
    if isinstance(value,dict):return {key:resolve(item,aliases) for key,item in value.items()}
    return value


class ReferenceModel:
    """Fixture responses exercise wiring; never score these as model quality."""
    model='reference-fixture';last_reasoning=None;last_usage=None
    response=None

    def chat(self,messages,**kwargs):
        payload=json.loads(messages[-1]['content'])
        if 'previous_summaries' in payload:
            document=SummaryDocument(user_profile=Profile(),relationship_state=Relationship()).model_dump()
            document['user_profile']['preferences']=[{'text':fact['content'],'reality':fact['reality'],
                'citations':[{'note_id':fact['id'],'quote':fact['content'][:160]}]} for fact in payload['current_facts'][:8]]
            return json.dumps(document,ensure_ascii=False)
        return json.dumps(self.response,ensure_ascii=False)


def check_contract(ctx,check,aliases,user,persona):
    kind=check['type']
    if kind=='count':
        table=check.get('table','note')
        if table=='note':
            result=ctx.conn.execute('SELECT count(*) FROM note WHERE user_id=? AND persona_id=? AND kind=?',
                                   (user,persona,check['kind'])).fetchone()[0]
        elif table in ('note_evidence','note_revision'):
            result=ctx.conn.execute('SELECT count(*) FROM '+table+' WHERE note_id=?',(aliases[check['note']],)).fetchone()[0]
        elif table=='note_link':
            result=ctx.conn.execute('SELECT count(*) FROM note_link l JOIN note source ON source.id=l.from_id '
                'JOIN note target ON target.id=l.to_id WHERE source.user_id=? AND source.persona_id=? '
                'AND target.user_id=? AND target.persona_id=? AND l.link_type=?',
                (user,persona,user,persona,check['relation'])).fetchone()[0]
        else:raise ValueError('unsupported contract table')
        return result==check['equals'],f'{table}: {result} != {check["equals"]}'
    if kind=='note':
        note=ctx.repos.note.get(aliases[check['note']])
        passed=note is not None and all(getattr(note,key)==value for key,value in check.get('fields',{}).items())
        passed &= note is not None and all(term in note.content for term in check.get('contains',[]))
        return passed,'note content/revision contract failed'
    if kind=='summary':
        document=ctx.services.summary.status(user,persona)['document'] or {}
        selected={c['note_id'] for claim in claims(document) for c in claim['citations']}
    elif kind=='recall':
        selected={note.id for note in ctx.services.retrieval.retrieve(user,persona,check['query'])}
    elif kind=='prompt':
        prompt=ctx.services.prompt.build(user,persona,check['query'])
        selected={nid for nid in aliases.values() if isinstance(nid,str) and
                  (note:=ctx.repos.note.get(nid)) is not None and note.content in prompt['system_prompt']}
        history='\n'.join(m['content'] for m in prompt['messages'] if m['role'] in ('user','assistant'))
        for name in check.get('history_excludes',[]):
            row=ctx.conn.execute('SELECT content FROM conversation_log WHERE id=?',(aliases[name],)).fetchone()
            if row['content'] in history:return False,'old source remains inside the history window'
    else:raise ValueError('unsupported contract check: '+kind)
    passed=all(aliases[name] in selected for name in check.get('includes',[])) and all(
        aliases[name] not in selected for name in check.get('excludes',[]))
    return passed,kind+' source membership contract failed'


def run_flow_suite(suite,cfg,output,*,reference=False):
    """Expectations stay in this runner; only stored messages reach the model."""
    if suite.get('version')!=1 or not suite.get('flows'):
        raise ValueError('invalid flow suite')
    if not trace_enabled():raise ValueError('跨轮评测需要开启 COMPANION_TRACE，才能核对逐次调用与成本覆盖')
    ids=[case['id'] for case in suite['flows']]
    if len(set(ids))!=len(ids):raise ValueError('duplicate flow IDs')
    output=Path(output);store=TraceStore(output.with_suffix('.traces'))
    report={'suite_sha256':fingerprint(suite),'mode':'reference' if reference else 'model',
            'model':'reference-fixture' if reference else cfg.llm.model,'cases':[],
            'contract_review_status':suite.get('contract_review_status','unreviewed'),'semantic_review_status':'pending',
            'notice':'跨轮工程契约探针，不是语义准确率。reference 仅验证链路；新增措辞和概括质量待人工审阅。'}
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        for case in suite['flows']:
            with tempfile.TemporaryDirectory(prefix='companion-flow-') as tmp:
                case_cfg=type(cfg)(Path(tmp)/'flow.db',llm=cfg.llm,extraction_timeout=cfg.extraction_timeout,
                    retrieval=cfg.retrieval,tz_offset_hours=cfg.tz_offset_hours,history_max_messages=cfg.history_max_messages,
                    idle_session_minutes=0,background=replace(cfg.background,enabled=False))
                ctx=build_app(case_cfg);set_default_store(store)
                user='flow-'+case['id'];persona='one';aliases={}
                row={'id':case['id'],'name':case['name'],'status':'passed','steps':[],'failures':[]}
                report['cases'].append(row);ref=ReferenceModel()
                if reference:
                    client=TracedLLMClient(ref);ctx.services.extraction.llm_client=client;ctx.services.summary.llm=client
                ctx.repos.user.create(user);ctx.services.persona.create_persona(user,persona,'{"name":"小伴"}')
                try:
                    for index,step in enumerate(case['steps']):
                        started=time.perf_counter();result={'index':index,'action':step['action'],'checks':[]}
                        row['steps'].append(result)
                        if step['action']=='messages':
                            for message in step['messages']:
                                mid=ctx.services.conversation.log_turn(user_id=user,persona_id=persona,role=message['role'],
                                                                     content=message['content'],turn_index=index)
                                aliases[message['id']]=mid
                                ctx.conn.execute("UPDATE conversation_log SET created_at='2026-10-29 10:00:00' WHERE id=?",(mid,))
                        elif step['action']=='extract':
                            if reference:ref.response=resolve(step['reference'],aliases)
                            out=ctx.services.extraction.run({'user_id':user,'persona_id':persona})
                            if out.get('job_status')!='succeeded':raise ValueError(str(out.get('errors')))
                            for name,selector in step.get('bind',{}).items():
                                matches=[note for nid in set(out['note_ids'].values()) if (note:=ctx.repos.note.get(nid))
                                    and note.kind==selector['kind'] and selector['contains'] in note.content]
                                if len(matches)!=1:raise ValueError('could not uniquely bind '+name)
                                aliases[name]=matches[0].id
                        elif step['action']=='summary':
                            before_calls=sum(len(d.get('llm_calls',[])) for _,d in store.list_traces(user_id=user,kind='summary',limit=1000))
                            request=ctx.services.summary.request(user,persona,background=False)
                            job=ctx.services.summary.repo.get(request['job_id']) if request['job_id'] else None
                            while job:
                                out=ctx.services.summary.run(job['id'])
                                if out['status']!='succeeded':raise ValueError(out['last_error'])
                                job=ctx.services.summary.repo.enqueue(user,persona)
                            after_calls=sum(len(d.get('llm_calls',[])) for _,d in store.list_traces(user_id=user,kind='summary',limit=1000))
                            if 'model_calls' in step and after_calls-before_calls!=step['model_calls']:
                                row['failures'].append('unexpected summary model call count')
                        elif step['action']=='delete':
                            ctx.conn.execute('DELETE FROM note WHERE id=? AND user_id=? AND persona_id=?',
                                             (aliases[step['note']],user,persona))
                        elif step['action']=='history_roll':
                            for number in range(55):
                                ctx.services.conversation.log_turn(user_id=user,persona_id=persona,role='assistant',
                                    content=f'窗口占位短应答 {number}',turn_index=index)
                        elif step['action']!='check':raise ValueError('unsupported flow action')
                        for check in step.get('checks',[]):
                            passed,detail=check_contract(ctx,check,aliases,user,persona)
                            result['checks'].append({'passed':passed,'check':check})
                            if not passed:row['failures'].append(detail)
                        result['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
                except Exception as exc:
                    from companion_ever.runtime.errors import describe_error
                    row['failures'].append(describe_error(exc));row['status']='error'
                finally:
                    if row['failures'] and row['status']=='passed':row['status']='contract_failed'
                    row['usage_by_layer']={kind:summarize_calls([call for _,d in store.list_traces(user_id=user,kind=kind,limit=1000)
                        for call in d.get('llm_calls',[])]) for kind in ('chat','memory_attempt','summary')}
                    row['integrity']=ctx.conn.execute('PRAGMA integrity_check').fetchone()[0]
                    row['foreign_key_errors']=[tuple(r) for r in ctx.conn.execute('PRAGMA foreign_key_check')]
                    if row['integrity']!='ok' or row['foreign_key_errors']:
                        row['failures'].append('database integrity failed');row['status']='error'
                    ctx.services.extraction.runtime.shutdown();ctx.conn.close()
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(case['id']+': '+row['status'],flush=True)
        report['passed']=sum(row['status']=='passed' for row in report['cases'])
        report['total']=len(report['cases'])
        report['trace_write_error']=store._write_error_reported
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        lines=['# 跨轮记忆契约评测','',report['notice'],'',f"模式：{report['mode']}；通过：{report['passed']}/{report['total']}",
               f"工程预期：{report['contract_review_status']}；语义审阅：{report['semantic_review_status']}",
               f"输入指纹：{report['suite_sha256']}",'','| 场景 | 状态 | 整理调用 | 摘要调用 | 失败 |','|---|---|---:|---:|---|']
        for row in report['cases']:
            layers=row['usage_by_layer']
            lines.append(f"| {row['id']} {row['name']} | {row['status']} | {layers['memory_attempt']['calls']} | {layers['summary']['calls']} | {'; '.join(row['failures'])} |")
        output.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        return report
    finally:set_default_store(None)
