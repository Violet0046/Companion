"""会话边界只是触发器；抽取进度和失败恢复由 memory_job 管理。"""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time

from companion_ever.services.extraction_service import ExtractionReport, report_from_state
from companion_ever.storage.memory_job_repo import MemoryJobRepo
from companion_ever.trace import trace_scope
from companion_ever.utils.time import parse_sqlite_ts


@dataclass
class SessionCloseReport:
    marker_id: int
    extraction: ExtractionReport


class SessionCloseService:
    def __init__(self, authorizer, conversation_service, extraction_graph):
        self.authorizer = authorizer
        self.conversation_service = conversation_service
        self.extraction_graph = extraction_graph
        self.jobs = MemoryJobRepo(conversation_service.conv_repo.conn)
        self._closing = set()
        self._lock = threading.Lock()
        self.on_record_done = None

    def is_closing(self, user_id, persona_id):
        with self._lock:
            return (user_id,persona_id) in self._closing

    def close_if_stale(self, user_id, persona_id, idle_minutes):
        if idle_minutes <= 0:
            return False
        active = self.conversation_service.get_active_session(user_id=user_id,persona_id=persona_id)
        last = parse_sqlite_ts(active['last_message_at']) if active else None
        if last and (datetime.now(timezone.utc)-last).total_seconds() >= idle_minutes*60:
            return self.close_session_background(user_id,persona_id)
        return False

    def close_session_background(self, user_id, persona_id, *, boundary=True):
        self.authorizer.require_persona(user_id,persona_id)
        key=(user_id,persona_id)
        with self._lock:
            if key in self._closing:
                return False
            self._closing.add(key)
        try:
            self.jobs.enqueue(user_id,persona_id)
            marker=self.conversation_service.log_session_close(user_id,persona_id) if boundary else 0
        except Exception:
            with self._lock:
                self._closing.discard(key)
            raise
        def worker():
            try:
                self._run(user_id,persona_id,marker)
            except Exception:
                logging.getLogger(__name__).exception('记忆任务执行失败')
            finally:
                with self._lock:
                    self._closing.discard(key)
                self.jobs.conn.close_current()
        threading.Thread(target=worker,daemon=True,name='memory-job').start()
        return True

    def close_session(self, user_id, persona_id):
        self.authorizer.require_persona(user_id,persona_id)
        self.jobs.enqueue(user_id,persona_id)
        marker=self.conversation_service.log_session_close(user_id,persona_id)
        return self._run(user_id,persona_id,marker)

    def _run(self, user_id, persona_id, marker):
        with trace_scope('extraction',user_id,persona_id) as trace:
            if marker:
                trace.step('db.log_session_close',marker_id=marker)
            state=self.extraction_graph.invoke({'user_id':user_id,'persona_id':persona_id})
            report=report_from_state(state)
            if report.job_status == 'succeeded':
                self.jobs.enqueue(user_id,persona_id)
            trace.step('extraction.workflow',job_id=report.job_id,status=report.job_status,
                       facts_created=report.facts_created,errors=report.errors)
            result=SessionCloseReport(marker,report)
            if self.on_record_done and report.job_status not in ('queued','running'):
                try:
                    self.on_record_done(user_id,persona_id,result)
                except Exception:
                    logging.getLogger(__name__).exception('记忆完成通知失败')
            return result

    def resume_pending(self):
        """Startup/periodic recovery does not depend on session markers."""
        rows=self.jobs.conn.execute("SELECT DISTINCT c.user_id,c.persona_id FROM memory_job j "
            "JOIN conversation c ON c.id=j.conversation_id WHERE j.status='queued' OR "
            "(j.status='running' AND j.lease_expires_at<=?) OR "
            "(j.status='failed' AND j.retry_at<=?)",(time.time(),time.time())).fetchall()
        for row in rows:
            self.close_session_background(row['user_id'],row['persona_id'],boundary=False)
