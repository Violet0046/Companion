"""回复执行入口；订阅者只控制投递，不拥有生成任务的生命周期。"""
from __future__ import annotations

import logging
import threading
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import chain

from companion_ever.conversations.stream_filter import ThinkFilter
from companion_ever.infrastructure.database.connection import transaction


@dataclass
class _Execution:
    listeners: list[Callable[[dict], None]] = field(default_factory=list)
    finished: bool = False
    cancelled: bool = False


class GenerationService:
    """HTTP、WS 和其他宿主共用的后台执行、查询、重试与恢复能力。

    回调必须快速返回。回调抛错、断开或停止订阅不取消生成；SQLite claim
    仍是跨线程/进程的执行依据，内存注册表只避免本实例重复创建执行线程。
    """

    def __init__(self, chat_service, setting_service, *, proactive=None, groups=None):
        self.chat = chat_service
        self.repo = chat_service.generations
        self.conn = self.repo.conn
        self.authorizer = chat_service.conversation_service.authorizer
        self.settings = setting_service
        self.proactive = proactive
        self.groups = groups
        self.on_proactive_message = None
        self.on_group_message=None
        self._lock = threading.Lock()
        self._executions: dict[str, _Execution] = {}
        self._closing = False

    def start(self):
        with self._lock:
            self._closing = False

    def shutdown(self):
        with self._lock:
            self._closing = True
            for execution in self._executions.values():
                execution.cancelled = True
            self.repo.interrupt_running(tuple(self._executions))

    def submit(self, user, persona, content, client_message_id=None, *, on_event=None, quoted_message_id=None):
        generation, created = self.chat.receive(user, persona, content, client_message_id, quoted_message_id=quoted_message_id)
        # A repeated queued receipt also needs execution, even if its original
        # caller disappeared between committing the receipt and starting work.
        if created or generation['status'] == 'queued' or on_event is not None:
            self._start(generation, on_event)
        return generation, created

    def submit_group(self,user,cid,content,client_message_id=None,*,on_event=None,quoted_message_id=None,mentions=None):
        generation,created=self.groups.receive(user,cid,content,client_message_id,quoted_message_id=quoted_message_id,mentions=mentions)
        if created or generation['status']=='queued' or on_event is not None:self._start(generation,on_event)
        return generation,created

    def group_snapshot(self,user,cid,gid=None):
        import json
        self.groups.require(user,cid)
        if gid is None:
            gid=self.groups.group_repo.latest_generation_id(cid)
            if gid is None:return None
        g=self.repo.get(gid)
        if g is None or g['conversation_id']!=cid:raise LookupError('生成任务不存在')
        pending=set(json.loads(g['target_persona_ids']))-set(json.loads(g['completed_persona_ids']))
        return {**g,'messages':[self.groups.present(m) for m in self.repo.outputs(gid)],
            'can_retry':g['origin']=='reply' and g['status']=='failed' and bool(pending or not g['coordination_done'])}

    def retry_group(self,user,cid,gid):
        import json
        with transaction(self.conn):
            old=self.group_snapshot(user,cid,gid)
            latest=self.group_snapshot(user,cid)
            if latest['trigger_message_id']!=old['trigger_message_id']:raise ValueError('只能重试最后一轮失败回复')
            if latest['id']!=gid:return latest
            if not old['can_retry']:raise ValueError('没有可重试的未完成任务，请继续发送消息')
            for p in json.loads(old['target_persona_ids']):self.groups.require_member(user,cid,p)
            self.repo.usage.require_capacity(user)
            retry_id=self.groups.group_repo.retry(old)
            g=self.repo.get(retry_id)
        self._start(g)
        return self.group_snapshot(user,cid,retry_id)

    def _owned(self, user, persona, generation_id):
        self.authorizer.require_persona(user, persona)
        generation = self.repo.get(generation_id)
        if generation is None or generation['conversation_kind']!='private' or (generation['user_id'], generation['persona_id']) != (user, persona):
            raise LookupError('生成任务不存在')
        return generation

    def state(self, user, persona, generation_id):
        return self._owned(user, persona, generation_id)

    def snapshot(self, user, persona, generation_id):
        generation = self._owned(user, persona, generation_id)
        outputs = self.repo.outputs(generation_id)
        if not self.settings.get_show_thinking(user, persona):
            for output in outputs:
                output.pop('thinking', None)
        return {**generation, 'messages': outputs,
                'can_retry': generation['origin']=='reply' and generation['status'] == 'failed' and not outputs}

    def latest(self, user, persona):
        self.authorizer.require_persona(user, persona)
        generation = self.repo.latest(user, persona)
        return self.snapshot(user, persona, generation['id']) if generation else None

    def retry(self, user, persona, generation_id, *, on_event=None):
        with transaction(self.conn):
            self._owned(user, persona, generation_id)
            generation, created = self.repo.retry(generation_id)
        if created or generation['status'] == 'queued' or on_event is not None:
            self._start(generation, on_event)
        return self.snapshot(user, persona, generation['id'])

    def cancel(self, user, persona, generation_id):
        with transaction(self.conn):
            self._owned(user, persona, generation_id)
            self.repo.cancel(generation_id)
        return self.snapshot(user, persona, generation_id)

    def recover(self):
        """只恢复 queued；过期 running 明确失败，保留已保存的完整气泡。"""
        self.repo.recover()
        for generation in self.repo.queued():
            try:
                if generation['conversation_kind']=='group':self.groups.require(generation['user_id'],generation['conversation_id'])
                else:self.authorizer.require_persona(generation['user_id'], generation['persona_id'])
            except PermissionError:
                self.repo.finish(generation['id'], 'failed', '角色访问权限已撤销')
                continue
            self._start(generation)

    def _start(self, generation, on_event=None):
        gid = generation['id']
        replay = False
        with self._lock:
            if self._closing:
                return  # The durable queued receipt resumes on the next start.
            active = self._executions.get(gid)
            if active is not None and active.cancelled:
                active = None
            if active is not None:
                if active.finished:
                    replay = on_event is not None
                elif on_event is not None:
                    active.listeners.append(on_event)
                if not replay:
                    return
            if not replay:
                execution = _Execution([on_event] if on_event else [])
                self._executions[gid] = execution
                try:
                    threading.Thread(target=self._run, args=(generation, execution),
                                     daemon=True, name='chat-generation').start()
                except BaseException:
                    self._executions.pop(gid, None)
                    raise
        if replay:
            # The registered execution already delivered its terminal event.
            # Replay persisted messages without calling or claiming a model.
            for event in self._events(generation):
                self._deliver(on_event, event)

    @staticmethod
    def _deliver(listener, event):
        try:
            listener(deepcopy(event))
        except Exception:
            logging.getLogger(__name__).debug('回复订阅已断开', exc_info=True)

    def _publish(self, execution, event):
        with self._lock:
            if execution.cancelled:
                return
            if event['type'] in ('done', 'error', 'cancelled', 'failed', 'running', 'queued'):
                execution.finished = True
            listeners = tuple(execution.listeners)
        for listener in listeners:
            self._deliver(listener, event)

    def _events(self, generation):
        """输出可投递的事件；模型原文和思考标签不进入正文增量。"""
        gid = generation['id']
        events = (self.groups.reply_events(generation) if generation['conversation_kind']=='group'
                  else self.proactive.reply_events(generation) if generation['origin']=='proactive' and self.proactive is not None
                  else self.chat.reply_events(generation))
        filt = ThinkFilter()
        try:
            for event in events:
                if event['type'] == 'delta':
                    content = filt.feed(event['content'])
                    if content:
                        yield {**event, 'content': content}
                elif event['type'] == 'message_done':
                    tail = filt.flush()
                    if tail:
                        yield {'type': 'delta', 'content': tail, 'generation_id': gid}
                    message = dict(event['message'])
                    show = generation['conversation_kind']=='private' and self.settings.get_show_thinking(generation['user_id'], generation['persona_id'])
                    thinking = message.get('thinking')
                    if not show:
                        message.pop('thinking', None)
                    elif thinking:
                        yield {'type': 'thinking', 'content': thinking, 'generation_id': gid}
                    yield {k: (message if k == 'message' else v)
                           for k, v in event.items() if k != 'raw'}
                    filt = ThinkFilter()
                elif event['type'] == 'failed':
                    saved = self.repo.get(gid)
                    yield {'type': 'error', 'generation_id': gid,
                           'message': saved.get('last_error') or '回复生成失败'}
                else:
                    yield event
        finally:
            events.close()

    def _run(self, generation, execution):
        gid = generation['id']
        events = self._events(generation)
        try:
            with self._lock:
                if self._closing or execution.cancelled:
                    return
                first = next(events, None)
            for event in chain([first] if first else [], events):
                if execution.cancelled:
                    break
                self._publish(execution, event)
                if generation['conversation_kind']=='group' and event['type']=='message_done' and self.on_group_message:
                    self._deliver(lambda item:self.on_group_message(generation['user_id'],generation['conversation_id'],item['message']),event)
                elif generation['origin']=='proactive' and event['type']=='message_done' and self.on_proactive_message:
                    self._deliver(lambda item: self.on_proactive_message(generation['user_id'],generation['persona_id'],item['message']),event)
        except Exception:
            logging.getLogger(__name__).exception('回复生成失败')
            try:
                saved = self.repo.get(gid) or {}
            except Exception:
                saved = {}
            self._publish(execution, {'type': 'error', 'generation_id': gid,
                                     'message': saved.get('last_error') or '回复生成失败'})
        finally:
            try:
                events.close()
            except Exception:
                logging.getLogger(__name__).debug('已停止的回复执行释放资源失败', exc_info=True)
            finally:
                with self._lock:
                    if self._executions.get(gid) is execution:
                        self._executions.pop(gid)
                self.conn.close_current()
