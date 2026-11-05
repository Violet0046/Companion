"""Group storage; callers own transactions, authorization and model policy."""
import json
import uuid
import time


class GroupRepo:
    def __init__(self,conn):
        self.conn=conn

    def owned(self,user,cid):
        row=self.conn.execute("SELECT c.* FROM conversation c JOIN user u ON u.id=c.user_id "
            "WHERE c.id=? AND c.user_id=? AND c.kind='group' AND u.disabled=0",(cid,user)).fetchone()
        return dict(row) if row else None

    def set_proactive(self,cid,enabled):
        self.conn.execute('UPDATE conversation SET proactive_enabled=? WHERE id=?',(int(enabled),cid))
        if not enabled:
            self.conn.execute("UPDATE generation SET status='cancelled',last_error='群主动联系已关闭',completed_at=CURRENT_TIMESTAMP WHERE conversation_id=? AND origin='proactive' AND test_mode=0 AND status IN ('queued','running')",(cid,))

    def set_visible(self,cid,visible):
        self.conn.execute('UPDATE conversation SET visible_until=? WHERE id=?',(time.time()+45 if visible else 0,cid))

    def notification(self,cid,mid):
        row=self.conn.execute("SELECT m.sender_id,c.visible_until FROM conversation c JOIN conversation_log m ON m.conversation_id=c.id "
            "WHERE c.id=? AND m.id=? AND m.role='assistant' AND c.last_read_message_id<m.id AND NOT EXISTS(SELECT 1 FROM conversation_log newer "
            "WHERE newer.conversation_id=c.id AND newer.role='user' AND newer.id>m.id)",(cid,mid)).fetchone()
        return dict(row) if row else None

    def members(self,cid,persona=None):
        sql='SELECT * FROM conversation_member WHERE conversation_id=? AND left_after_message_id IS NULL'
        args=[cid]
        if persona is not None:sql+=' AND persona_id=?';args.append(persona)
        return [dict(r) for r in self.conn.execute(sql+' ORDER BY rowid',args)]

    def create(self,user,name,personas):
        cid=self.conn.execute("INSERT INTO conversation(user_id,kind,name) VALUES (?,'group',?)",(user,name)).lastrowid
        self.conn.executemany('INSERT INTO conversation_member(conversation_id,persona_id) VALUES (?,?)',[(cid,p) for p in personas])
        return cid

    def list_ids(self,user):
        return [r[0] for r in self.conn.execute("SELECT id FROM conversation WHERE user_id=? AND kind='group' ORDER BY id DESC",(user,))]

    def leave(self,cid,persona):
        last=self.conn.execute('SELECT coalesce(max(id),0) FROM conversation_log WHERE conversation_id=?',(cid,)).fetchone()[0]
        self.conn.execute('INSERT OR IGNORE INTO conversation_membership_history SELECT conversation_id,persona_id,revision,'
            'joined_after_message_id,? FROM conversation_member WHERE conversation_id=? AND persona_id=? AND left_after_message_id IS NULL',(last,cid,persona))
        self.conn.execute('UPDATE conversation_member SET left_after_message_id=?,revision=revision+1 WHERE conversation_id=? AND persona_id=?',(last,cid,persona))
        self.conn.execute("UPDATE push_delivery SET status='skipped',lease_token=NULL,last_error='群成员已变化' "
            "WHERE conversation_id=? AND status IN ('queued','running')",(cid,))
        self.cancel_round(cid)

    def cancel_round(self,cid):
        self.conn.execute("UPDATE generation SET status='cancelled',last_error='群成员已变化',completed_at=CURRENT_TIMESTAMP "
            "WHERE conversation_id=? AND status IN ('queued','running')",(cid,))

    def join(self,cid,persona):
        last=self.conn.execute('SELECT coalesce(max(id),0) FROM conversation_log WHERE conversation_id=?',(cid,)).fetchone()[0]
        self.conn.execute('INSERT INTO conversation_member(conversation_id,persona_id,joined_after_message_id) VALUES (?,?,?) '
            'ON CONFLICT(conversation_id,persona_id) DO UPDATE SET joined_after_message_id=excluded.joined_after_message_id,'
            'left_after_message_id=NULL,revision=conversation_member.revision+1',(cid,persona,last))
        self.cancel_round(cid)

    def event(self,user,cid,content):
        turn=self.conn.execute('SELECT coalesce(max(turn_index),0)+1 FROM conversation_log WHERE conversation_id=?',(cid,)).fetchone()[0]
        self.conn.execute("INSERT INTO conversation_log(user_id,conversation_id,turn_index,role,sender_kind,sender_id,content) "
            "VALUES (?,?,?,'system','system','system',?)",(user,cid,turn,content))

    def history(self,cid,*,before_id=None,after_id=None,limit=50,messages_only=False):
        sql="SELECT * FROM conversation_log WHERE conversation_id=? AND role IN ('user','assistant','system')"
        if messages_only:sql+=" AND role!='system'"
        args=[cid];direction='DESC'
        if before_id is not None:sql+=' AND id<?';args.append(before_id)
        elif after_id is not None:sql+=' AND id>?';args.append(after_id);direction='ASC'
        rows=list(self.conn.execute(sql+f' ORDER BY id {direction} LIMIT ?',(*args,limit)))
        return list(reversed(rows)) if direction=='DESC' else rows

    def unread(self,cid,after):
        row=self.conn.execute("SELECT count(*),coalesce(max(id),0) FROM conversation_log WHERE conversation_id=? AND role='assistant' AND id>?",(cid,after)).fetchone()
        return {'count':row[0],'latest_id':row[1]}

    def mark_read(self,cid,mid):
        self.conn.execute('UPDATE conversation SET last_read_message_id=max(last_read_message_id,?) WHERE id=? '
            "AND EXISTS(SELECT 1 FROM conversation_log WHERE id=? AND conversation_id=? AND role='assistant')",(mid,cid,mid,cid))

    def receipt(self,cid,client_id):
        return self.conn.execute('SELECT * FROM conversation_log WHERE conversation_id=? AND client_message_id=?',(cid,client_id)).fetchone() if client_id else None

    def generation_for(self,mid):
        return self.conn.execute("SELECT id FROM generation WHERE trigger_message_id=? AND origin='reply' ORDER BY rowid DESC LIMIT 1",(mid,)).fetchone()[0]

    def latest_generation_id(self,cid):
        row=self.conn.execute("SELECT id FROM generation WHERE conversation_id=? AND origin='reply' ORDER BY rowid DESC LIMIT 1",(cid,)).fetchone()
        return row[0] if row else None

    def retry(self,old):
        gid=uuid.uuid4().hex
        self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status,actor_persona_id,target_persona_ids,completed_persona_ids,coordination_done,trigger_kind,request_key,context_message_id) "
            "VALUES (?,?,?,'queued',?,?,?,?,?,?,?)",(gid,old['conversation_id'],old['trigger_message_id'],old['actor_persona_id'],old['target_persona_ids'],old['completed_persona_ids'],old['coordination_done'],old['trigger_kind'],old['request_key'],old['context_message_id']))
        self.conn.execute('INSERT INTO group_invitation(generation_id,persona_id,suggestion,anchor_message_ids,source_message_id) '
            'SELECT ?,persona_id,suggestion,anchor_message_ids,source_message_id FROM group_invitation WHERE generation_id=?',(gid,old['id']))
        return gid

    def continue_group(self,cid,anchor,request_key,context_id=None):
        gid=uuid.uuid4().hex
        self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status,trigger_kind,request_key,context_message_id) "
            "VALUES (?,?,?,'queued','manual_continue',?,?)",(gid,cid,anchor,request_key,context_id or anchor))
        return gid

    def continuation(self,cid,request_key):
        row=self.conn.execute('SELECT id FROM generation WHERE conversation_id=? AND request_key=? ORDER BY rowid DESC LIMIT 1',(cid,request_key)).fetchone()
        return row['id'] if row else None

    def receive(self,user,cid,content,client_id,quoted_message_id,mentions,targets):
        self.conn.execute("UPDATE generation SET status='cancelled',completed_at=CURRENT_TIMESTAMP,last_error='用户已发来新群消息' "
            "WHERE origin='proactive' AND status IN ('queued','running') AND actor_persona_id IN "
            "(SELECT persona_id FROM conversation_member WHERE conversation_id=? AND left_after_message_id IS NULL) "
            "AND conversation_id IN (SELECT id FROM conversation WHERE user_id=?)",(cid,user))
        self.conn.execute("UPDATE generation SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE conversation_id=? AND status IN ('queued','running')",(cid,))
        turn=self.conn.execute('SELECT coalesce(max(turn_index),0)+1 FROM conversation_log WHERE conversation_id=?',(cid,)).fetchone()[0]
        mid=self.conn.execute("INSERT INTO conversation_log(user_id,turn_index,role,content,conversation_id,sender_kind,sender_id,client_message_id,quoted_message_id,mentions_json) "
            "VALUES (?,?,'user',?,?,'user',?,?,?,?)",(user,turn,content,cid,user,client_id or uuid.uuid4().hex,quoted_message_id,json.dumps(mentions))).lastrowid
        gid=uuid.uuid4().hex
        self.conn.execute("INSERT INTO generation(id,conversation_id,trigger_message_id,status,actor_persona_id,target_persona_ids,coordination_done) VALUES (?,?,?,'queued',?,?,?)",
            (gid,cid,mid,targets[0] if targets else None,json.dumps(targets),int(bool(targets))))
        return gid

    def reserve_call(self,gid):
        # Ordinary coordination/handoffs retain four calls. Explicit @ selections
        # may contain all eight members, processed sequentially without a coordinator.
        return self.conn.execute("UPDATE generation SET model_requests=model_requests+1 WHERE id=? "
            "AND model_requests<min(8,max(4,json_array_length(target_persona_ids))) AND status='running'",(gid,)).rowcount==1

    def save_plan(self,gid,targets,invitations):
        for inv in invitations:
            self.conn.execute('INSERT INTO group_invitation(generation_id,persona_id,suggestion,anchor_message_ids) VALUES (?,?,?,?)',
                (gid,inv.persona_id,inv.suggestion,json.dumps(inv.anchor_message_ids)))
        self.conn.execute('UPDATE generation SET target_persona_ids=?,coordination_done=1 WHERE id=?',
            (json.dumps(targets+[inv.persona_id for inv in invitations]),gid))

    def invitation(self,gid,persona):
        row=self.conn.execute('SELECT * FROM group_invitation WHERE generation_id=? AND persona_id=?',(gid,persona)).fetchone()
        return dict(row) if row else None

    def save_handoff(self,g,persona,source_id):
        targets=json.loads(g['target_persona_ids'])
        if persona not in targets:targets.append(persona)
        # User-selected members have no invitation row; their explicit order and
        # trigger take precedence. Existing coordinator invitations can be clarified.
        if persona not in json.loads(g['target_persona_ids']) or self.invitation(g['id'],persona):
            self.conn.execute('INSERT INTO group_invitation(generation_id,persona_id,suggestion,anchor_message_ids,source_message_id) '
                "VALUES (?,?,'',?,?) ON CONFLICT(generation_id,persona_id) DO UPDATE SET suggestion='',source_message_id=excluded.source_message_id,anchor_message_ids=excluded.anchor_message_ids",
                (g['id'],persona,json.dumps([source_id]),source_id))
        self.conn.execute('UPDATE generation SET target_persona_ids=? WHERE id=?',(json.dumps(targets),g['id']))

    def save_reply(self,g,persona,messages,mentions=None,reply_to=None):
        saved=[]
        index=self.conn.execute('SELECT count(*) FROM conversation_log WHERE generation_id=?',(g['id'],)).fetchone()[0]
        turn=self.conn.execute('SELECT turn_index FROM conversation_log WHERE id=?',(g['trigger_message_id'],)).fetchone()[0]
        for offset,text in enumerate(messages):
            mid=self.conn.execute("INSERT INTO conversation_log(user_id,turn_index,role,content,conversation_id,sender_kind,sender_id,reply_to_message_id,generation_id,output_index,mentions_json) "
                "VALUES (?,?,'assistant',?,?,'persona',?,?,?,?,?)",(g['user_id'],turn,text.strip(),g['conversation_id'],persona,reply_to or g['trigger_message_id'],g['id'],index+offset,json.dumps((mentions or {}).get(offset,[])))).lastrowid
            saved.append(self.conn.execute('SELECT * FROM conversation_log WHERE id=?',(mid,)).fetchone())
        done=json.loads(g['completed_persona_ids']);done.append(persona)
        self.conn.execute('UPDATE generation SET completed_persona_ids=? WHERE id=?',(json.dumps(done),g['id']))
        return saved
