-- Operational metadata only: never duplicate chat, facts, prompts or model output.
CREATE TABLE IF NOT EXISTS task_attempt (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('memory','summary')),
 job_id TEXT NOT NULL, user_id TEXT NOT NULL, persona_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, owner_id TEXT NOT NULL, pid INTEGER,
 status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
 stage TEXT NOT NULL, started_at REAL NOT NULL, heartbeat_at REAL, finished_at REAL,
 failure_kind TEXT, error TEXT, range_json TEXT NOT NULL,
 UNIQUE(kind,job_id,sequence)
);
CREATE INDEX IF NOT EXISTS idx_task_attempt_scope ON task_attempt(user_id,persona_id,started_at);
CREATE TABLE IF NOT EXISTS task_event (
 id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL REFERENCES task_attempt(id),
 created_at REAL NOT NULL, stage TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_event_attempt ON task_event(attempt_id,id);
