"""Account-wide call statistics and transactional message/reply admission limits."""
from datetime import date, datetime, timedelta, timezone
import os
import time
import json

from companion_ever.config import AccountLimitsConfig
from companion_ever.characters.users import UserRepo
from companion_ever.infrastructure.tracing import LAYERS, summarize_calls
from companion_ever.infrastructure.database.connection import transaction


class LimitExceeded(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AccountUsage:
    def __init__(self, conn, limits=None, *, tz_offset_hours=8, call_logs=None):
        self.conn = conn
        self.defaults = limits or AccountLimitsConfig()
        self.zone = timezone(timedelta(hours=tz_offset_hours))
        self.call_logs = call_logs

    def day_window(self, day=None):
        day = day or datetime.now(self.zone).date().isoformat()
        parsed = date.fromisoformat(day)
        if parsed.isoformat() != day:
            raise ValueError('日期必须使用 YYYY-MM-DD')
        start = datetime.combine(parsed, datetime.min.time(), tzinfo=self.zone)
        end = start + timedelta(days=1)
        return day, start.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), end.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), end.isoformat()

    def limits(self, user):
        row = self.conn.execute('SELECT daily_message_limit,concurrent_reply_limit FROM user WHERE id=?', (user,)).fetchone()
        if row is None:
            raise PermissionError('用户不存在')
        return {
            'daily_messages': self.defaults.daily_messages if row[0] is None else row[0],
            'concurrent_replies': self.defaults.concurrent_replies if row[1] is None else row[1],
        }

    def message_count(self, user, start, end):
        messages=self.conn.execute("SELECT count(*) FROM conversation_log WHERE user_id=? AND role='user' AND created_at>=? AND created_at<?", (user, start, end)).fetchone()[0]
        continuations=self.conn.execute("SELECT count(*) FROM (SELECT min(g.created_at) AS accepted_at FROM generation g "
            "JOIN conversation c ON c.id=g.conversation_id WHERE c.user_id=? AND g.trigger_kind='manual_continue' AND g.request_key IS NOT NULL "
            "GROUP BY g.conversation_id,g.request_key HAVING accepted_at>=? AND accepted_at<?)",(user,start,end)).fetchone()[0]
        deleted=self.conn.execute('SELECT coalesce(sum(count),0) FROM deleted_message_usage WHERE user_id=? AND accepted_at>=? AND accepted_at<?',(user,start,end)).fetchone()[0]
        return messages+continuations+deleted

    def active_replies(self, user, *, excluding_persona=None, excluding_conversation=None, running_only=False):
        sql = ("SELECT count(*) FROM generation g JOIN conversation c ON c.id=g.conversation_id "
               "WHERE c.user_id=? AND ((g.status='running' AND g.lease_expires_at>?)")
        args = [user, time.time()]
        if not running_only:
            sql += " OR (g.status='queued' AND g.scheduled_at<=?)"
            args.append(time.time())
        sql += ')'
        if excluding_persona is not None:
            sql += ' AND (c.persona_id IS NULL OR c.persona_id!=?)'
            args.append(excluding_persona)
            sql += " AND NOT (g.origin='proactive' AND g.actor_persona_id IS NOT NULL AND g.actor_persona_id=?)"
            args.append(excluding_persona)
        if excluding_conversation is not None:
            sql += ' AND c.id!=?'
            args.append(excluding_conversation)
            sql += " AND NOT (g.origin='proactive' AND g.actor_persona_id IS NOT NULL AND g.actor_persona_id IN (SELECT persona_id FROM conversation_member WHERE conversation_id=? AND left_after_message_id IS NULL))"
            args.append(excluding_conversation)
        return self.conn.execute(sql, args).fetchone()[0]

    def require_capacity(self, user, *, replacing_persona=None, replacing_conversation=None, new_message=False):
        """Caller holds the same SQLite write transaction that creates the receipt."""
        limits = self.limits(user)
        if new_message and limits['daily_messages']:
            _, start, end, _ = self.day_window()
            if self.message_count(user, start, end) >= limits['daily_messages']:
                raise LimitExceeded('daily_messages', '今天的消息额度已用完，将于明天恢复')
        cap = limits['concurrent_replies']
        if cap and self.active_replies(user, excluding_persona=replacing_persona,excluding_conversation=replacing_conversation) >= cap:
            raise LimitExceeded('concurrent_replies', '其他会话正在回复，请等一条回复完成后再发送')

    def can_start(self, user):
        cap = self.limits(user)['concurrent_replies']
        return not cap or self.active_replies(user, running_only=True) < cap

    def set_limits(self, name, *, daily=None, concurrent=None, reset=False):
        if any(type(value) is not int or value < 0 for value in (daily, concurrent) if value is not None):
            raise ValueError('额度需为非负整数，0 表示不限')
        if reset and (daily is not None or concurrent is not None):
            raise ValueError('--reset 不能和指定额度同时使用')
        with transaction(self.conn):
            row = self.conn.execute('SELECT id FROM user WHERE login_name=?', (name.strip().lower(),)).fetchone()
            if row is None:
                raise ValueError('账号不存在')
            if reset:
                self.conn.execute('UPDATE user SET daily_message_limit=NULL,concurrent_reply_limit=NULL WHERE id=?', (row['id'],))
            else:
                for field, value in (('daily_message_limit', daily), ('concurrent_reply_limit', concurrent)):
                    if value is not None:
                        self.conn.execute(f'UPDATE user SET {field}=? WHERE id=?', (value, row['id']))
        return row['id']

    def summary(self, user, day=None):
        day, start, end, resets_at = self.day_window(day)
        limits = self.limits(user)
        used = self.message_count(user, start, end)
        read_error = False
        try:
            records = self.call_logs.read(day, user_id=user) if self.call_logs is not None else []
        except OSError:
            records = []
            read_error = True
        now = time.time()
        counts = self.conn.execute("SELECT coalesce(sum(CASE WHEN g.status='running' AND g.lease_expires_at>? THEN 1 ELSE 0 END),0), "
            "coalesce(sum(CASE WHEN g.status='queued' AND g.scheduled_at<=? THEN 1 ELSE 0 END),0) "
            "FROM generation g JOIN conversation c ON c.id=g.conversation_id WHERE c.user_id=? AND g.status IN ('queued','running')",
            (now, now, user)).fetchone()
        return {
            'date': day, 'resets_at': resets_at,
            'overview': self.overview(user),
            'messages': {'used': used, 'limit': limits['daily_messages'],
                         'remaining': max(0, limits['daily_messages'] - used) if limits['daily_messages'] else None},
            'replies': {'active': counts[0], 'waiting': counts[1], 'limit': limits['concurrent_replies']},
            'calls': summarize_calls(records),
            'layers': {layer: summarize_calls([row for row in records if row['layer'] == layer]) for layer in LAYERS},
            'recording_enabled': os.environ.get('COMPANION_CALL_LOG', '1').lower() not in ('0', 'false', 'off', 'no'),
            'write_error': bool(self.call_logs and self.call_logs.write_error),
            'read_error': read_error,
        }

    def overview(self, user):
        """Lifetime totals for active personas; group messages count once, not per member."""
        personas = UserRepo(self.conn).list_personas(user)
        row = self.conn.execute(
            'WITH active(persona_id) AS (SELECT value FROM json_each(?)), '
            'scopes AS (SELECT id,kind FROM conversation WHERE user_id=? '
            "AND (kind='group' OR persona_id IN active)) SELECT "
            "(SELECT count(*) FROM scopes WHERE kind='group') AS group_count, "
            "(SELECT count(*) FROM conversation_log WHERE user_id=? AND role IN ('user','assistant') "
            'AND conversation_id IN (SELECT id FROM scopes)) AS message_count, '
            ' (SELECT count(*) FROM note WHERE user_id=? AND persona_id IN active AND is_archived=0) AS note_count, '
            ' (SELECT count(*) FROM memory_job WHERE conversation_id IN (SELECT id FROM scopes) '
            "AND persona_id IN active AND status='succeeded') AS extraction_count",
            (json.dumps(personas), user, user, user)).fetchone()
        return {'persona_count': len(personas), **dict(row)}

    def for_login(self, name, day=None):
        row = self.conn.execute('SELECT id FROM user WHERE login_name=?', (name.strip().lower(),)).fetchone()
        if row is None:
            raise ValueError('账号不存在')
        return self.summary(row['id'], day)
