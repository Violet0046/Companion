"""记忆业务触发规则。持久恢复与新任务判断独立，不持有应用容器或 SQL。"""
import json
import logging
import time

from companion_ever.runtime.repository import TaskRepo
from companion_ever.utils.time import parse_sqlite_ts


class MemoryScheduler:
    def __init__(self, cfg, authorizer, conversations, sessions, summaries):
        self.cfg = cfg
        self.authorizer = authorizer
        self.conversations = conversations
        self.sessions = sessions
        self.summaries = summaries
        self.jobs = sessions.jobs
        self.tasks = TaskRepo(self.jobs.conn)

    def memory_busy(self, user, persona):
        return self.jobs.busy(user, persona)

    def tick(self):
        # Explicit jobs recover even when automatic creation is disabled.
        if self.cfg.enabled:
            self._cooldown_retries()
        self.sessions.resume_pending()
        if self.cfg.enabled:
            for conversation in self.conversations.list_conversations():
                try:
                    self.consider_memory(conversation)
                except PermissionError:
                    continue
                except Exception:
                    logging.getLogger(__name__).exception('自动记忆触发失败')
        self.summaries.resume_pending(auto_evolve=False)
        if self.cfg.enabled:
            for scope in self.summaries.repo.change_scopes():
                try:
                    self.consider_summary(scope['user_id'], scope['persona_id'])
                except PermissionError:
                    continue
                except Exception:
                    logging.getLogger(__name__).exception('自动摘要触发失败')

    def _cooldown_retries(self):
        for attempt in self.tasks.retry_candidates(self.cfg.failure_cooldown_seconds):
            try:
                self.authorizer.require_persona(attempt['user_id'], attempt['persona_id'])
                self.tasks.cooldown_retry(attempt)
            except PermissionError:
                continue

    def consider_memory(self, conversation):
        conversation=dict(conversation)
        user, persona = conversation['user_id'], conversation['persona_id']
        self.authorizer.require_persona(user, persona)
        if self.jobs.pending_for_conversation(conversation['id'],persona):
            return
        scope_options={'conversation_id':conversation['id']} if conversation.get('kind')=='group' else {}
        window = self.conversations.pending_input(user, persona, self.jobs.progress(conversation),**scope_options)
        if window['generating'] or not window['user_count']:
            return
        last = parse_sqlite_ts(window['last_at'])
        oldest = parse_sqlite_ts(window['oldest_user_at'])
        idle = last is not None and time.time() - last.timestamp() >= self.cfg.memory_idle_seconds
        aged = oldest is not None and time.time() - oldest.timestamp() >= self.cfg.memory_max_wait_seconds
        if (window['user_count'] >= self.cfg.memory_message_threshold
                or (idle and window['user_count'] >= self.cfg.memory_idle_min_messages) or aged):
            self.sessions.close_session_background(user, persona, boundary=False,**scope_options)

    def memory_settled(self, user, persona):
        """整理成功后重新检查摘要；不依赖页面通知回调。"""
        if self.cfg.enabled:
            self.consider_summary(user, persona)

    def consider_summary(self, user, persona):
        self.authorizer.require_persona(user, persona)
        repo = self.summaries.repo
        if self.memory_busy(user, persona) or repo.pending(user, persona):
            return
        checkpoint = repo.checkpoint(user, persona)
        cursor = checkpoint['last_change_id'] if checkpoint else 0
        window = repo.change_window(user, persona, cursor)
        if not window['raw_count']:
            return
        changes = window['changes']
        document = json.loads(checkpoint['document_json']) if checkpoint else {}
        referenced = {citation['note_id'] for block in ('user_profile', 'relationship_state')
                      for items in document.get(block, {}).values() for claim in items for citation in claim['citations']}
        invalidating = any(change['note_id'] in referenced for change in changes)
        oldest = parse_sqlite_ts(min((change['created_at'] for change in changes), default=None))
        aged = oldest is not None and time.time() - oldest.timestamp() >= self.cfg.summary_interval_seconds
        # Net-zero changes advance the journal without a model. Other changes
        # retain the established count, age and referenced-source priorities.
        if (not changes or checkpoint is None or invalidating
                or len(changes) >= self.cfg.summary_fact_threshold or aged or window['has_more']):
            self.summaries.schedule(user, persona)
