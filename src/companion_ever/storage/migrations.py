"""Versioned, transactional migration; retain message IDs and thinking references."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute('PRAGMA user_version').fetchone()[0] >= 1:
        return
    has_history = conn.execute('SELECT count(*) FROM conversation_log').fetchone()[0]
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='conversation_log'").fetchone()
    old_sequence = seq[0] if seq else 0
    if has_history:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if path:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            backup = sqlite3.connect(str(Path(path)) + '.bak-before-jobs-' + stamp)
            try:
                conn.backup(backup)
            finally:
                backup.close()
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
            memory_start_message_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, persona_id)
        );
        INSERT INTO conversation(user_id,persona_id,memory_start_message_id)
          SELECT user_id,persona_id,max(id) FROM conversation_log GROUP BY user_id,persona_id;
        CREATE TABLE generation (
            id TEXT PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            trigger_message_id INTEGER NOT NULL REFERENCES conversation_log(id),
            status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT, completed_at TEXT, lease_expires_at REAL,
            last_error TEXT
        );
        CREATE TABLE conversation_log_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','session_close')),
            content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            sender_kind TEXT NOT NULL, sender_id TEXT NOT NULL,
            client_message_id TEXT, reply_to_message_id INTEGER REFERENCES conversation_log(id),
            generation_id TEXT REFERENCES generation(id), output_index INTEGER,
            UNIQUE(conversation_id, client_message_id), UNIQUE(generation_id, output_index)
        );
        INSERT INTO conversation_log_new
          (id,user_id,persona_id,turn_index,role,content,created_at,conversation_id,sender_kind,sender_id)
          SELECT l.id,l.user_id,l.persona_id,l.turn_index,l.role,l.content,l.created_at,c.id,
          CASE l.role WHEN 'user' THEN 'user' WHEN 'assistant' THEN 'persona' ELSE 'system' END,
          CASE l.role WHEN 'user' THEN l.user_id ELSE l.persona_id END
          FROM conversation_log l JOIN conversation c ON c.user_id=l.user_id AND c.persona_id=l.persona_id;
        DROP TABLE conversation_log;
        ALTER TABLE conversation_log_new RENAME TO conversation_log;
        CREATE INDEX idx_log_user_persona ON conversation_log(user_id,persona_id,turn_index);
        CREATE INDEX idx_message_conversation ON conversation_log(conversation_id,id);
        CREATE INDEX idx_generation_pending ON generation(status,lease_expires_at);
        CREATE TABLE memory_job (
            id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversation(id),
            after_message_id INTEGER NOT NULL, through_message_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
            attempt_count INTEGER NOT NULL DEFAULT 0, retry_at REAL,
            lease_token TEXT, lease_expires_at REAL, last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT, completed_at TEXT,
            CHECK(through_message_id > after_message_id)
        );
        CREATE UNIQUE INDEX idx_memory_one_pending ON memory_job(conversation_id) WHERE status != 'succeeded';
        CREATE TABLE note_evidence (
            note_id TEXT NOT NULL REFERENCES note(id) ON DELETE CASCADE,
            message_id INTEGER NOT NULL REFERENCES conversation_log(id),
            quote TEXT NOT NULL, PRIMARY KEY(note_id,message_id,quote)
        );
        PRAGMA user_version=1;
        ''')
        conn.execute("UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name='conversation_log'",(old_sequence,))
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Migration would break foreign keys')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
