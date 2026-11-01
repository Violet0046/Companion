CREATE UNIQUE INDEX IF NOT EXISTS uq_user_login ON user(login_name) WHERE login_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS auth_session (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    absolute_expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_session_user ON auth_session(user_id);
CREATE INDEX IF NOT EXISTS idx_log_account_day ON conversation_log(user_id,role,created_at);

