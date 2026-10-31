"""应用宿主的共同生命周期；业务判断仍由会话和记忆模块拥有。"""
import logging
import threading


class ApplicationLifecycle:
    def __init__(self, conn, tasks, generations, memory, conversations, sessions, *, idle_minutes=120, interval=15):
        self.conn = conn
        self.tasks = tasks
        self.generations = generations
        self.memory = memory
        self.conversations = conversations
        self.sessions = sessions
        self.idle_minutes = idle_minutes
        self.interval = interval
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError('上一次应用扫描仍在停止中')
                return
            self.tasks.start()
            self.generations.start()
            stop = threading.Event()
            self._stop = stop
            self._thread = threading.Thread(target=self._run, args=(stop,), daemon=True, name='application-scan')
            self._thread.start()

    def _run(self, stop):
        while not stop.is_set():
            self.tick()
            stop.wait(self.interval)

    def _idle_sessions(self):
        for session in self.conversations.list_active_sessions():
            try:
                self.sessions.close_if_stale(session['user_id'], session['persona_id'], self.idle_minutes)
            except PermissionError:
                continue

    def tick(self):
        """一次宿主扫描；单条链路故障不阻断其他恢复入口。"""
        with self._tick_lock:
            try:
                for name, action in (('回复恢复', self.generations.recover),
                                     ('记忆调度', self.memory.tick), ('空闲边界', self._idle_sessions)):
                    if self._stop.is_set():
                        break
                    try:
                        action()
                    except Exception:
                        logging.getLogger(__name__).exception('%s失败', name)
            finally:
                self.conn.close_current()

    def stop(self):
        with self._lock:
            self._stop.set()
            thread = self._thread
            # Fence writes before returning; neither path waits for a model.
            try:
                self.generations.shutdown()
            finally:
                self.tasks.shutdown()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self.conn.close_current()


def build_lifecycle(cfg, conn, services):
    """仅在装配阶段把业务入口连接到宿主。"""
    return ApplicationLifecycle(conn, services.extraction.runtime, services.generation,
                                services.background, services.conversation, services.session_close,
                                idle_minutes=cfg.idle_session_minutes)
