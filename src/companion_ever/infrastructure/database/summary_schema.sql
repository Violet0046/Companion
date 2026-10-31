-- Immutable evidence of fact operations, including deletion. note_id intentionally has no FK.
CREATE TABLE IF NOT EXISTS fact_change (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
 note_id TEXT NOT NULL, operation TEXT NOT NULL, before_json TEXT, after_json TEXT,
 source_job_id TEXT REFERENCES memory_job(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fact_change_scope ON fact_change(user_id,persona_id,id);
CREATE TABLE IF NOT EXISTS summary_job (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
 after_change_id INTEGER NOT NULL, through_change_id INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_expires_at REAL, retry_at REAL,
 last_error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT,
 result_json TEXT, previous_blocks_json TEXT,
 CHECK(through_change_id>=after_change_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_pending ON summary_job(user_id,persona_id) WHERE status!='succeeded';
CREATE TABLE IF NOT EXISTS summary_checkpoint (
 user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
 last_change_id INTEGER NOT NULL, job_id TEXT NOT NULL REFERENCES summary_job(id),
 document_json TEXT NOT NULL, blocks_hash TEXT NOT NULL, generated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,persona_id)
);
CREATE TRIGGER IF NOT EXISTS fact_change_insert AFTER INSERT ON note WHEN new.kind='fact' BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json) VALUES (new.user_id,new.persona_id,new.id,'create',json_object('id',new.id,'kind',new.kind,'content',new.content,'reality',new.reality,'event_time',new.event_time,'time_basis',new.time_basis,'importance',new.importance,'is_archived',new.is_archived,'revision',new.revision,'source_quote',new.source_quote));
END;
CREATE TRIGGER IF NOT EXISTS fact_change_update AFTER UPDATE OF kind,content,reality,event_time,time_basis,importance,is_archived,source_quote ON note
WHEN (old.kind='fact' OR new.kind='fact') AND (old.kind IS NOT new.kind OR old.content IS NOT new.content OR old.reality IS NOT new.reality OR old.event_time IS NOT new.event_time OR old.time_basis IS NOT new.time_basis OR old.importance IS NOT new.importance OR old.is_archived IS NOT new.is_archived OR old.source_quote IS NOT new.source_quote) BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,before_json,after_json)
 VALUES(new.user_id,new.persona_id,new.id,CASE WHEN old.is_archived=0 AND new.is_archived=1 THEN 'archive'
 WHEN old.is_archived=1 AND new.is_archived=0 THEN 'restore' ELSE 'update' END,json_object('id',old.id,'kind',old.kind,'content',old.content,'reality',old.reality,'event_time',old.event_time,'time_basis',old.time_basis,'importance',old.importance,'is_archived',old.is_archived,'revision',old.revision,'source_quote',old.source_quote),json_object('id',new.id,'kind',new.kind,'content',new.content,'reality',new.reality,'event_time',new.event_time,'time_basis',new.time_basis,'importance',new.importance,'is_archived',new.is_archived,'revision',new.revision,'source_quote',new.source_quote));
END;
CREATE TRIGGER IF NOT EXISTS fact_change_delete BEFORE DELETE ON note WHEN old.kind='fact' BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,before_json) VALUES (old.user_id,old.persona_id,old.id,'delete',json_object('id',old.id,'kind',old.kind,'content',old.content,'reality',old.reality,'event_time',old.event_time,'time_basis',old.time_basis,'importance',old.importance,'is_archived',old.is_archived,'revision',old.revision,'source_quote',old.source_quote));
END;
CREATE TRIGGER IF NOT EXISTS fact_evidence_added AFTER INSERT ON note_evidence BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json)
 SELECT n.user_id,n.persona_id,n.id,'evidence_added',json_object('message_id',new.message_id,'quote',new.quote)
 FROM note n WHERE n.id=new.note_id AND n.kind='fact';
END;
CREATE TRIGGER IF NOT EXISTS fact_supersedes_added AFTER INSERT ON note_link WHEN new.link_type='supersedes' BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json,source_job_id)
 SELECT n.user_id,n.persona_id,n.id,'supersedes_added',json_object('from_id',new.from_id,'to_id',new.to_id),new.job_id
 FROM note n WHERE n.id=new.to_id AND n.kind='fact';
END;
CREATE TRIGGER IF NOT EXISTS fact_evidence_removed AFTER DELETE ON note_evidence BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json)
 SELECT n.user_id,n.persona_id,n.id,'evidence_removed',json_object('message_id',old.message_id,'quote',old.quote)
 FROM note n WHERE n.id=old.note_id AND n.kind='fact';
END;
CREATE TRIGGER IF NOT EXISTS fact_supersedes_removed AFTER DELETE ON note_link WHEN old.link_type='supersedes' BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json,source_job_id)
 SELECT n.user_id,n.persona_id,n.id,'supersedes_removed',json_object('from_id',old.from_id,'to_id',old.to_id),old.job_id
 FROM note n WHERE n.id=old.to_id AND n.kind='fact';
END;
CREATE TRIGGER IF NOT EXISTS fact_supersedes_changed_removed AFTER UPDATE OF from_id,to_id,link_type ON note_link
 WHEN old.link_type='supersedes' AND (old.from_id IS NOT new.from_id OR old.to_id IS NOT new.to_id OR old.link_type IS NOT new.link_type) BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json,source_job_id)
 SELECT n.user_id,n.persona_id,n.id,'supersedes_removed',json_object('from_id',old.from_id,'to_id',old.to_id),old.job_id
 FROM note n WHERE n.id=old.to_id AND n.kind='fact';
END;
CREATE TRIGGER IF NOT EXISTS fact_supersedes_changed_added AFTER UPDATE OF from_id,to_id,link_type ON note_link
 WHEN new.link_type='supersedes' AND (old.from_id IS NOT new.from_id OR old.to_id IS NOT new.to_id OR old.link_type IS NOT new.link_type) BEGIN
 INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json,source_job_id)
 SELECT n.user_id,n.persona_id,n.id,'supersedes_added',json_object('from_id',new.from_id,'to_id',new.to_id),new.job_id
 FROM note n WHERE n.id=new.to_id AND n.kind='fact';
END;
