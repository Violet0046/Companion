"""A role opportunity chooses its destination and message in one model call."""
from datetime import datetime, timedelta, timezone
import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from companion_ever.conversations.reply import split_assistant_reply
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.infrastructure.llm.json_output import clean_response
from companion_ever.infrastructure.tracing import call_context
from companion_ever.runtime.errors import describe_error
from companion_ever.utils.time import parse_sqlite_ts
from companion_ever.accounts.usage import LimitExceeded


class Destination(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['private','group']
    conversation_id:StrictInt=Field(gt=0)


class ProactiveReply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision:Literal['speak','skip']
    destination:Destination|None
    messages:list[StrictStr]=Field(max_length=3)


class ProactiveService:
    def __init__(self,repo,authorizer,settings,prompt,llm,notifications,*,tz_offset_hours=8):
        self.repo=repo;self.authorizer=authorizer;self.settings=settings
        self.prompt=prompt;self.llm=llm;self.notifications=notifications
        self.zone=timezone(timedelta(hours=tz_offset_hours));self.groups=None

    def group_setting(self,user,cid,enabled=None):
        self.groups.require(user,cid)
        with transaction(self.repo.conn):
            if enabled is not None:self.groups.group_repo.set_proactive(cid,enabled)
            return {'enabled':bool(self.groups.require(user,cid)['proactive_enabled'])}

    def setting(self,user,persona,enabled=None):
        self.authorizer.require_persona(user,persona)
        with transaction(self.repo.conn):
            if enabled is not None:
                self.settings.set_proactive(user,persona,enabled)
                if not enabled:self.repo.cancel_automatic(user,persona)
            return {'enabled':self.settings.proactive_enabled(user,persona)}

    def _scopes(self,user,persona,*,test=False,conversation_id=None,excluding_generation=''):
        scopes=[]
        if conversation_id is None:
            private=self.repo.proactive_snapshot(user,persona,excluding_generation=excluding_generation)
            if private:scopes.append(private)
        if self.groups is not None:
            ids=[conversation_id] if conversation_id is not None else self.groups.group_repo.list_ids(user)
            for cid in ids:
                group=self.groups.require(user,cid);members=self.groups.group_repo.members(cid,persona)
                if not members:
                    if conversation_id is not None:raise PermissionError('这个角色不在指定群聊中')
                    continue
                if not group['proactive_enabled'] and not (test and conversation_id is not None):continue
                snapshot=self.repo.proactive_snapshot(user,conversation_id=cid,excluding_generation=excluding_generation)
                snapshot['member_revision']=members[0]['revision'];snapshot['joined_after_message_id']=members[0]['joined_after_message_id']
                snapshot['member_state']=[[m['persona_id'],m['revision']] for m in self.groups.members(user,cid)]
                scopes.append(snapshot)
        return scopes

    def _source(self,user,persona,*,test=False,conversation_id=None):
        scopes=self._scopes(user,persona,test=test,conversation_id=conversation_id)
        sources=[s for s in scopes if s['user_id'] and s['user_id']>s.get('joined_after_message_id',0)]
        return max(sources,key=lambda s:s['user_id']) if sources else None

    def _actor_busy(self,user,persona):
        return bool(self.repo.conn.execute("SELECT 1 FROM generation g JOIN conversation c ON c.id=g.conversation_id "
            "WHERE c.user_id=? AND g.status IN ('queued','running') AND (g.actor_persona_id=? OR "
            "(c.kind='private' AND c.persona_id=?) OR EXISTS(SELECT 1 FROM json_each(g.target_persona_ids) WHERE value=?)) LIMIT 1",
            (user,persona,persona,persona)).fetchone())

    def check(self,user,persona,*,test=False,conversation_id=None):
        self.authorizer.require_persona(user,persona)
        snapshot=self._source(user,persona,test=test,conversation_id=conversation_id)
        if not snapshot:return {'allowed':False,'reason':'请先与这个角色聊过，指定群也需允许主动联系'}
        snapshot=dict(snapshot);snapshot['busy']=snapshot['busy'] or self._actor_busy(user,persona)
        snapshot['unread_proactive']=bool(self.repo.conn.execute("SELECT 1 FROM conversation_log m JOIN generation g ON g.id=m.generation_id "
            "JOIN conversation c ON c.id=m.conversation_id WHERE c.user_id=? AND g.origin='proactive' AND g.actor_persona_id=? "
            "AND m.id>c.last_read_message_id LIMIT 1",(user,persona)).fetchone())
        return self._eligibility(user,snapshot,self.settings.proactive_enabled(user,persona),test=test)

    def _eligibility(self,user,snapshot,enabled,*,test):
        if snapshot['busy']:return {'allowed':False,'reason':'已有回复或主动任务待处理'}
        try:self.repo.usage.require_capacity(user)
        except LimitExceeded as exc:return {'allowed':False,'reason':str(exc)}
        if not test:
            now=datetime.now(self.zone)
            start=now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            reason=('尚未开启这个角色的主动联系' if not enabled else
                '当前处于免打扰时段（22:00—10:00）' if not 10<=now.hour<22 else
                '上次用户发言距今不足四小时' if time.time()-parse_sqlite_ts(snapshot['user_at']).timestamp()<4*3600 else
                '上一条主动消息尚未读' if snapshot['unread_proactive'] else
                '今天已安排过主动联系' if self.repo.automatic_attempted(user,start) else '')
            if reason:return {'allowed':False,'reason':reason}
        return {'allowed':True,'reason':None}

    def trigger(self,user,persona,*,delay=0,test=False,request_id=None,conversation_id=None):
        if not 0<=delay<=3600:raise ValueError('延迟必须在 0—3600 秒之间')
        self.authorizer.require_persona(user,persona)
        gid='proactive-'+uuid.uuid5(uuid.NAMESPACE_URL,json.dumps([user,persona,request_id])).hex if request_id else 'proactive-'+uuid.uuid4().hex
        with transaction(self.repo.conn):
            old=self.repo.get(gid)
            if old:
                previous=json.loads(old['opportunity_json']) if old['opportunity_json'] else {}
                if bool(old['test_mode'])!=bool(test) or previous.get('forced_group_id')!=conversation_id:
                    raise ValueError('同一 request_id 不能用于不同触发模式或目标范围')
                return old
            eligibility=self.check(user,persona,test=test,conversation_id=conversation_id)
            if not eligibility['allowed']:raise ValueError(eligibility['reason'])
            source=self._source(user,persona,test=test,conversation_id=conversation_id)
            return self.repo.enqueue_proactive(user,persona,source,gid,delay=delay,test=test,forced_group_id=conversation_id)

    def tick(self):
        candidates=[]
        for user,persona in self.repo.enabled_targets():
            try:
                self.authorizer.require_persona(user,persona);source=self._source(user,persona)
                if source:candidates.append((source['user_id'],user,persona))
            except PermissionError:continue
        for _,user,persona in sorted(candidates,reverse=True):
            try:self.trigger(user,persona,request_id='automatic-'+datetime.now(self.zone).date().isoformat())
            except (PermissionError,ValueError):continue

    def _history(self,snapshot):
        rows=self.repo.conn.execute("SELECT id,sender_kind,sender_id,sender_name,content,created_at,quoted_message_id FROM conversation_log "
            "WHERE conversation_id=? AND id>? AND role IN ('user','assistant','system') ORDER BY id DESC LIMIT 12",
            (snapshot['id'],snapshot.get('joined_after_message_id',0))).fetchall()
        history=[]
        for row in reversed(rows):
            message=dict(row);qid=message.pop('quoted_message_id')
            message['quoted_message']=self.repo.messages.quote(snapshot['id'],qid) if qid and qid>snapshot.get('joined_after_message_id',0) else None
            for item in (message,message['quoted_message']):
                if item:
                    stamp=parse_sqlite_ts(item.get('created_at'))
                    item['created_at']=stamp.isoformat() if stamp else None
            history.append(message)
        return history

    def _prepare(self,g):
        opportunity=json.loads(g['opportunity_json']) if g['opportunity_json'] else {
            'source':self.repo.proactive_snapshot(g['user_id'],g['persona_id'],excluding_generation=g['id']),'forced_group_id':None}
        user=g['user_id'];persona=g['persona_id'];forced=opportunity['forced_group_id']
        if forced is None:self.repo.messages.ensure_conversation(user,persona)
        scopes=self._scopes(user,persona,test=bool(g['test_mode']),conversation_id=forced,excluding_generation=g['id'])
        scopes=[s for s in scopes if not s['busy'] and (g['test_mode'] or not s['unread_proactive'])]
        groups=[s for s in scopes if s['kind']=='group' and s['user_id'] and s['user_id']>s['joined_after_message_id']]
        group=max(groups,key=lambda s:s['user_id']) if groups else None
        selected=[s for s in scopes if s['kind']=='private']+([group] if group else [])
        opportunity['destinations']=selected
        self.repo.conn.execute('UPDATE generation SET opportunity_json=? WHERE id=?',(json.dumps(opportunity),g['id']))
        views=[]
        for scope in selected:
            views.append({'kind':scope['kind'],'conversation_id':scope['id'],'name':scope['name'] if scope['kind']=='group' else '与用户私聊',
                'members':[m['persona_id'] for m in self.groups.members(user,scope['id'])] if scope['kind']=='group' else [persona],
                'member_names':{m['persona_id']:m['name'] for m in self.groups.members(user,scope['id'])} if scope['kind']=='group' else {},
                'messages':self._history(scope) if scope['kind']=='group' or group is None else []})
        return selected,views

    def _destination_valid(self,g,selected):
        current=self.repo.proactive_snapshot(g['user_id'],conversation_id=selected['id'],excluding_generation=g['id'])
        if not current or current['last_id']!=selected['last_id'] or current['busy']:return False
        if selected['kind']=='private':
            row=self.repo.conn.execute('SELECT persona_id FROM conversation WHERE id=?',(selected['id'],)).fetchone()
            return row['persona_id']==g['persona_id']
        try:member=self.groups.require_member(g['user_id'],selected['id'],g['persona_id'])
        except PermissionError:return False
        roster=[[m['persona_id'],m['revision']] for m in self.groups.members(g['user_id'],selected['id'])]
        return member['revision']==selected['member_revision'] and roster==selected['member_state'] and (current['proactive_enabled'] or g['test_mode'] and json.loads(self.repo.get(g['id'])['opportunity_json'])['forced_group_id']==selected['id'])

    def reply_events(self,generation):
        gid=generation['id'];user=generation['user_id'];persona=generation['persona_id']
        self.authorizer.require_persona(user,persona)
        if not self.repo.claim(gid):return
        try:
            if not self._active(generation):return
            yield {'type':'start','generation_id':gid}
            with transaction(self.repo.conn):
                if not self._active(generation):return
                selected,views=self._prepare(generation)
            if not selected:
                self.repo.finish(gid,'cancelled','没有当前可用的主动联系目标');return
            prompt=self.prompt.build_proactive(user,persona,test=bool(generation['test_mode']),destinations=views)
            with transaction(self.repo.conn):
                if not self._active(generation):return
                reserved=self.repo.conn.execute("UPDATE generation SET model_requests=model_requests+1 WHERE id=? AND model_requests=0 AND status='running'",(gid,)).rowcount
                if not reserved:raise ValueError('主动机会已使用模型调用预算')
            with call_context('chat',user,persona,generation_id=gid,purpose='proactive'):
                raw=self.llm.chat(prompt['messages'])
            if not self._active(generation):return
            body,thinking=split_assistant_reply(raw,getattr(self.llm,'last_reasoning',None))
            reply=ProactiveReply.model_validate_json(clean_response(body))
            if reply.decision=='skip':
                if reply.destination is not None or reply.messages or generation['test_mode']:raise ValueError('主动联系静默格式无效，测试模式应生成消息')
                self.repo.finish(gid,'cancelled','模型选择暂不联系')
                yield {'type':'done','generation_id':gid,'skipped':True};return
            if not reply.destination or not reply.messages or any(not m.strip() or len(m)>16000 for m in reply.messages):raise ValueError('主动联系回复格式无效')
            destination=next((s for s in selected if s['id']==reply.destination.conversation_id and s['kind']==reply.destination.kind),None)
            if destination is None:raise ValueError('模型选择了候选范围外的主动联系目标')
            parts=[m.strip() for m in reply.messages]
            with transaction(self.repo.conn):
                self.authorizer.require_persona(user,persona)
                if not self._active(generation):return
                if not self._destination_valid(generation,destination):
                    self.repo.finish(gid,'cancelled','主动联系目标已变化');return
                saved=self.repo.save_proactive_messages(gid,destination,parts,thinking)
                if destination['kind']=='group':saved=[self.groups.present(row) for row in saved]
                if self.notifications is not None:
                    if destination['kind']=='group':
                        group=self.groups.require(user,destination['id'])
                        self.notifications.enqueue_group(user,destination['id'],group['name'],gid,saved[-1]['id'],persona,'\n'.join(parts))
                    else:self.notifications.enqueue(user,persona,gid,saved[-1]['id'],'\n'.join(parts))
                self.repo.finish(gid)
            for message in saved:yield {'type':'message_done','message':message,'generation_id':gid}
            yield {'type':'done','generation_id':gid}
        except Exception as exc:
            self.repo.finish(gid,'failed',describe_error(exc));raise
        finally:self.repo.finish(gid,'cancelled','主动任务执行中断')

    def _active(self,generation):
        if not generation['test_mode'] and not 10<=datetime.now(self.zone).hour<22:
            self.repo.finish(generation['id'],'cancelled','当前处于免打扰时段');return False
        return self.repo.active(generation['id'])
