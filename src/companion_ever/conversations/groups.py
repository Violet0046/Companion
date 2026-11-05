"""Owned group conversations and bounded, durable invitations on the shared generation runtime."""
from companion_ever.characters.repository import persona_name
import json
import logging
import time

from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictInt
from typing import Literal

from companion_ever.infrastructure.database.connection import transaction
from companion_ever.infrastructure.tracing import call_context
from companion_ever.conversations.reply import split_assistant_reply
from companion_ever.runtime.errors import describe_error
from companion_ever.conversations.group_repository import GroupRepo
from companion_ever.infrastructure.llm.json_output import clean_response


MAX_GROUP_SPEAKERS=3


class Handoff(BaseModel):
    model_config = ConfigDict(extra='forbid')
    persona_id: StrictStr
    message_index: StrictInt = Field(ge=0,le=2)


class GroupReply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['speak','skip']
    channel: Literal['group'] | None
    messages: list[StrictStr] = Field(max_length=3)
    handoff: Handoff | None = None


class Invitation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    persona_id: StrictStr
    anchor_message_ids: list[StrictInt] = Field(max_length=4)
    suggestion: str = Field(max_length=120)
    destination_hint: Literal['group']


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invitations: list[Invitation] = Field(max_length=2)


class GroupService:
    def __init__(self, generations, authorizer, prompt, llm):
        self.repo=generations; self.conn=generations.conn; self.authorizer=authorizer
        self.prompt=prompt; self.llm=llm
        self.group_repo=GroupRepo(self.conn)
        self.notifications=None
        self.on_member_left=None

    def active(self,gid):
        return self.repo.active(gid)

    def visible(self,user,cid,visible):
        with transaction(self.conn):
            self.require(user,cid);self.group_repo.set_visible(cid,visible)
            if not visible and self.notifications is not None:self.notifications.wake_group(cid)

    def notification_allowed(self,user,cid,mid):
        try:
            self.require(user,cid)
            row=self.group_repo.notification(cid,mid)
            if row is None:return False
            self.require_member(user,cid,row['sender_id'])
        except PermissionError:return False
        return {'defer_until':row['visible_until']} if row['visible_until']>time.time() else True

    def _notify_saved(self,gid):
        if self.notifications is None:return
        with transaction(self.conn):
            g=self.repo.get(gid)
            if not g or g['status']!='running':return
            outputs=self.repo.outputs(gid)
            if not outputs:return
            try:group=self.require(g['user_id'],g['conversation_id'])
            except PermissionError:return
            self.notifications.enqueue_group(g['user_id'],group['id'],group['name'],gid,outputs[-1]['id'],outputs[-1]['sender_id'],
                '\n'.join((m.get('sender_name') or persona_name(self.conn,g['user_id'],m['sender_id']))+'：'+m['content'] for m in outputs))

    def require(self,user,cid):
        row=self.group_repo.owned(user,cid)
        if row is None:raise PermissionError('群聊不存在或不可访问')
        return row

    def members(self,user,cid):
        self.require(user,cid)
        return [{**m,'name':persona_name(self.conn,user,m['persona_id'])} for m in self.group_repo.members(cid) if self.authorizer.owns_persona(user,m['persona_id'])]

    def require_member(self,user,cid,persona):
        self.require(user,cid);self.authorizer.require_persona(user,persona)
        rows=self.group_repo.members(cid,persona)
        if not rows:raise PermissionError('角色已不在这个群聊中')
        return rows[0]

    def create(self,user,name,personas):
        name=name.strip()
        if not name or len(name)>60 or not 2<=len(personas)<=8 or len(set(personas))!=len(personas):
            raise ValueError('请输入群名，并选择2至8个不同角色')
        with transaction(self.conn):
            for persona in personas:self.authorizer.require_persona(user,persona)
            cid=self.group_repo.create(user,name,personas)
        return self.info(user,cid)

    def delete(self,user,cid):
        with transaction(self.conn):
            self.require(user,cid)
            self.group_repo.delete(cid)

    def info(self,user,cid):
        return {**self.require(user,cid),'members':self.members(user,cid)}

    def list(self,user):
        return [self.info(user,cid) for cid in self.group_repo.list_ids(user)]

    def leave(self,user,cid,persona):
        with transaction(self.conn):
            self.require_member(user,cid,persona)
            if len(self.members(user,cid))<=2:raise ValueError('群聊至少保留两位角色，请先添加成员再移出')
            self.group_repo.leave(cid,persona)
            self.group_repo.event(user,cid,persona_name(self.conn,user,persona)+'已被移出群聊')
            if self.on_member_left is not None:self.on_member_left(user,persona,cid)
        return self.info(user,cid)

    def add_members(self,user,cid,personas):
        if not personas or len(personas)>8 or len(set(personas))!=len(personas):raise ValueError('请选择不同的角色')
        with transaction(self.conn):
            self.require(user,cid)
            existing={m['persona_id'] for m in self.members(user,cid)}
            if len(existing|set(personas))>8:raise ValueError('群聊最多八位角色')
            for persona in personas:
                self.authorizer.require_persona(user,persona)
                if persona in existing:raise ValueError('角色已经在群里，请刷新成员列表')
            for persona in personas:
                self.group_repo.join(cid,persona)
                self.group_repo.event(user,cid,persona_name(self.conn,user,persona)+'加入了群聊')
        return self.info(user,cid)

    def remove_persona(self,user,persona):
        """Runs inside role deactivation's transaction, before access is revoked."""
        ids=[cid for cid in self.group_repo.list_ids(user) if self.group_repo.members(cid,persona)]
        for cid in ids:
            if len(self.members(user,cid))<=2:raise ValueError('角色所在群聊至少需两位角色，请先补充成员再删除角色')
        for cid in ids:self.leave(user,cid,persona)

    def present(self,row):
        m=dict(row)
        m['mentions']=json.loads(m.pop('mentions_json','[]'))
        m['quoted_message']=self.repo.messages.quote(m['conversation_id'],m['quoted_message_id']) if m.get('quoted_message_id') is not None else None
        return m

    def history(self,user,cid,*,before_id=None,after_id=None,limit=50,messages_only=False):
        self.require(user,cid)
        return [self.present(r) for r in self.group_repo.history(cid,before_id=before_id,after_id=after_id,limit=limit,messages_only=messages_only)]

    def unread(self,user,cid,after_id=0):
        group=self.require(user,cid)
        return self.group_repo.unread(cid,max(after_id,group['last_read_message_id']))

    def mark_read(self,user,cid,mid):
        with transaction(self.conn):
            self.require(user,cid)
            self.group_repo.mark_read(cid,mid)

    def receive(self,user,cid,content,client_id=None,*,quoted_message_id=None,mentions=None):
        # Preserve the exact text so mention offsets also survive emoji/zero-width sequences.
        if not isinstance(content,str):raise ValueError('消息内容必须是文本')
        if not content.strip() or len(content)>16000:raise ValueError('消息不能为空或超过16000字')
        # Offsets refer to the exact untrimmed content (Unicode code points).
        mentions=mentions or []
        if not isinstance(mentions,list) or len(mentions)>8:raise ValueError('@ 信息无效')
        normalized=[];targets=[];end=-1
        for mention in mentions:
            if not isinstance(mention,dict) or set(mention)!={'persona_id','start','end'}:raise ValueError('@ 信息无效')
            p=mention['persona_id'];start=mention['start'];stop=mention['end']
            if not isinstance(p,str) or type(start) is not int or type(stop) is not int or start<end or not 0<=start<stop<=len(content) or content[start:stop]!='@'+persona_name(self.conn,user,p):
                raise ValueError('@ 文本已变化，请重新选择角色')
            if stop<len(content) and (content[stop].isalnum() or content[stop]=='_'):
                raise ValueError('@ 文本已变化，请重新选择角色')
            normalized.append({'persona_id':p,'start':start,'end':stop});end=stop
            if p not in targets:targets.append(p)
        if len(targets)>8:raise ValueError('一次最多 @ 八位角色')
        with transaction(self.conn):
            self.require(user,cid)
            if quoted_message_id is not None:self.repo.messages.quote(cid,quoted_message_id)
            old=self.group_repo.receipt(cid,client_id)
            if old:
                if old['content']!=content or old['quoted_message_id']!=quoted_message_id or json.loads(old['mentions_json'])!=normalized:
                    raise ValueError('同一 client_message_id 不能用于不同内容、引用或 @')
                gid=self.group_repo.generation_for(old['id'])
                return self.repo.get(gid),False
            for p in targets:self.require_member(user,cid,p)
            self.repo.usage.require_capacity(user,replacing_conversation=cid,new_message=True)
            gid=self.group_repo.receive(user,cid,content,client_id,quoted_message_id,normalized,targets)
            return self.repo.get(gid),True

    def continue_group(self,user,cid,request_key,after_message_id=None):
        if not isinstance(request_key,str) or not request_key.strip() or len(request_key)>128:
            raise ValueError('续聊请求标识无效')
        with transaction(self.conn):
            self.require(user,cid)
            old=self.group_repo.continuation(cid,request_key)
            if old:
                generation=self.repo.get(old)
                if after_message_id is not None and after_message_id!=generation['context_message_id']:
                    raise ValueError('同一续聊请求标识不能用于不同消息')
                return generation,False
            if self.conn.execute("SELECT 1 FROM generation WHERE conversation_id=? AND status IN ('queued','running')",(cid,)).fetchone():
                raise ValueError('群里正在回复，请等这一轮结束后再继续')
            full_history=self.group_repo.history(cid,limit=50)
            history=[m for m in full_history if m['role']!='system']
            if not history:raise ValueError('先发一条消息，再邀请大家接话')
            anchor=history[-1]['id']
            if after_message_id is not None and after_message_id!=full_history[-1]['id']:
                raise ValueError('群聊已有新消息，请回到最新后再继续')
            self.repo.usage.require_capacity(user,new_message=True)
            gid=self.group_repo.continue_group(cid,anchor,request_key,full_history[-1]['id'])
            return self.repo.get(gid),True

    def _history_for(self,g,persona=None):
        user=g['user_id'];cid=g['conversation_id']
        history=self.history(user,cid,limit=self.prompt.history_max_messages)
        if persona:
            boundary=self.require_member(user,cid,persona)['joined_after_message_id']
            history=[m for m in history if m['id']>boundary]
            for m in history:
                if m.get('quoted_message_id') is not None and m['quoted_message_id']<=boundary:m['quoted_message']=None
        for m in history:
            m['created_at']=m['created_at'].replace(' ','T')+'Z'
            if m['quoted_message']:
                q=m['quoted_message'];q['created_at']=q['created_at'].replace(' ','T')+'Z'
        return [{k:m[k] for k in ('id','role','sender_kind','sender_id','sender_name','content','created_at','quoted_message','mentions')} for m in history]

    def _request(self,g,layer,persona,messages,purpose):
        with transaction(self.conn):
            self.require(g['user_id'],g['conversation_id'])
            if not self.active(g['id']):raise ValueError('本轮已结束')
            if not self.group_repo.reserve_call(g['id']):raise ValueError('本轮模型调用预算已用完')
        with call_context(layer,g['user_id'],persona or '',generation_id=g['id'],purpose=purpose):
            raw=self.llm.chat(messages)
        body,_=split_assistant_reply(raw,getattr(self.llm,'last_reasoning',None))
        if len(body)>50000:raise ValueError('模型输出过长')
        return body

    def _coordinate(self,g):
        targets=json.loads(g['target_persona_ids']);done=json.loads(g['completed_persona_ids'])
        members=self.members(g['user_id'],g['conversation_id'])
        eligible=[m['persona_id'] for m in members if m['persona_id'] not in targets]
        remaining=2-len(targets)
        if remaining<=0 or not eligible:plan=Plan(invitations=[])
        else:
            prompt=self.prompt.build_group_coordination(g['user_id'],members=members,eligible_ids=eligible,
                history=self._history_for(g),trigger_message_id=g['trigger_message_id'],spoken_ids=done,remaining=remaining,
                conversation_id=g['conversation_id'],manual=g['trigger_kind']=='manual_continue')
            plan=Plan.model_validate_json(clean_response(self._request(g,'coordination',None,prompt['messages'],'group_coordination')))
            if len(plan.invitations)>remaining:raise ValueError('协调邀请超过名额')
            seen=set()
            for inv in plan.invitations:
                if inv.persona_id not in eligible or inv.persona_id in seen:raise ValueError('协调模型选择了无效成员')
                self.require_member(g['user_id'],g['conversation_id'],inv.persona_id)
                visible={m['id'] for m in self._history_for(g,inv.persona_id)}
                if any(type(mid) is not int or mid not in visible for mid in inv.anchor_message_ids):raise ValueError('协调引用了不可见消息')
                seen.add(inv.persona_id)
        with transaction(self.conn):
            if not self.active(g['id']):return
            for inv in plan.invitations:self.require_member(g['user_id'],g['conversation_id'],inv.persona_id)
            self.group_repo.save_plan(g['id'],targets,plan.invitations)

    def _reply(self,g,persona):
        member=self.require_member(g['user_id'],g['conversation_id'],persona)
        invitation=self.group_repo.invitation(g['id'],persona)
        history=self._history_for(g,persona)
        targets=json.loads(g['target_persona_ids']);done=json.loads(g['completed_persona_ids'])
        member_rows=self.members(g['user_id'],g['conversation_id'])
        members=[m['persona_id'] for m in member_rows]
        pending=[p for p in targets if p not in done and p!=persona]
        # Explicit user selections all get a turn; automatic handoffs retain
        # the original three-speaker bound and cannot expand an all-member round.
        limit=max(MAX_GROUP_SPEAKERS,len(targets))
        source_id=invitation['source_message_id'] if invitation else None
        source=next((m for m in history if m['id']==(source_id or g['trigger_message_id'])),None)
        invocation={
            'kind':'ai_mention' if source_id else 'coordination' if invitation else 'user_mention',
            'round_trigger':g['trigger_kind'],
            'root_message_id':g['trigger_message_id'],
            'source_message_id':source_id or g['trigger_message_id'],
            'source_message':source,
            'member_names':{m['persona_id']:m['name'] for m in member_rows},
            'members':members,'already_spoken_ids':done,'pending_persona_ids':pending,
            'handoff_eligible_ids':[p for p in members if p!=persona and p not in done and (p in pending or len(targets)<limit)],
            'remaining_new_speakers':max(0,limit-len(targets)),
        }
        hint=invitation['suggestion'] if invitation else ''
        prompt=self.prompt.build_group(g['user_id'],persona,history=history,
            trigger_message_id=g['trigger_message_id'],suggestion=hint,
            conversation_id=g['conversation_id'],invocation=invocation)
        reply=GroupReply.model_validate_json(clean_response(self._request(g,'chat',persona,prompt['messages'],'group_reply')))
        if (reply.decision=='skip' and (reply.channel is not None or reply.messages)) or (reply.decision=='speak' and (reply.channel!='group' or not reply.messages or any(not m.strip() or len(m)>16000 for m in reply.messages))):
            raise ValueError('群回复格式无效')
        # Some models return a display name in this explicit routing field.
        # Resolve only an exact, unique current-member name; IDs take precedence.
        # Never infer an invitation from ordinary reply text or quoted @ mentions.
        if reply.handoff and reply.handoff.persona_id not in members:
            matches=[m['persona_id'] for m in member_rows if m['name']==reply.handoff.persona_id]
            if len(matches)==1:
                reply.handoff=reply.handoff.model_copy(update={'persona_id':matches[0]})
        saved=[]
        with transaction(self.conn):
            current_member=self.require_member(g['user_id'],g['conversation_id'],persona)
            if current_member['revision']!=member['revision'] or not self.active(g['id']):return []
            # Output and its next invitation commit together. An interruption can
            # retry the recipient without regenerating an already delivered speaker.
            texts=[m.strip() for m in reply.messages];mentions={};handoff=reply.handoff
            if handoff and (not texts or handoff.message_index>=len(texts) or handoff.persona_id not in invocation['handoff_eligible_ids']):
                logging.getLogger(__name__).warning('忽略无效群交棒：generation=%s persona=%s',g['id'],persona)
                handoff=None
            if handoff:
                self.require_member(g['user_id'],g['conversation_id'],handoff.persona_id)
                i=handoff.message_index;token='@'+persona_name(self.conn,g['user_id'],handoff.persona_id)
                # Program-generated suffix keeps display and Unicode mention offsets
                # identical. Textual @ alone never schedules a model.
                if not texts[i].endswith(token):texts[i]+=' '+token
                if len(texts[i])>16000:raise ValueError('交棒消息过长')
                mentions[i]=[{'persona_id':handoff.persona_id,'start':len(texts[i])-len(token),'end':len(texts[i])}]
            rows=self.group_repo.save_reply(g,persona,texts,mentions,reply_to=source_id)
            if handoff:self.group_repo.save_handoff(g,handoff.persona_id,rows[handoff.message_index]['id'])
            saved=[self.present(row) for row in rows]
            if saved:self._notify_saved(g['id'])
        return saved

    def reply_events(self,g):
        gid=g['id'];self.require(g['user_id'],g['conversation_id'])
        if not self.repo.claim(gid):
            for m in self.repo.outputs(gid):yield {'type':'message_done','generation_id':gid,'message':m,'replayed':True}
            saved=self.repo.get(gid)
            status=saved['status'] if saved else 'cancelled';yield {'type':'done' if status=='completed' else status,'generation_id':gid}
            return
        try:
            yield {'type':'start','generation_id':gid}
            while self.active(gid):
                g=self.repo.get(gid)
                pending=[p for p in json.loads(g['target_persona_ids']) if p not in json.loads(g['completed_persona_ids'])]
                if pending:
                    for m in self._reply(g,pending[0]):yield {'type':'message_done','generation_id':gid,'message':m}
                elif not g['coordination_done']:self._coordinate(g)
                else:
                    self.repo.finish(gid);yield {'type':'done','generation_id':gid};return
            yield {'type':'cancelled','generation_id':gid}
        except Exception as exc:
            self.repo.finish(gid,'failed',describe_error(exc));raise
        finally:
            self.repo.finish(gid,'cancelled','群回复执行中断')
