"""Proactive conversation policy and execution; uses shared generation persistence."""
from datetime import datetime, timedelta, timezone
import time
import uuid
import json

from companion_ever.conversations.reply import split_assistant_reply, split_messages
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.infrastructure.tracing import call_context
from companion_ever.runtime.errors import describe_error
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.accounts.usage import LimitExceeded


class ProactiveService:
    def __init__(self, repo, authorizer, settings, prompt, llm, notifications, *, tz_offset_hours=8):
        self.repo=repo;self.authorizer=authorizer;self.settings=settings
        self.prompt=prompt;self.llm=llm;self.notifications=notifications
        self.zone=timezone(timedelta(hours=tz_offset_hours))
        self.groups=None

    def group_setting(self,user,cid,enabled=None):
        self.groups.require(user,cid)
        with transaction(self.repo.conn):
            if enabled is not None:self.groups.group_repo.set_proactive(cid,enabled)
            return {'enabled':bool(self.groups.require(user,cid)['proactive_enabled'])}

    def check_group(self,user,cid,*,test=False):
        self.groups.require(user,cid)
        if not self.groups.members(user,cid):return {'allowed':False,'reason':'群内没有可发言的角色'}
        snapshot=self.groups.group_repo.proactive_snapshot(cid)
        return self._eligibility(user,snapshot,bool(snapshot and snapshot['proactive_enabled']),test=test,
            empty_reason='请先在群里聊过一句',label='这个群')

    def trigger_group(self,user,cid,*,delay=0,test=False,request_id=None):
        if not 0<=delay<=3600:raise ValueError('延迟必须在0—3600秒之间')
        self.groups.require(user,cid)
        gid='proactive-'+uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([user,'group',cid,request_id])).hex if request_id else 'proactive-'+uuid.uuid4().hex
        with transaction(self.repo.conn):
            old=self.repo.get(gid)
            if old:
                if bool(old['test_mode'])!=bool(test):raise ValueError('同一request_id不能用于不同触发模式')
                return old
            check=self.check_group(user,cid,test=test)
            if not check['allowed']:raise ValueError(check['reason'])
            snapshot=self.groups.group_repo.proactive_snapshot(cid)
            self.groups.group_repo.enqueue_proactive(cid,snapshot,gid,delay,test)
            return self.repo.get(gid)

    def setting(self, user, persona, enabled=None):
        self.authorizer.require_persona(user,persona)
        with transaction(self.repo.conn):
            if enabled is not None:
                self.settings.set_proactive(user,persona,enabled)
                if not enabled:
                    self.repo.cancel_automatic(user,persona)
            return {'enabled':self.settings.proactive_enabled(user,persona)}

    def check(self,user,persona,*,test=False):
        self.authorizer.require_persona(user,persona)
        return self._eligibility(user,self.repo.proactive_snapshot(user,persona),self.settings.proactive_enabled(user,persona),test=test,
            empty_reason='请先与这个角色聊过一句',label='这个角色')

    def _eligibility(self,user,snapshot,enabled,*,test,empty_reason,label):
        if not snapshot or not snapshot['user_id']:return {'allowed':False,'reason':empty_reason}
        if snapshot['busy']:return {'allowed':False,'reason':'已有回复或主动任务待处理'}
        try:self.repo.usage.require_capacity(user)
        except LimitExceeded as exc:return {'allowed':False,'reason':str(exc)}
        if not test:
            now=datetime.now(self.zone)
            start=now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            reason=(f'尚未开启{label}的主动联系' if not enabled else
                '当前处于免打扰时段（22:00—10:00）' if not 10<=now.hour<22 else
                '上次用户发言距今不足四小时' if time.time()-parse_sqlite_ts(snapshot['user_at']).timestamp()<4*3600 else
                '上一条主动消息尚未读' if snapshot['unread_proactive'] else
                '今天已安排过主动联系' if self.repo.automatic_attempted(user,start) else '')
            if reason:return {'allowed':False,'reason':reason}
        return {'allowed':True,'reason':None}

    def trigger(self, user, persona, *, delay=0, test=False, request_id=None):
        if not 0<=delay<=3600:
            raise ValueError('延迟必须在 0—3600 秒之间')
        self.authorizer.require_persona(user,persona)
        gid='proactive-'+uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([user,persona,request_id])).hex if request_id else 'proactive-'+uuid.uuid4().hex
        with transaction(self.repo.conn):
            old=self.repo.get(gid)
            if old:
                if bool(old['test_mode'])!=bool(test):
                    raise ValueError('同一 request_id 不能用于不同触发模式')
                return old
            eligibility=self.check(user,persona,test=test)
            if not eligibility['allowed']:
                raise ValueError(eligibility['reason'])
            snapshot=self.repo.proactive_snapshot(user,persona)
            return self.repo.enqueue_proactive(user,persona,snapshot,gid,delay=delay,test=test)

    def tick(self):
        candidates=[]
        for user,persona in self.repo.enabled_targets():
            snapshot=self.repo.proactive_snapshot(user,persona)
            candidates.append((snapshot['user_id'] or 0,user,persona,None))
        if self.groups is not None:
            for row in self.groups.group_repo.proactive_targets():
                snapshot=self.groups.group_repo.proactive_snapshot(row['id'])
                candidates.append((snapshot['user_id'] or 0,row['user_id'],None,row['id']))
        for _,user,persona,cid in sorted(candidates,key=lambda c:c[0],reverse=True):
            try:
                check=self.check_group(user,cid) if cid is not None else self.check(user,persona)
                if check['allowed']:
                    request_id='automatic-'+datetime.now(self.zone).date().isoformat()
                    if cid is not None:self.trigger_group(user,cid,request_id=request_id)
                    else:self.trigger(user,persona,request_id=request_id)
            except (PermissionError,ValueError):
                continue

    def reply_events(self, generation):
        gid=generation['id'];user=generation['user_id'];persona=generation['persona_id']
        self.authorizer.require_persona(user,persona)
        if not self.repo.claim(gid):
            return
        try:
            if not self._active(generation):
                return
            yield {'type':'start','generation_id':gid}
            prompt=self.prompt.build_proactive(user,persona,test=bool(generation['test_mode']))
            if not self._active(generation):
                return
            with call_context('chat',user,persona,generation_id=gid,purpose='proactive'):
                raw=self.llm.chat(prompt['messages'])
            body,thinking=split_assistant_reply(raw,getattr(self.llm,'last_reasoning',None))
            if body.strip()=='<no_contact>' and not generation['test_mode']:
                self.repo.finish(gid,'cancelled','模型选择暂不联系')
                yield {'type':'done','generation_id':gid,'skipped':True}
                return
            if not body.strip() or '<no_contact>' in body:
                raise ValueError('主动消息正文为空或格式无效')
            # Strip thought blocks before explicit-boundary / plain-paragraph splitting.
            parts=split_messages(body)
            saved=[]
            with transaction(self.repo.conn):
                self.authorizer.require_persona(user,persona)
                if not self._active(generation):
                    return
                for index,part in enumerate(parts):
                    message=self.repo.save_message(gid,index,part,thinking if index==0 else None)
                    if message is None:
                        raise ValueError('主动消息的上下文已经变化')
                    saved.append(message)
                if self.notifications is not None:
                    self.notifications.enqueue(user,persona,gid,saved[-1]['id'],'\n'.join(parts))
                self.repo.finish(gid)
            for message in saved:
                yield {'type':'message_done','message':message,'generation_id':gid}
            yield {'type':'done','generation_id':gid}
        except Exception as exc:
            self.repo.finish(gid,'failed',describe_error(exc))
            raise
        finally:
            self.repo.finish(gid,'cancelled','主动任务执行中断')

    def _active(self, generation):
        if not generation['test_mode'] and not 10<=datetime.now(self.zone).hour<22:
            self.repo.finish(generation['id'],'cancelled','当前处于免打扰时段')
            return False
        return self.repo.active(generation['id'])
