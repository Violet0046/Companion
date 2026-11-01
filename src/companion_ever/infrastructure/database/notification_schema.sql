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
