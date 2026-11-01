-- ============================================================
-- Companion Wiki Schema
-- ============================================================

-- === 用户表 ===
-- 用途：每个用户一行，persona_ids 用 CSV 存储该用户的所有 persona
CREATE TABLE IF NOT EXISTS user (
    id           TEXT PRIMARY KEY,
    persona_ids  TEXT NOT NULL DEFAULT '',
    login_name TEXT,
    password_hash TEXT,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
    auth_failures INTEGER NOT NULL DEFAULT 0,
    auth_locked_until REAL NOT NULL DEFAULT 0
);
-- === Wiki 笔记表（核心）===
-- 用途：所有抽取的事实，经历与事实，角色设定独立存储
CREATE TABLE IF NOT EXISTS note (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('episode','fact')), content TEXT NOT NULL CHECK(length(trim(content))>0),
    source_quote TEXT, importance REAL NOT NULL DEFAULT 0.5 CHECK(importance>=0 AND importance<=1),
    decay_score REAL NOT NULL DEFAULT 1.0, is_archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at TEXT, reality TEXT NOT NULL DEFAULT 'uncertain'
      CHECK(reality IN ('user_report','shared_fiction','uncertain')),
    event_time TEXT, time_basis TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1
);

-- 索引：用户×persona×类型 是最常用的过滤组合
CREATE INDEX IF NOT EXISTS idx_note_user_persona_kind
    ON note(user_id, persona_id, kind, is_archived);

-- 索引：按衰减分排序时用
CREATE INDEX IF NOT EXISTS idx_note_decay
    ON note(user_id, persona_id, decay_score DESC, is_archived);

-- 索引：按更新时间排序（最新在前）
CREATE INDEX IF NOT EXISTS idx_note_updated
    ON note(user_id, persona_id, updated_at DESC);

-- === 笔记关系图（A-Mem 风格，wiki 的核心差异化能力）===
-- 用途：表达笔记之间的因果/时序/细化等关系
CREATE TABLE IF NOT EXISTS note_link (
    id          TEXT PRIMARY KEY,
    from_id     TEXT NOT NULL REFERENCES note(id) ON DELETE CASCADE,
    to_id       TEXT NOT NULL REFERENCES note(id) ON DELETE CASCADE,
    link_type   TEXT NOT NULL CHECK(link_type IN ('supports','continues','supersedes')),
    strength    REAL NOT NULL DEFAULT 0.5 CHECK(strength>=0 AND strength<=1),
    reason TEXT NOT NULL DEFAULT '', job_id TEXT REFERENCES memory_job(id),
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_id, to_id, link_type), CHECK(from_id != to_id)
);

CREATE INDEX IF NOT EXISTS idx_link_from ON note_link(from_id);
CREATE INDEX IF NOT EXISTS idx_link_to   ON note_link(to_id);

-- === 用户画像与关系进展摘要 ===
-- 当前保留内容供查看；后续从有效 fact 独立演化，不与单批记忆整理耦合。
CREATE TABLE IF NOT EXISTS memory_block (
    id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES user(id),
    persona_id      TEXT NOT NULL,
    block_type      TEXT NOT NULL,
    fields_json     TEXT NOT NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, persona_id, block_type),
    CHECK (block_type IN (
        'user_profile', 'relationship_state'
    ))
);

-- === 原始对话存档 ===
-- 用途：每次对话追加一行
CREATE TABLE IF NOT EXISTS conversation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
 memory_start_message_id INTEGER NOT NULL DEFAULT 0, last_read_message_id INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(user_id,persona_id)
);
CREATE TABLE IF NOT EXISTS generation (
 id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversation(id),
 trigger_message_id INTEGER NOT NULL REFERENCES conversation_log(id),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, completed_at TEXT,
 lease_expires_at REAL, last_error TEXT,
 origin TEXT NOT NULL DEFAULT 'reply' CHECK(origin IN ('reply','proactive')),
 scheduled_at REAL NOT NULL DEFAULT 0, expires_at REAL,
 context_message_id INTEGER REFERENCES conversation_log(id), test_mode INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS conversation_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL,
 turn_index INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant','session_close')),
 content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 conversation_id INTEGER NOT NULL REFERENCES conversation(id), sender_kind TEXT NOT NULL, sender_id TEXT NOT NULL,
 client_message_id TEXT, reply_to_message_id INTEGER REFERENCES conversation_log(id),
 generation_id TEXT REFERENCES generation(id), output_index INTEGER,
 UNIQUE(conversation_id,client_message_id), UNIQUE(generation_id,output_index)
);

CREATE INDEX IF NOT EXISTS idx_log_user_persona
    ON conversation_log(user_id, persona_id, turn_index);

-- === 思考记录表 ===
-- 用途：保存推理模型的 <think>...</think> 思考过程，与 conversation_log 一一对应。
--
-- 为什么分开存：
--   conversation_log 的内容要送进 prompt（模型看的），思考过程是纯污染
--   （占历史 token 的 90%+）；但它对用户展示很有价值（能看到"TA 在想什么"）。
--   同一份数据两种用途 → 必须分开存。
--
-- 为什么不用第二张完整的 conversation_log：
--   这里只多一个 content 列，其余元数据都能从 conversation_log 派生，
--   不需要维护两套 UNIQUE 约束，也不会两路写入失步。
CREATE TABLE IF NOT EXISTS thinking_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES conversation_log(id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES user(id),
    persona_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    content     TEXT NOT NULL,          -- 原始 <think>...</think> 内容
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(message_id)
);

CREATE INDEX IF NOT EXISTS idx_thinking_user
    ON thinking_log(user_id, persona_id, turn_index);

-- === 用户设置表 ===
-- 用途：持久化用户的个性偏好（每个 persona 独立）。
-- 目前只有 show_thinking：控制界面是否展示模型的思考过程。
-- 数据两边都存着（干净正文 + 思考记录），这里只决定"给不给你看"。
CREATE TABLE IF NOT EXISTS user_settings (
    user_id        TEXT NOT NULL REFERENCES user(id),
    persona_id     TEXT NOT NULL,
    show_thinking  INTEGER NOT NULL DEFAULT 1,   -- 1 = 界面展示思考过程
    proactive_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, persona_id)
);

-- === FTS5 全文检索虚拟表 ===
-- 用途：对 note.content 做关键词搜索
CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5(
    note_id UNINDEXED,
    content,
    user_id UNINDEXED,
    persona_id UNINDEXED,
    is_archived UNINDEXED,
    tokenize='trigram'
);

-- 同步触发器：note → note_fts
CREATE TRIGGER IF NOT EXISTS note_fts_insert AFTER INSERT ON note BEGIN
    INSERT INTO note_fts(note_id, content, user_id, persona_id, is_archived)
    VALUES (new.id, new.content, new.user_id, new.persona_id, new.is_archived);
END;

CREATE TRIGGER IF NOT EXISTS note_fts_update AFTER UPDATE ON note BEGIN
    UPDATE note_fts
    SET content = new.content,
        is_archived = new.is_archived
    WHERE note_id = new.id;
END;

CREATE TRIGGER IF NOT EXISTS note_fts_delete AFTER DELETE ON note BEGIN
    DELETE FROM note_fts WHERE note_id = old.id;
END;
CREATE INDEX IF NOT EXISTS idx_message_conversation ON conversation_log(conversation_id,id);
CREATE INDEX IF NOT EXISTS idx_generation_pending ON generation(status,lease_expires_at);
CREATE TABLE IF NOT EXISTS memory_job (
 id TEXT PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversation(id),
 after_message_id INTEGER NOT NULL, through_message_id INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
 attempt_count INTEGER NOT NULL DEFAULT 0, retry_at REAL, lease_token TEXT, lease_expires_at REAL, last_error TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, completed_at TEXT,
 CHECK(through_message_id>after_message_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_one_pending ON memory_job(conversation_id) WHERE status!='succeeded';
CREATE TABLE IF NOT EXISTS note_evidence (
 note_id TEXT NOT NULL REFERENCES note(id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES conversation_log(id), quote TEXT NOT NULL CHECK(length(trim(quote))>0),
 PRIMARY KEY(note_id,message_id,quote)
);
CREATE TABLE IF NOT EXISTS persona_definition (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES user(id), persona_id TEXT NOT NULL, content TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 is_archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_persona_definition ON persona_definition(user_id,persona_id,is_archived);
CREATE UNIQUE INDEX IF NOT EXISTS uq_persona_definition_active
 ON persona_definition(user_id,persona_id) WHERE is_archived=0;
CREATE TABLE IF NOT EXISTS note_revision (
 note_id TEXT NOT NULL REFERENCES note(id) ON DELETE CASCADE, revision INTEGER NOT NULL,
 content TEXT NOT NULL, reality TEXT NOT NULL, event_time TEXT, time_basis TEXT NOT NULL,
 updated_at TEXT NOT NULL, job_id TEXT REFERENCES memory_job(id), PRIMARY KEY(note_id,revision)
);
