"""Versioned, transactional migration; retain message IDs and thinking references."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import re


def migrate(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > 13:
        raise ValueError('数据库版本比当前程序新，拒绝降级运行')
    if version < 1:
        _migrate_jobs(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 2:
        _migrate_kinds(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 3:
        _remove_persona_runtime(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 4:
        _add_summary_journal(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 5:
        _add_task_attempts(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 6:
        _constrain_persona_definitions(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 7:
        _remove_note_pinning(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 8:
        _add_notifications(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 9:
        _add_proactive(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 10:
        _add_accounts(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 11:
        _add_account_limits(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 12:
        _add_group_conversations(conn)
    if conn.execute('PRAGMA user_version').fetchone()[0] < 13:
        _add_group_automation(conn)


def _add_group_automation(conn):
    from companion_ever.infrastructure.database.connection import transaction
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    if path and conn.execute('SELECT 1 FROM user LIMIT 1').fetchone():
        backup_path=Path(path+'.bak-before-group-automation-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
        backup_path.touch(mode=0o600,exist_ok=False)
        backup=sqlite3.connect(backup_path)
        try:conn.backup(backup)
        finally:backup.close()
    with transaction(conn):
        for table,column,definition in (
            ('conversation','proactive_enabled','INTEGER NOT NULL DEFAULT 0'),
            ('conversation','visible_until','REAL NOT NULL DEFAULT 0'),
            ('memory_job','persona_id','TEXT'),
            ('memory_job','member_revision','INTEGER'),
            ('push_delivery','conversation_id','INTEGER REFERENCES conversation(id)'),
        ):
            if column not in {r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        conn.execute('UPDATE memory_job SET persona_id=(SELECT persona_id FROM conversation WHERE id=memory_job.conversation_id) WHERE persona_id IS NULL')
        conn.execute('DROP INDEX IF EXISTS idx_memory_one_pending')
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_scope_pending ON memory_job(conversation_id,persona_id) WHERE status!='succeeded'")
        conn.execute('UPDATE push_delivery SET conversation_id=(SELECT conversation_id FROM generation WHERE id=push_delivery.generation_id) WHERE conversation_id IS NULL')
        if conn.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('群自动任务迁移外键检查失败')
        conn.execute('PRAGMA user_version=13')


def _add_group_conversations(conn):
    """Preserve all IDs and references while making private persona binding optional."""
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path and conn.execute('SELECT 1 FROM user LIMIT 1').fetchone():
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup_path = Path(path + '.bak-before-groups-' + stamp)
        backup_path.touch(mode=0o600, exist_ok=False)
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    if conn.in_transaction:
        raise ValueError('会话迁移必须在启动时独立执行')
    from importlib import resources
    schema = resources.files('companion_ever.infrastructure.database').joinpath('group_schema.sql').read_text(encoding='utf-8')
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(conversation)')}
        if 'kind' not in columns:
            for table in ('conversation', 'conversation_log'):
                definition = re.search(r'CREATE TABLE IF NOT EXISTS ' + table + r' \(.*?\n\);', schema, re.S).group()
                conn.execute(definition.replace('EXISTS ' + table + ' (', 'EXISTS ' + table + '_new (', 1))
                fields = [r['name'] for r in conn.execute('PRAGMA table_info(' + table + ')')]
                names = ','.join(fields)
                old_sequence = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
                conn.execute(f'INSERT INTO {table}_new ({names}) SELECT {names} FROM {table}')
                conn.execute('DROP TABLE ' + table)
                conn.execute(f'ALTER TABLE {table}_new RENAME TO {table}')
                if old_sequence:
                    conn.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (old_sequence[0], table))
        fields = {r['name'] for r in conn.execute('PRAGMA table_info(generation)')}
        if 'actor_persona_id' not in fields:
            conn.execute('ALTER TABLE generation ADD COLUMN actor_persona_id TEXT')
            conn.execute("ALTER TABLE generation ADD COLUMN target_persona_ids TEXT NOT NULL DEFAULT '[]'")
            conn.execute("ALTER TABLE generation ADD COLUMN completed_persona_ids TEXT NOT NULL DEFAULT '[]'")
            conn.execute('ALTER TABLE generation ADD COLUMN coordination_done INTEGER NOT NULL DEFAULT 0')
            conn.execute('ALTER TABLE generation ADD COLUMN model_requests INTEGER NOT NULL DEFAULT 0')
        statement = ''
        for line in schema.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('会话迁移外键检查失败，已回滚')
        conn.execute('PRAGMA user_version=12')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')


def _add_account_limits(conn):
    from companion_ever.infrastructure.database.connection import transaction
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup_path = Path(path + '.bak-before-account-limits-' + stamp)
        backup_path.touch(mode=0o600, exist_ok=False)
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    with transaction(conn):
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(user)')}
        for field in ('daily_message_limit', 'concurrent_reply_limit'):
            if field not in columns:
                conn.execute(f'ALTER TABLE user ADD COLUMN {field} INTEGER CHECK({field}>=0)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_log_account_day ON conversation_log(user_id,role,created_at)')
        conn.execute('PRAGMA user_version=11')


def _add_accounts(conn):
    """Add credentials and sessions without claiming or rewriting existing users."""
    from companion_ever.infrastructure.database.connection import transaction
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup_path = Path(path + '.bak-before-accounts-' + stamp)
        backup_path.touch(mode=0o600, exist_ok=False)
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    with transaction(conn):
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(user)')}
        for field, definition in (
            ('login_name', 'TEXT'), ('password_hash', 'TEXT'),
            ('disabled', 'INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1))'),
            ('must_change_password', 'INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1))'),
            ('auth_failures', 'INTEGER NOT NULL DEFAULT 0'),
            ('auth_locked_until', 'REAL NOT NULL DEFAULT 0'),
        ):
            if field not in columns:
                conn.execute(f'ALTER TABLE user ADD COLUMN {field} {definition}')
        from importlib import resources
        schema = resources.files('companion_ever.infrastructure.database').joinpath('account_schema.sql').read_text(encoding='utf-8')
        statement = ''
        for line in schema.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        conn.execute('PRAGMA user_version=10')


def _add_proactive(conn):
    from importlib import resources
    from companion_ever.infrastructure.database.connection import transaction
    changes = {
        'conversation': [('last_read_message_id', 'INTEGER NOT NULL DEFAULT 0')],
        'generation': [('origin', "TEXT NOT NULL DEFAULT 'reply' CHECK(origin IN ('reply','proactive'))"),
                       ('scheduled_at', 'REAL NOT NULL DEFAULT 0'), ('expires_at', 'REAL'),
                       ('context_message_id', 'INTEGER REFERENCES conversation_log(id)'),
                       ('test_mode', 'INTEGER NOT NULL DEFAULT 0')],
        'user_settings': [('proactive_enabled', 'INTEGER NOT NULL DEFAULT 0')],
    }
    with transaction(conn):
        for table, fields in changes.items():
            columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name, definition in fields:
                if name not in columns:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        schema = resources.files('companion_ever.infrastructure.database').joinpath('notification_schema.sql').read_text(encoding='utf-8')
        statement = ''
        for line in schema.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        conn.execute('PRAGMA user_version=9')


def _add_notifications(conn):
    """Add notification-owned tables without rewriting conversation or memory data."""
    from importlib import resources
    from companion_ever.infrastructure.database.connection import transaction
    schema = resources.files('companion_ever.infrastructure.database').joinpath('notification_schema.sql').read_text(encoding='utf-8')
    with transaction(conn):
        statement = ''
        for line in schema.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        conn.execute('PRAGMA user_version=8')


def _remove_note_pinning(conn):
    """仅删除固定标记，保留笔记、关系、证据、摘要及历史流水。"""
    from companion_ever.infrastructure.database.connection import transaction
    with transaction(conn):
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(note)')}
        if 'pinned' in columns:
            conn.execute('ALTER TABLE note DROP COLUMN pinned')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('删除固定标记将破坏外键，schema v7 迁移已回滚')
        conn.execute('PRAGMA user_version=7')


def _constrain_persona_definitions(conn):
    """仅增加约束和编辑版本；重复设定需显式处理，不自动覆盖或归档。"""
    from companion_ever.infrastructure.database.connection import transaction
    with transaction(conn):
        duplicate = conn.execute('SELECT 1 FROM persona_definition WHERE is_archived=0 '
            'GROUP BY user_id,persona_id HAVING count(*)>1 LIMIT 1').fetchone()
        if duplicate:
            raise ValueError('角色有效设定存在重复，schema v6 迁移已回滚；请先确认应保留的设定并归档重复行')
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(persona_definition)')}
        if 'revision' not in columns:
            conn.execute('ALTER TABLE persona_definition ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_persona_definition_active '
                     'ON persona_definition(user_id,persona_id) WHERE is_archived=0')
        conn.execute('PRAGMA user_version=6')


def _add_task_attempts(conn):
    import json
    import time
    from importlib import resources
    from companion_ever.infrastructure.database.connection import transaction
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup=sqlite3.connect(path+'.bak-before-task-runtime-'+stamp)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    schema=resources.files('companion_ever.infrastructure.database').joinpath('task_schema.sql').read_text(encoding='utf-8')
    with transaction(conn):
        statement=''
        for line in schema.splitlines(True):
            statement+=line
            if sqlite3.complete_statement(statement):
                conn.execute(statement);statement=''
        for kind,table in [('memory','memory_job'),('summary','summary_job')]:
            sql=('SELECT j.*,c.user_id,c.persona_id FROM memory_job j JOIN conversation c ON c.id=j.conversation_id'
                 if kind=='memory' else 'SELECT * FROM summary_job')
            for row in conn.execute(sql).fetchall():
                job=dict(row)
                if not job['attempt_count'] or job['status']=='succeeded':
                    continue
                interrupted=job['status']=='failed' and job['last_error'] in (
                    '执行租约到期，重试次数已用尽','摘要任务租约到期，重试次数已用尽')
                token=job['lease_token'] or 'legacy-'+kind+'-'+job['id']
                # One imported snapshot, not fabricated records for unknown earlier attempts.
                span={k:job[k] for k in (('after_message_id','through_message_id') if kind=='memory' else ('after_change_id','through_change_id'))}
                status='interrupted' if interrupted else ('running' if job['status']=='running' else 'failed')
                conn.execute('INSERT INTO task_attempt(id,kind,job_id,user_id,persona_id,sequence,owner_id,status,stage,started_at,finished_at,failure_kind,error,range_json) '
                    'VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?)',
                    (token,kind,job['id'],job['user_id'],job['persona_id'],'before-v5',status,'legacy_snapshot',time.time(),
                     None if status=='running' else time.time(),'legacy_snapshot',job['last_error'],json.dumps(span)))
                conn.execute('INSERT INTO task_event(attempt_id,created_at,stage,detail_json) VALUES (?,?,?,?)',
                    (token,time.time(),'legacy_snapshot',json.dumps({'previous_status':job['status'],
                     'previous_attempt_count':job['attempt_count'],'lease_expires_at':job['lease_expires_at']})))
                if interrupted:
                    conn.execute(f"UPDATE {table} SET status='queued',attempt_count=0,lease_token=NULL,lease_expires_at=NULL,retry_at=NULL WHERE id=?",(job['id'],))
        conn.execute('PRAGMA user_version=5')


def _add_summary_journal(conn):
    from importlib import resources
    from companion_ever.infrastructure.database.connection import transaction
    path=conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup=sqlite3.connect(path+'.bak-before-summary-journal-'+stamp)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    schema=resources.files('companion_ever.infrastructure.database').joinpath('summary_schema.sql').read_text(encoding='utf-8')
    with transaction(conn):
        statement=''
        for line in schema.splitlines(True):
            statement+=line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement=''
        # A one-time baseline for legacy facts. Reopening does not reseed it.
        conn.execute("INSERT INTO fact_change(user_id,persona_id,note_id,operation,after_json) "
            "SELECT n.user_id,n.persona_id,n.id,'bootstrap',json_object('id',n.id,'kind',n.kind,'content',n.content,"
            "'reality',n.reality,'event_time',n.event_time,'time_basis',n.time_basis,'importance',n.importance,"
            "'is_archived',n.is_archived,'revision',n.revision,'source_quote',n.source_quote) FROM note n "
            "WHERE n.kind='fact' AND NOT EXISTS (SELECT 1 FROM fact_change e WHERE e.note_id=n.id)")
        conn.execute('PRAGMA user_version=4')


def _remove_persona_runtime(conn):
    """Remove the retired block; preserve both summaries byte-for-byte and retain a full backup."""
    from companion_ever.infrastructure.database.connection import _read_schema, transaction
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup = sqlite3.connect(path + '.bak-before-runtime-removal-' + stamp)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    statement = re.search(r'CREATE TABLE IF NOT EXISTS memory_block \(.*?\n\);',_read_schema(),re.S).group()
    with transaction(conn):
        conn.execute(statement.replace('EXISTS memory_block (','EXISTS memory_block_new (',1))
        conn.execute("INSERT INTO memory_block_new(id,user_id,persona_id,block_type,fields_json,updated_at) "
                     "SELECT id,user_id,persona_id,block_type,fields_json,updated_at FROM memory_block "
                     "WHERE block_type!='persona_runtime'")
        conn.execute('DROP TABLE memory_block')
        conn.execute('ALTER TABLE memory_block_new RENAME TO memory_block')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('删除运行时摘要将破坏外键，迁移已回滚')
        conn.execute('PRAGMA user_version=3')


def _migrate_kinds(conn):
    from companion_ever.infrastructure.database.connection import _read_schema
    schema = _read_schema(include_accounts=False)
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if path:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        backup = sqlite3.connect(path + '.bak-before-kind-' + stamp)
        try:
            conn.backup(backup)
        finally:
            backup.close()
    conn.execute('PRAGMA foreign_keys=OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        # Historical taxonomy exists only here, to migrate old files without losing IDs.
        episodes = ('event','milestone','inside_joke','callback','persona_memory','persona_arc')
        facts = ('profile_fact','preference','promise','sensitive','observation','persona_fact','persona_preference')
        known = episodes + facts + ('persona_basics',)
        if conn.execute('SELECT 1 FROM note WHERE type NOT IN (' + ','.join('?' for _ in known) + ') LIMIT 1',known).fetchone():
            raise ValueError('存在未知旧分类，拒绝静默归类')
        persona_sql = re.search(r'CREATE TABLE IF NOT EXISTS persona_definition \(.*?\n\);',schema,re.S).group()
        conn.execute(persona_sql)
        conn.execute("INSERT INTO persona_definition(id,user_id,persona_id,content,created_at,updated_at,is_archived) "
                     "SELECT id,user_id,persona_id,content,created_at,updated_at,is_archived FROM note WHERE type='persona_basics'")
        for trigger in ('note_fts_insert','note_fts_update','note_fts_delete'):
            conn.execute('DROP TRIGGER IF EXISTS ' + trigger)
        note_sql = re.search(r'CREATE TABLE IF NOT EXISTS note \(.*?\n\);',schema,re.S).group()
        conn.execute(note_sql.replace('EXISTS note (','EXISTS note_new (',1))
        marks = ','.join('?' for _ in episodes)
        conn.execute('INSERT INTO note_new(id,user_id,persona_id,kind,content,source_quote,importance,decay_score,'
                     'is_archived,created_at,updated_at,last_accessed_at) SELECT id,user_id,persona_id,'
                     f"CASE WHEN type IN ({marks}) THEN 'episode' ELSE 'fact' END,content,source_quote,importance,"
                     "decay_score,is_archived,created_at,updated_at,last_accessed_at FROM note WHERE type!='persona_basics'",episodes)
        conn.execute('DROP TABLE note')
        conn.execute('ALTER TABLE note_new RENAME TO note')
        link_sql = re.search(r'CREATE TABLE IF NOT EXISTS note_link \(.*?\n\);',schema,re.S).group()
        conn.execute(link_sql.replace('EXISTS note_link (','EXISTS note_link_new (',1))
        conn.execute('INSERT INTO note_link_new(id,from_id,to_id,link_type,strength,created_at) '
                     'SELECT id,from_id,to_id,link_type,strength,created_at FROM note_link')
        conn.execute('DROP TABLE note_link')
        conn.execute('ALTER TABLE note_link_new RENAME TO note_link')
        invalid = conn.execute("SELECT 1 FROM note_link l JOIN note a ON a.id=l.from_id JOIN note b ON b.id=l.to_id "
            "WHERE a.user_id!=b.user_id OR a.persona_id!=b.persona_id OR NOT "
            "((l.link_type='supports' AND a.kind='episode' AND b.kind='fact') OR "
            "(l.link_type='continues' AND a.kind='episode' AND b.kind='episode') OR "
            "(l.link_type='supersedes' AND a.kind='fact' AND b.kind='fact')) LIMIT 1").fetchone()
        if invalid:
            raise ValueError('旧关系的归属或方向无效，迁移已回滚')
        # Reapply indexes and triggers statement by statement, without executescript's implicit COMMIT.
        statement = ''
        for line in schema.splitlines(True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        conn.execute('DELETE FROM note_fts')
        conn.execute('INSERT INTO note_fts(note_id,content,user_id,persona_id,is_archived) '
                     'SELECT id,content,user_id,persona_id,is_archived FROM note')
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('分类迁移破坏来源或关系外键，已回滚，请检查备份')
        conn.execute('PRAGMA user_version=2')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')


def _migrate_jobs(conn: sqlite3.Connection) -> None:
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
