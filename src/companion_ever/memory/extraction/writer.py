"""Validated drafts become notes, evidence, links and job completion in one transaction."""
from companion_ever.domain.enums import MemoryKind
from companion_ever.memory.extraction.draft import validate_draft
from companion_ever.runtime.invariants import check_extraction_invariants
from companion_ever.infrastructure.database.connection import transaction
from companion_ever.runtime.errors import RetryableTaskError
from companion_ever.memory.notes.links import NoteLinkRepo


class MemoryWriter:
    def __init__(self, note_service, jobs):
        self.notes = note_service
        self.repo = note_service.note_repo
        self.conn = self.repo.conn
        self.jobs = jobs
        self.links = NoteLinkRepo(self.conn)

    def apply(self, case, data, job, token, snapshots):
        draft = validate_draft(case,data)
        violations = check_extraction_invariants(len(case['new_message_ids']),len(draft.proposals),[])
        if violations:
            raise ValueError('; '.join(v.message for v in violations))
        user, persona = job['user_id'], job['persona_id']
        ids, created, written, changes = {}, 0, [], 0
        with transaction(self.conn):
            self.notes.authorizer.require_persona(user,persona)
            self.jobs.require_owner(job['id'],token)
            group_id=job['conversation_id'] if job['conversation_kind']=='group' else None
            visible=self.repo.visible_ids(user,persona,group_id) if group_id is not None else None
            journal_start=self.conn.execute('SELECT coalesce(max(id),0) FROM fact_change').fetchone()[0]
            # Validate against current DB rows, rather than trusting the snapshot returned by the model.
            for nid, snapshot in snapshots.items():
                current = self.repo.get(nid)
                if current is None or (visible is not None and nid not in visible) or any(getattr(current,k)!=getattr(snapshot,k) for k in
                    ('revision','is_archived','content','kind','reality','event_time','time_basis','user_id','persona_id')):
                    raise RetryableTaskError('候选记忆已改变，请重新整理')
            for proposal in draft.proposals:
                for evidence in proposal.evidence:
                    row = self.conn.execute('SELECT * FROM conversation_log WHERE id=?',(evidence.message_id,)).fetchone()
                    source = next(m for m in case['messages'] if m['id']==evidence.message_id)
                    in_scope=self.jobs.source_visible(job,row)
                    if not in_scope or row['role']!=source['role'] or evidence.quote not in row['content'] or (group_id is not None and row['sender_id']!=source.get('sender_id')):
                        raise ValueError('数据库中的来源已改变或超出当前聊天')
                    if evidence.message_id in case['new_message_ids'] and not job['after_message_id'] < row['id'] <= job['through_message_id']:
                        raise ValueError('新证据不在任务固定范围内')
                target = snapshots.get(proposal.target_id)
                if proposal.action != 'create':
                    if target is None or target.is_archived or (target.user_id,target.persona_id)!=(user,persona) or (visible is not None and target.id not in visible):
                        raise ValueError('更新目标无效')
                    if self.conn.execute("SELECT 1 FROM note_link l JOIN note n ON n.id=l.from_id WHERE l.to_id=? "
                                         "AND l.link_type='supersedes' AND n.is_archived=0",(target.id,)).fetchone():
                        raise ValueError('不能更新已被取代的事实')
                if proposal.action == 'confirm':
                    if proposal.kind != 'fact' or proposal.content.strip() != target.content.strip():
                        raise ValueError('确认只能补充既有事实的证据；修改内容请使用 revise')
                    if proposal.reality != target.reality and target.reality != 'uncertain':
                        raise ValueError('确认不能改变现实情境')
                    nid = target.id
                    if target.reality == 'uncertain' and proposal.reality != 'uncertain':
                        self.conn.execute('UPDATE note SET reality=?,revision=revision+1 WHERE id=?',(proposal.reality,nid))
                elif proposal.action == 'append':
                    if proposal.kind != 'episode' or (proposal.reality != target.reality and target.reality != 'uncertain'):
                        raise ValueError('append 只补充同一现实情境的经历')
                    nid = target.id
                    self.conn.execute('INSERT INTO note_revision(note_id,revision,content,reality,event_time,time_basis,updated_at,job_id) '
                        'VALUES (?,?,?,?,?,?,?,?)',(nid,target.revision,target.content,target.reality,target.event_time,target.time_basis,target.updated_at,job['id']))
                    self.conn.execute('UPDATE note SET content=?,reality=?,event_time=?,time_basis=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                      (proposal.content,proposal.reality,proposal.event_time,proposal.time_basis,nid))
                    changes += 1
                else:
                    if proposal.action == 'revise' and proposal.kind != 'fact':
                        raise ValueError('revise 用于事实新版本；同一经历的纠正请 append')
                    # Episode identity is not its text: two equal descriptions can be distinct occurrences.
                    duplicate = None
                    if proposal.kind == 'fact' and proposal.action == 'create':
                        clause,extra=self.repo._allowed_sql(visible,'n.')
                        duplicate = self.conn.execute("SELECT n.id FROM note n WHERE user_id=? AND persona_id=? AND kind='fact' "
                            "AND trim(content)=? AND reality=? AND event_time IS ? AND is_archived=0 AND NOT EXISTS "
                            "(SELECT 1 FROM note_link l JOIN note newer ON newer.id=l.from_id WHERE l.to_id=n.id "
                            "AND l.link_type='supersedes' AND newer.is_archived=0)"+clause+" ORDER BY created_at,id LIMIT 1",
                            (user,persona,proposal.content.strip(),proposal.reality,proposal.event_time,*extra)).fetchone()
                    if duplicate:
                        nid = duplicate[0]
                    else:
                        nid = self.repo.create(user_id=user,persona_id=persona,kind=MemoryKind(proposal.kind),
                                               content=proposal.content,source_quote=proposal.evidence[0].quote,
                                               importance=proposal.importance,reality=proposal.reality,
                                               event_time=proposal.event_time,time_basis=proposal.time_basis)
                        created += 1
                if nid in written:
                    raise ValueError('同一批不能对同一记忆执行多次写入')
                written.append(nid)
                ids[proposal.id] = nid
                if visible is not None:visible.add(nid)
                for evidence in proposal.evidence:
                    self.conn.execute('INSERT OR IGNORE INTO note_evidence(note_id,message_id,quote) VALUES (?,?,?)',
                                      (nid,evidence.message_id,evidence.quote))
                if proposal.action == 'revise':
                    self.links.create(nid,target.id,'supersedes',user,persona,reason=proposal.reason,job_id=job['id'])
            for link in draft.links:
                self.links.create(ids.get(link.from_id,link.from_id),ids.get(link.to_id,link.to_id),link.relation,
                                  user,persona,reason=link.reason,job_id=job['id'])
            self.conn.execute('UPDATE fact_change SET source_job_id=? WHERE id>? AND user_id=? AND persona_id=? AND source_job_id IS NULL',
                              (job['id'],journal_start,user,persona))
            self.jobs.succeed(job['id'],token)
        return {'facts_created':created,'notes_updated':changes,'note_ids':ids}
