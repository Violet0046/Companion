CREATE TABLE IF NOT EXISTS push_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    private_key TEXT NOT NULL,
    public_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscription (
    endpoint TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    revision TEXT NOT NULL,
    updated_at REAL NOT NULL,
    last_test_at REAL
);
CREATE INDEX IF NOT EXISTS idx_push_subscription_user ON push_subscription(user_id);
CREATE TABLE IF NOT EXISTS push_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id TEXT NOT NULL REFERENCES generation(id),
    message_id INTEGER NOT NULL REFERENCES conversation_log(id),
    user_id TEXT NOT NULL, persona_id TEXT NOT NULL, endpoint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','accepted','skipped','failed')),
    attempt INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL, expires_at REAL NOT NULL,
    lease_token TEXT, lease_until REAL, last_error TEXT,
    UNIQUE(generation_id,endpoint)
);
CREATE INDEX IF NOT EXISTS idx_push_delivery_ready ON push_delivery(status,retry_at);
