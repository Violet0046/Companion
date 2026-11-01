"""Identity only: existing user IDs continue to own all application data."""
import hashlib
import re
import secrets
import sqlite3
import threading
from time import time
from collections import OrderedDict

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from companion_ever.infrastructure.database.connection import transaction

COOKIE = 'companion_session'
IDLE_SECONDS = 30 * 86400
MAX_SECONDS = 90 * 86400
PASSWORDS = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


class AuthError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login_name(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9_.-]{3,32}', value):
        raise ValueError('账号使用 3–32 位字母、数字、点、下划线或短横线')
    return value


def hash_password(password):
    if not 15 <= len(password) <= 128:
        raise ValueError('密码需要 15–128 个字符，可以使用长句子')
    return PASSWORDS.hash(password)


class AccountService:
    def __init__(self, conn):
        self.conn = conn
        self._dummy = PASSWORDS.hash(secrets.token_urlsafe(32))
        self._attempts = OrderedDict()
        self._lock = threading.Lock()

    def provision(self, name, password, *, user_id=None, temporary=True):
        """Only the local CLI may bind an existing user; never overwrite credentials."""
        name = login_name(name)
        hashed = hash_password(password)
        with transaction(self.conn):
            if user_id is None:
                user_id = secrets.token_hex(16)
                self.conn.execute('INSERT INTO user(id) VALUES (?)', (user_id,))
            row = self.conn.execute('SELECT login_name FROM user WHERE id=?', (user_id,)).fetchone()
            if row is None:
                raise ValueError('要绑定的原用户不存在；请核对 ID')
            if row['login_name'] is not None:
                raise ValueError('该用户已经绑定账号，请使用 reset-password')
            try:
                self.conn.execute('UPDATE user SET login_name=?,password_hash=?,must_change_password=? WHERE id=?',
                                  (name, hashed, int(temporary), user_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError('账号名已存在') from exc
        return user_id

    def _throttle(self, key):
        now = time()
        with self._lock:
            recent = [t for t in self._attempts.pop(key, []) if now - t < 900]
            if len(recent) >= 20:
                self._attempts[key] = recent
                raise AuthError(429, '尝试过于频繁，请稍后再试')
            self._attempts[key] = recent + [now]
            while len(self._attempts) > 4096:
                self._attempts.popitem(last=False)

    def login(self, name, password, ip):
        self._throttle(ip)
        row = self.conn.execute('SELECT * FROM user WHERE login_name=?', (name.strip().lower(),)).fetchone()
        try:
            valid = PASSWORDS.verify(row['password_hash'] if row and row['password_hash'] else self._dummy, password)
        except (VerificationError, InvalidHashError):
            valid = False
        with transaction(self.conn):
            # Read again under the write lock: password changes and disable race safely.
            current = self.conn.execute('SELECT * FROM user WHERE id=?', (row['id'],)).fetchone() if row else None
            now = time()
            if not valid or not current or current['disabled'] or current['password_hash'] != row['password_hash'] or current['auth_locked_until'] > now:
                if current and current['auth_locked_until'] <= now:
                    failures = current['auth_failures'] + 1
                    self.conn.execute('UPDATE user SET auth_failures=?,auth_locked_until=? WHERE id=?',
                                      (failures, now + 900 if failures >= 5 else 0, current['id']))
                failure = True
            else:
                failure = False
                self.conn.execute('UPDATE user SET auth_failures=0,auth_locked_until=0 WHERE id=?', (current['id'],))
                self.conn.execute('DELETE FROM auth_session WHERE expires_at<=? OR absolute_expires_at<=?', (now, now))
                token = secrets.token_urlsafe(32)
                self.conn.execute('INSERT INTO auth_session VALUES (?,?,?,?,?,?,?)',
                                  (digest(token), current['id'], secrets.token_urlsafe(32), now, now, now + IDLE_SECONDS, now + MAX_SECONDS))
        if failure:
            raise AuthError(401, '账号或密码不正确，或账号暂不可用')
        return token

    def authenticate(self, token):
        if not token or len(token) > 200:
            raise AuthError(401, '请先登录')
        row = self.conn.execute('SELECT s.*,u.login_name,u.disabled,u.must_change_password FROM auth_session s '
                                'JOIN user u ON u.id=s.user_id WHERE token_hash=?', (digest(token),)).fetchone()
        now = time()
        if not row or row['disabled'] or min(row['expires_at'], row['absolute_expires_at']) <= now:
            raise AuthError(401, '登录已失效，请重新登录')
        if now - row['last_seen_at'] >= 300:
            self.conn.execute('UPDATE auth_session SET last_seen_at=?,expires_at=min(absolute_expires_at,?) WHERE token_hash=?',
                              (now, now + IDLE_SECONDS, row['token_hash']))
        return dict(row)

    def logout(self, account, *, all_devices=False, endpoint=''):
        with transaction(self.conn):
            self.conn.execute('DELETE FROM auth_session WHERE ' + ('user_id=?' if all_devices else 'token_hash=?'),
                              (account['user_id'] if all_devices else account['token_hash'],))
            if all_devices:
                self.conn.execute('DELETE FROM push_subscription WHERE user_id=?', (account['user_id'],))
            elif endpoint:
                self.conn.execute('DELETE FROM push_subscription WHERE user_id=? AND endpoint=?', (account['user_id'], endpoint))

    def change_password(self, account, old_password, new_password):
        hashed = hash_password(new_password)
        if old_password == new_password:
            raise ValueError('新密码需要与原密码不同')
        with transaction(self.conn):
            row = self.conn.execute('SELECT password_hash FROM user WHERE id=?', (account['user_id'],)).fetchone()
            try:
                PASSWORDS.verify(row['password_hash'], old_password)
            except (VerificationError, InvalidHashError) as exc:
                raise AuthError(400, '当前密码不正确') from exc
            self.conn.execute('UPDATE user SET password_hash=?,must_change_password=0,auth_failures=0,auth_locked_until=0 WHERE id=?',
                              (hashed, account['user_id']))
            self.logout(account, all_devices=True)

    def reset_password(self, name, password):
        hashed = hash_password(password)
        with transaction(self.conn):
            row = self.conn.execute('SELECT id FROM user WHERE login_name=?', (login_name(name),)).fetchone()
            if row is None:
                raise ValueError('账号不存在')
            self.conn.execute('UPDATE user SET password_hash=?,must_change_password=1,auth_failures=0,auth_locked_until=0 WHERE id=?', (hashed, row['id']))
            self.conn.execute('DELETE FROM auth_session WHERE user_id=?', (row['id'],))
            self.conn.execute('DELETE FROM push_subscription WHERE user_id=?', (row['id'],))

    def set_disabled(self, name, disabled):
        from companion_ever.conversations.generation_repository import GenerationRepo
        from companion_ever.memory.extraction.jobs import MemoryJobRepo
        from companion_ever.memory.summaries.repository import SummaryRepo
        with transaction(self.conn):
            row = self.conn.execute('SELECT id,persona_ids FROM user WHERE login_name=?', (login_name(name),)).fetchone()
            if row is None:
                raise ValueError('账号不存在')
            self.conn.execute('UPDATE user SET disabled=? WHERE id=?', (int(disabled), row['id']))
            if disabled:
                self.conn.execute('DELETE FROM auth_session WHERE user_id=?', (row['id'],))
                self.conn.execute('DELETE FROM push_subscription WHERE user_id=?', (row['id'],))
            for persona in filter(None, (value.strip() for value in row['persona_ids'].split(','))):
                if disabled:
                    GenerationRepo(self.conn).revoke_persona(row['id'], persona)
                for repo in (MemoryJobRepo(self.conn), SummaryRepo(self.conn)):
                    action = repo.revoke_persona if disabled else repo.restore_persona
                    action(row['id'], persona)

    def revoke_sessions(self, name):
        with transaction(self.conn):
            row = self.conn.execute('SELECT id FROM user WHERE login_name=?', (login_name(name),)).fetchone()
            if row is None:
                raise ValueError('账号不存在')
            self.conn.execute('DELETE FROM auth_session WHERE user_id=?', (row['id'],))
            self.conn.execute('DELETE FROM push_subscription WHERE user_id=?', (row['id'],))

    def list_accounts(self):
        return [dict(row) for row in self.conn.execute('SELECT id,login_name,disabled,must_change_password FROM user ORDER BY id')]
