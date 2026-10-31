"""One background model slot, durable execution stages and resilient lease renewal."""
from contextlib import contextmanager
import logging
import threading
import uuid
from companion_ever.runtime.repository import TaskRepo
from companion_ever.runtime.errors import describe_error,LeaseLostError
from companion_ever.infrastructure.tracing import record_step


_local=threading.local()


class TaskInterrupted(RuntimeError):
    def __init__(self,reason='execution_interrupted'):
        self.reason=reason
        super().__init__('后台执行已中断，原任务保留以便恢复：'+reason)


def checkpoint(stage=None,**detail):
    task=getattr(_local,'task',None)
    if task:
        task.check()
        if stage:
            task.event(stage,detail)
            record_step('task.'+stage,**detail)


class TaskExecution:
    def __init__(self,runtime,kind,repo,job):
        self.runtime=runtime;self.kind=kind;self.repo=repo;self.job=job
        self.stop=threading.Event();self.cancelled=threading.Event();self.reason=None
        self.thread=None

    def check(self):
        if self.cancelled.is_set() or self.runtime.closing.is_set():
            raise TaskInterrupted(self.reason or 'server_shutdown')

    def event(self,stage,detail=None):
        TaskRepo(self.runtime.conn).event(self.job['lease_token'],stage,detail)

    def interrupt(self,reason):
        self.reason=reason;self.cancelled.set()
        error=str(TaskInterrupted(reason))
        if self.kind=='memory':
            self.repo.interrupt(self.job['id'],self.job['lease_token'],error,failure_kind=reason)
        else:
            self.repo.interrupt(self.job,error,failure_kind=reason)

    def _heartbeat(self):
        conn=self.runtime.conn
        try:
            # Heartbeats have their own connection; a busy writer must not
            # stall renewal for the normal 30 second SQLite busy timeout.
            delay=self.runtime.heartbeat_interval
            opened=False
            while not self.stop.wait(delay):
                if self.cancelled.is_set():
                    break
                try:
                    if not opened:
                        conn.open_current(timeout=1)
                        conn.execute('PRAGMA busy_timeout=1000')
                        opened=True
                    renewed=(self.repo.renew(self.job['id'],self.job['lease_token'],lease_seconds=self.runtime.lease_seconds)
                             if self.kind=='memory' else self.repo.renew(self.job,lease_seconds=self.runtime.lease_seconds))
                    if not renewed:
                        self.interrupt('lease_lost')
                        break
                    delay=self.runtime.heartbeat_interval
                except Exception as exc:
                    logging.getLogger(__name__).warning('任务续租暂时失败：%s',describe_error(exc))
                    # Preserve the failure if possible; a failed diagnostic
                    # write must not terminate the renewal loop either.
                    try:
                        self.event('heartbeat_error',{'error':describe_error(exc)})
                    except Exception:
                        pass
                    delay=min(3.0,self.runtime.heartbeat_interval)
        finally:
            conn.close_current()


class TaskRuntime:
    def __init__(self,conn,*,lease_seconds=300,heartbeat_interval=30):
        self.conn=conn;self.lease_seconds=lease_seconds;self.heartbeat_interval=heartbeat_interval
        self.owner_id=uuid.uuid4().hex;self.closing=threading.Event()
        self._slot=threading.BoundedSemaphore(1);self._lock=threading.Lock();self._active={}

    @contextmanager
    def slot(self):
        gate=self._slot
        owner=self.owner_id
        while not gate.acquire(timeout=.2):
            if self.closing.is_set() or owner != self.owner_id:
                raise TaskInterrupted('server_shutdown')
        try:
            if self.closing.is_set() or owner != self.owner_id:
                raise TaskInterrupted('server_shutdown')
            yield
        finally:
            gate.release()

    @contextmanager
    def execute(self,kind,repo,job):
        task=TaskExecution(self,kind,repo,job);token=job['lease_token']
        with self._lock:
            self._active[token]=task
        previous=getattr(_local,'task',None);_local.task=task
        task.thread=threading.Thread(target=task._heartbeat,daemon=True,name=kind+'-lease')
        task.thread.start()
        try:
            task.check()
            yield task
        except LeaseLostError as exc:
            task.interrupt('lease_lost')
            raise TaskInterrupted('lease_lost') from exc
        except BaseException as exc:
            # The service records model failures itself. Only a still-running
            # owner is changed here, so committed successes/failures stay intact.
            if isinstance(exc,TaskInterrupted) or not isinstance(exc,Exception):
                task.interrupt(task.reason or 'execution_interrupted')
            raise
        finally:
            task.stop.set();task.thread.join(timeout=1)
            _local.task=previous
            with self._lock:
                self._active.pop(token,None)

    def shutdown(self):
        self.closing.set()
        with self._lock:
            active=list(self._active.values())
        for task in active:
            try:
                task.interrupt('server_shutdown')
            except Exception:
                logging.getLogger(__name__).exception('记录任务中断失败，将由租约接管恢复')

    def start(self):
        if self.closing.is_set():
            self.owner_id=uuid.uuid4().hex
            self._slot=threading.BoundedSemaphore(1)
            self.closing.clear()


def runtime_for(conn):
    # Services sharing a database proxy share the same background model slot.
    # It is separate from chat generation; ordinary replies remain independent.
    runtime=vars(conn).get('_task_runtime')
    if runtime is None:
        runtime=TaskRuntime(conn)
        conn._task_runtime=runtime
    return runtime
