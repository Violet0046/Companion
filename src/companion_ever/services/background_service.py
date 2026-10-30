"""Persisted work is recovered first; new note and summary triggers stay independent."""
import logging
import time
import json
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.storage.task_repo import TaskRepo


class BackgroundService:
    def __init__(self,ctx):
        self.ctx=ctx;self.conn=ctx.conn;self.cfg=ctx.cfg.background

    def memory_busy(self,user,persona):
        return bool(self.conn.execute("SELECT 1 FROM memory_job j JOIN conversation c ON c.id=j.conversation_id "
            "WHERE c.user_id=? AND c.persona_id=? AND j.status IN ('queued','running') LIMIT 1",(user,persona)).fetchone())

    def tick(self):
        # Recovery also applies to explicit jobs when automatic creation is off.
        if self.cfg.enabled:
            self._cooldown_retries()
        self.ctx.services.session_close.resume_pending()
        if self.cfg.enabled:
            for row in self.conn.execute('SELECT * FROM conversation').fetchall():
                try:
                    self._memory(row)
                except PermissionError:
                    continue
                except Exception:
                    logging.getLogger(__name__).exception('自动记忆触发失败')
        summary=self.ctx.services.summary
        if summary is not None:
            summary.resume_pending(auto_evolve=False)
            if self.cfg.enabled:
                scopes=self.conn.execute('SELECT DISTINCT user_id,persona_id FROM fact_change').fetchall()
                for row in scopes:
                    try:
                        self._summary(row['user_id'],row['persona_id'])
                    except PermissionError:
                        continue
                    except Exception:
                        logging.getLogger(__name__).exception('自动摘要触发失败')

    def _cooldown_retries(self):
        rows=self.conn.execute("SELECT a.* FROM task_attempt a WHERE a.status='failed' AND a.finished_at<=? "
            "AND a.failure_kind IN ('transport','model_timeout','provider_status','storage') "
            "AND a.sequence=(SELECT max(b.sequence) FROM task_attempt b WHERE b.kind=a.kind AND b.job_id=a.job_id)",
            (time.time()-self.cfg.failure_cooldown_seconds,)).fetchall()
        for row in rows:
            try:
                self.ctx.services.note.authorizer.require_persona(row['user_id'],row['persona_id'])
                TaskRepo(self.conn).cooldown_retry(row)
            except PermissionError:
                continue

    def _memory(self,conversation):
        user,persona=conversation['user_id'],conversation['persona_id'];cid=conversation['id']
        self.ctx.services.note.authorizer.require_persona(user,persona)
        if self.conn.execute("SELECT 1 FROM memory_job WHERE conversation_id=? AND status!='succeeded'",(cid,)).fetchone():
            return
        if self.conn.execute("SELECT 1 FROM generation WHERE conversation_id=? AND status IN ('queued','running')",(cid,)).fetchone():
            return
        cursor=self.conn.execute("SELECT max(?,coalesce(max(through_message_id),0)) FROM memory_job WHERE conversation_id=? AND status='succeeded'",
            (conversation['memory_start_message_id'],cid)).fetchone()[0]
        count,last,oldest=self.conn.execute("SELECT sum(role='user'),max(created_at),min(CASE WHEN role='user' THEN created_at END) "
                                    "FROM conversation_log WHERE conversation_id=? AND id>? "
                                    "AND role IN ('user','assistant')",(cid,cursor)).fetchone()
        if not count:
            return
        timestamp=parse_sqlite_ts(last)
        idle=timestamp is not None and time.time()-timestamp.timestamp()>=self.cfg.memory_idle_seconds
        first=parse_sqlite_ts(oldest)
        aged=first is not None and time.time()-first.timestamp()>=self.cfg.memory_max_wait_seconds
        if count>=self.cfg.memory_message_threshold or (idle and count>=self.cfg.memory_idle_min_messages) or aged:
            self.ctx.services.session_close.close_session_background(user,persona,boundary=False)

    def memory_settled(self,user,persona):
        """Reconsider summaries after a successful drain, independently of UI hooks."""
        if self.cfg.enabled and self.ctx.services.summary is not None:
            self._summary(user,persona)

    def _summary(self,user,persona):
        svc=self.ctx.services.summary;svc.authorizer.require_persona(user,persona)
        if self.memory_busy(user,persona) or svc.repo.pending(user,persona):
            return
        checkpoint=svc.repo.checkpoint(user,persona);cursor=checkpoint['last_change_id'] if checkpoint else 0
        window=svc.repo.change_window(user,persona,cursor)
        if not window['raw_count']:
            return
        changes=window['changes']
        document=json.loads(checkpoint['document_json']) if checkpoint else {}
        referenced={citation['note_id'] for block in ('user_profile','relationship_state')
                    for items in document.get(block,{}).values() for claim in items for citation in claim['citations']}
        invalidating=any(change['note_id'] in referenced for change in changes)
        oldest=min((change['created_at'] for change in changes),default=None)
        timestamp=parse_sqlite_ts(oldest)
        aged=timestamp is not None and time.time()-timestamp.timestamp()>=self.cfg.summary_interval_seconds
        # Zero net changes still consume the journal, but reuse the summary
        # without a model. Unreferenced changes wait for the normal cadence.
        if (not changes or checkpoint is None or invalidating or len(changes)>=self.cfg.summary_fact_threshold
                or aged or window['has_more']):
            if svc.repo.enqueue(user,persona):
                svc._launch(user,persona)
