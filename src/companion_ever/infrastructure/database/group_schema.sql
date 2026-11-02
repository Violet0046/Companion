-- Applied by migration 12 after the historical schema/migrations.
CREATE TABLE IF NOT EXISTS conversation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT,
 memory_start_message_id INTEGER NOT NULL DEFAULT 0, last_read_message_id INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 kind TEXT NOT NULL DEFAULT 'private' CHECK(kind IN ('private','group')), name TEXT NOT NULL DEFAULT '',
 CHECK((kind='private' AND persona_id IS NOT NULL) OR (kind='group' AND persona_id IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_private_conversation ON conversation(user_id,persona_id) WHERE kind='private';
CREATE TABLE IF NOT EXISTS conversation_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT,
 turn_index INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','session_close')),
 content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 conversation_id INTEGER NOT NULL REFERENCES conversation(id), sender_kind TEXT NOT NULL, sender_id TEXT NOT NULL,
 client_message_id TEXT, reply_to_message_id INTEGER REFERENCES conversation_log(id),
 generation_id TEXT REFERENCES generation(id), output_index INTEGER,
 quoted_message_id INTEGER REFERENCES conversation_log(id), mentions_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(conversation_id,client_message_id), UNIQUE(generation_id,output_index)
);
CREATE INDEX IF NOT EXISTS idx_log_user_persona ON conversation_log(user_id,persona_id,turn_index);
CREATE INDEX IF NOT EXISTS idx_message_conversation ON conversation_log(conversation_id,id);
CREATE INDEX IF NOT EXISTS idx_log_account_day ON conversation_log(user_id,role,created_at);
CREATE TABLE IF NOT EXISTS conversation_member (
 conversation_id INTEGER NOT NULL REFERENCES conversation(id), persona_id TEXT NOT NULL,
 joined_after_message_id INTEGER NOT NULL DEFAULT 0, left_after_message_id INTEGER,
 revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(conversation_id,persona_id)
);
CREATE TABLE IF NOT EXISTS group_invitation (
 generation_id TEXT NOT NULL REFERENCES generation(id), persona_id TEXT NOT NULL,
 suggestion TEXT NOT NULL, anchor_message_ids TEXT NOT NULL,
 PRIMARY KEY(generation_id,persona_id)
);
