-- ============================================================
-- Companion Wiki Schema
-- ============================================================

-- === 用户表 ===
-- 用途：每个用户一行，persona_ids 用 CSV 存储该用户的所有 persona
CREATE TABLE IF NOT EXISTS user (
    id           TEXT PRIMARY KEY,
    persona_ids  TEXT NOT NULL DEFAULT ''
);

-- === Wiki 笔记表（核心）===
-- 用途：所有抽取的事实，14 种类型（9 用户侧 + 5 人设侧）
CREATE TABLE IF NOT EXISTS note (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES user(id),
    persona_id       TEXT NOT NULL,

    type             TEXT NOT NULL,
    content          TEXT NOT NULL,

    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_accessed_at TIMESTAMP,

    importance       REAL NOT NULL DEFAULT 0.5,
    decay_score      REAL NOT NULL DEFAULT 1.0,
    is_archived      INTEGER NOT NULL DEFAULT 0,

    source_quote     TEXT,

    CHECK (type IN (
        -- 用户侧（9 种）：关于用户的观察/事实
        'profile_fact', 'event', 'preference', 'promise',
        'milestone', 'inside_joke', 'callback', 'sensitive',
        'observation',
        -- 人设侧（5 种）：关于助手人设的演化
        'persona_fact', 'persona_basics', 'persona_preference',
        'persona_memory', 'persona_arc'
    ))
);

-- 索引：用户×persona×类型 是最常用的过滤组合
CREATE INDEX IF NOT EXISTS idx_note_user_persona_type
    ON note(user_id, persona_id, type, is_archived);

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
    link_type   TEXT NOT NULL,
    strength    REAL NOT NULL DEFAULT 0.5,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_id, to_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_link_from ON note_link(from_id);
CREATE INDEX IF NOT EXISTS idx_link_to   ON note_link(to_id);

-- === Memory Block 表（3 个核心块）===
-- 用途：注入 system prompt
CREATE TABLE IF NOT EXISTS memory_block (
    id TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES user(id),
    persona_id      TEXT NOT NULL,
    block_type      TEXT NOT NULL,
    fields_json     TEXT NOT NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, persona_id, block_type),
    CHECK (block_type IN (
        'persona_runtime', 'user_profile', 'relationship_state'
    ))
);

-- === 原始对话存档 ===
-- 用途：每次对话追加一行
CREATE TABLE IF NOT EXISTS conversation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL REFERENCES user(id),
    persona_id   TEXT NOT NULL,
    turn_index   INTEGER NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, persona_id, turn_index, role),
    CHECK (role IN ('user', 'assistant', 'session_close'))
);

CREATE INDEX IF NOT EXISTS idx_log_user_persona
    ON conversation_log(user_id, persona_id, turn_index);

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