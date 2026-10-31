"""Verify automatic work on a temporary copy, or the graph on fictional data.

Requires --model: sends the original task inputs to the configured provider.
With --memory-graph, only fictional inputs are sent and no source DB is read.
No refresh/retry API is called and no data is imported back to the source DB.
"""
import argparse
import json
import sqlite3
import tempfile
import time
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path
from fastapi.testclient import TestClient
from companion_ever.bootstrap.app import build_app
from companion_ever.config import AppConfig,WebConfig
from companion_ever.entrypoints.web.server import create_app
from companion_ever.runtime.tasks import runtime_for
from companion_ever.infrastructure.tracing import TraceStore,set_default_store
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.retrieval.query import MemorySearchResult


def verify_memory_graph(cfg,output):
    """Exercise the actual model/tool protocol with fictional data only."""
    with tempfile.TemporaryDirectory(prefix='companion-memory-graph-') as tmp:
        cfg.db_path=Path(tmp)/'graph.db'
        cfg.background=replace(cfg.background,enabled=False)
        ctx=build_app(cfg)
        store=TraceStore(output.with_suffix('.traces'));set_default_store(store)
        report={'synthetic':True,'model':cfg.llm.model,'trace_dir':str(store.base_dir),
                'prefetch_miss_injected':True}
        try:
            ctx.repos.user.create('graph-review')
            ctx.services.persona.create_persona('graph-review','one','{}')
            old=ctx.repos.note.create(user_id='graph-review',persona_id='one',kind=MemoryKind.FACT,
                content='用户长期喜欢乌龙茶。',reality='user_report')
            ctx.services.conversation.log_turn(user_id='graph-review',persona_id='one',role='user',turn_index=1,
                content='我以前说过长期喜欢乌龙茶，这一点一直没变。这次只是在确认那个偏好。')
            svc=ctx.services.extraction;search=svc.retrieval.search
            # Isolate the need for active retrieval; tool searches still use
            # the real retrieval service and database, with no supplied answers.
            with patch.object(svc.retrieval,'search',side_effect=lambda user,persona,query:
                              MemorySearchResult([],[],[],False) if query.recent_limit else search(user,persona,query)):
                result=svc.run({'user_id':'graph-review','persona_id':'one'})
            report['reconciliation']={k:result.get(k) for k in
                ('job_status','errors','model_calls','tool_calls','tool_rounds','facts_created','notes_updated','attempt_trace_id')}
            facts=ctx.conn.execute("SELECT id,content FROM note WHERE kind='fact'").fetchall()
            evidence=ctx.conn.execute('SELECT count(*) FROM note_evidence WHERE note_id=?',(old,)).fetchone()[0]
            report['fact_count']=len(facts);report['reused_original_fact']=len(facts)==1 and facts[0]['id']==old
            report['new_evidence_count']=evidence
            reconciled=(result.get('job_status')=='succeeded' and result.get('tool_calls',0)>0
                        and report['reused_original_fact'] and evidence==1)
            # A new batch with no lasting information should finish without
            # a note, rather than being forced to invent content or use tools.
            ctx.services.conversation.log_turn(user_id='graph-review',persona_id='one',role='user',turn_index=2,
                                               content='刚刚网络断了一下，现在恢复了，没别的事。')
            skipped=svc.run({'user_id':'graph-review','persona_id':'one'})
            report['no_memory']={k:skipped.get(k) for k in
                ('job_status','errors','model_calls','tool_calls','facts_created','notes_updated','attempt_trace_id')}
            report['success']=bool(reconciled and skipped.get('job_status')=='succeeded'
                                   and skipped.get('facts_created')==0 and skipped.get('notes_updated')==0)
            report['integrity']=ctx.conn.execute('PRAGMA integrity_check').fetchone()[0]
            report['foreign_key_errors']=[tuple(row) for row in ctx.conn.execute('PRAGMA foreign_key_check')]
            report['success'] &= report['integrity']=='ok' and not report['foreign_key_errors']
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            return int(not report['success'])
        finally:
            runtime_for(ctx.conn).shutdown();ctx.conn.close();set_default_store(None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',action='store_true',required=True)
    parser.add_argument('--deadline',type=int,default=1200)
    parser.add_argument('--output',default='memory/reviews/2026-10-30/background-v5.json')
    parser.add_argument('--resume-db',type=Path,help='从先前保留的私人测试副本继续，仍不写入来源副本')
    parser.add_argument('--memory-graph',action='store_true',help='仅用虚构临时数据验证记忆图和真实模型的只读工具协议，不读取正式库')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];output=(root/args.output).resolve()
    if not output.is_relative_to(root/'memory'/'reviews'):
        raise SystemExit('Output must stay under memory/reviews')
    cfg=AppConfig.from_env()
    if args.memory_graph:
        if args.resume_db:
            parser.error('--memory-graph cannot be combined with --resume-db')
        return verify_memory_graph(cfg,output)
    report={'copy_only':True,'automatic':True,'model':cfg.llm.model,
                                  'resumed_test_copy':args.resume_db is not None}
    with tempfile.TemporaryDirectory(prefix='companion-background-v5-') as tmp:
        source_path=(args.resume_db or Path(cfg.db_path)).resolve()
        if args.resume_db and not source_path.is_relative_to(root/'memory'/'reviews'):
            raise SystemExit('Resume database must stay under memory/reviews')
        source=sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)
        target=sqlite3.connect(str(Path(tmp)/'copy.db'))
        try:
            source.backup(target)
        finally:
            source.close();target.close()
        cfg.db_path=Path(tmp)/'copy.db';cfg.idle_session_minutes=0
        cfg.background=replace(cfg.background,enabled=True)
        ctx=build_app(cfg);runtime=runtime_for(ctx.conn)
        if args.resume_db:
            # Explicit development replay only; production recovery never resets
            # permanent validation failures without a corrected implementation.
            for row in ctx.conn.execute("SELECT id FROM memory_job WHERE status='failed' AND retry_at IS NULL").fetchall():
                ctx.services.extraction.jobs.retry(row['id'])
            for row in ctx.conn.execute("SELECT user_id,persona_id FROM summary_job WHERE status='failed' AND retry_at IS NULL").fetchall():
                ctx.services.summary.repo.enqueue(row['user_id'],row['persona_id'],manual=True)
        trace_dir=output.with_suffix('.traces')
        set_default_store(TraceStore(trace_dir))
        report['trace_dir']=str(trace_dir.relative_to(root))
        # Deliberately short leases prove renewal during real long model calls.
        runtime.lease_seconds=15;runtime.heartbeat_interval=2
        started=time.monotonic();last=None;success=False
        try:
            with TestClient(create_app(ctx,WebConfig())) as client:
                health=client.get('/api/health').json()
                assert health['auto_memory'] and health['schema_version']==6
                while time.monotonic()-started<args.deadline:
                    memory=[dict(r) for r in ctx.conn.execute("SELECT id,status,last_error,retry_at FROM memory_job WHERE status!='succeeded'")]
                    summary=[dict(r) for r in ctx.conn.execute("SELECT id,status,last_error,retry_at FROM summary_job WHERE status!='succeeded'")]
                    scopes=ctx.conn.execute('SELECT DISTINCT user_id,persona_id FROM fact_change').fetchall()
                    summaries=[ctx.services.summary.status(r['user_id'],r['persona_id']) for r in scopes]
                    attempts=[dict(r) for r in ctx.conn.execute('SELECT kind,status,stage,started_at,heartbeat_at FROM task_attempt ORDER BY started_at DESC LIMIT 2')]
                    view={'seconds':round(time.monotonic()-started,1),'memory':[r['status'] for r in memory],
                          'summary':[r['status'] for r in summary],'pending_changes':sum(s['pending_changes'] for s in summaries),
                          'executions':[{'kind':a['kind'],'status':a['status'],'stage':a['stage'],
                                         'renewed_for':round((a['heartbeat_at'] or a['started_at'])-a['started_at'],1)} for a in attempts]}
                    key=json.dumps({k:v for k,v in view.items() if k not in ('seconds','executions')})+str([(a['kind'],a['status'],a['stage']) for a in attempts])
                    if key!=last:
                        print(json.dumps(view,ensure_ascii=False),flush=True);last=key
                    failed=[r for r in memory+summary if r['status']=='failed' and r['retry_at'] is None]
                    if failed:
                        report['errors']=[r['last_error'] for r in failed];break
                    if not memory and all(s['current'] for s in summaries) and not summary and not runtime._active:
                        success=True;break
                    time.sleep(1)
            report.update(success=success,seconds=round(time.monotonic()-started,2),
                notes=ctx.conn.execute('SELECT count(*) FROM note').fetchone()[0],
                attempts=[{k:r[k] for k in ('kind','sequence','status','stage','started_at','heartbeat_at','failure_kind','error')}
                          for r in ctx.conn.execute('SELECT * FROM task_attempt ORDER BY started_at')])
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            snapshot=sqlite3.connect(str(output.with_suffix('.db')))
            try:
                ctx.conn.backup(snapshot)
            finally:
                snapshot.close()
            print(json.dumps({'success':success,'seconds':report['seconds'],'notes':report['notes']},ensure_ascii=False),flush=True)
        finally:
            runtime.shutdown();ctx.conn.close()
    return int(not success)


if __name__=='__main__':
    raise SystemExit(main())
