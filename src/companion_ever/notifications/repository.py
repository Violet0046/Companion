"""Persistence owned by notifications; network requests never run in transactions."""
import time
import uuid

from companion_ever.infrastructure.database.connection import transaction


class NotificationRepo:
    def __init__(self, conn):
        self.conn = conn

    def identity(self, factory):
        with transaction(self.conn):
            row = self.conn.execute('SELECT private_key,public_key FROM push_identity WHERE id=1').fetchone()
            if row is None:
                private, public = factory()
                self.conn.execute('INSERT INTO push_identity(id,private_key,public_key) VALUES (1,?,?)', (private, public))
                return private, public
            return row['private_key'], row['public_key']

    def get(self, user, endpoint):
        row = self.conn.execute('SELECT * FROM push_subscription WHERE user_id=? AND endpoint=?', (user, endpoint)).fetchone()
        return dict(row) if row else None

    def save(self, user, subscription):
        with transaction(self.conn):
            owner = self.conn.execute('SELECT user_id FROM push_subscription WHERE endpoint=?', (subscription['endpoint'],)).fetchone()
            if owner is not None and owner['user_id'] != user:
                raise PermissionError('此设备订阅属于其他用户，请先关闭原用户的通知')
            self.conn.execute('INSERT INTO push_subscription(endpoint,user_id,p256dh,auth,revision,updated_at) '
                'VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET '
                'p256dh=excluded.p256dh,auth=excluded.auth,revision=excluded.revision,updated_at=excluded.updated_at',
                (subscription['endpoint'], user, subscription['keys']['p256dh'], subscription['keys']['auth'], uuid.uuid4().hex, time.time()))

    def delete(self, user, endpoint, revision=None):
        sql = 'DELETE FROM push_subscription WHERE user_id=? AND endpoint=?'
        args = [user, endpoint]
        if revision is not None:
            sql += ' AND revision=?'
            args.append(revision)
        self.conn.execute(sql, args)

    def claim_test(self, user, endpoint):
        with transaction(self.conn):
            row = self.get(user, endpoint)
            if row is None:
                raise LookupError('此设备尚未开启通知')
            now = time.time()
            if row['last_test_at'] is not None and now - row['last_test_at'] < 30:
                raise ValueError('请等待 30 秒后再发送测试通知')
            self.conn.execute('UPDATE push_subscription SET last_test_at=? WHERE user_id=? AND endpoint=?', (now, user, endpoint))
            return row
