"""Retiring runtime blocks preserves both useful summaries and the complete memory store."""
import json
import sqlite3
import tempfile
from pathlib import Path
from companion_ever.storage.db import _read_schema, init_db
from companion_ever.storage.migrations import migrate
from companion_ever.domain.enums import BlockType


def old_database(path):
    conn=sqlite3.connect(path)
    schema=_read_schema().replace("'user_profile', 'relationship_state'", "'persona_runtime', 'user_profile', 'relationship_state'")
    conn.executescript(schema)
    conn.execute("INSERT INTO user(id,persona_ids) VALUES ('alice','one')")
    conn.execute("INSERT INTO note(id,user_id,persona_id,kind,content) VALUES ('memory','alice','one','fact','retained fact')")
    conn.execute("INSERT INTO persona_definition(id,user_id,persona_id,content) VALUES ('persona','alice','one','{}')")
    for kind in ('persona_runtime','user_profile','relationship_state'):
        fields=json.dumps({'sentinel':kind,'list':['one','two']})
        conn.execute('INSERT INTO memory_block(id,user_id,persona_id,block_type,fields_json,updated_at) '
                     "VALUES (?,'alice','one',?,?,'2020-01-02 03:04:05')",(kind,kind,fields))
    conn.execute('PRAGMA user_version=2')
    conn.commit()
    conn.close()


def verify(path):
    old_database(path)
    old=sqlite3.connect(path)
    expected=old.execute("SELECT * FROM memory_block WHERE block_type!='persona_runtime' ORDER BY id").fetchall()
    old.close()
    conn=init_db(path)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==5
        assert [tuple(r) for r in conn.execute('SELECT * FROM memory_block ORDER BY id')]==expected
        assert conn.execute('SELECT id FROM note').fetchone()[0]=='memory'
        assert conn.execute('SELECT id FROM persona_definition').fetchone()[0]=='persona'
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        try:
            conn.execute("INSERT INTO memory_block(id,user_id,persona_id,block_type,fields_json) VALUES ('bad','alice','one','persona_runtime','{}')")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError('runtime block still accepted by SQL')
        try:
            BlockType('persona_runtime')
        except ValueError:
            pass
        else:
            raise AssertionError('runtime block still in application enum')
    finally:
        conn.close()
    conn=init_db(path);conn.close()
    backups=list(path.parent.glob(path.name+'.bak-before-runtime-removal-*'))
    assert len(backups)==1
    backup=sqlite3.connect(backups[0])
    assert backup.execute("SELECT count(*) FROM memory_block WHERE block_type='persona_runtime'").fetchone()[0]==1
    backup.close()


def rollback(path):
    old_database(path)
    conn=sqlite3.connect(path,isolation_level=None)
    class Failure:
        def execute(self,sql,*args):
            if sql.startswith('ALTER TABLE memory_block_new'):
                raise RuntimeError('injected migration failure')
            return conn.execute(sql,*args)
        def __getattr__(self,key):
            return getattr(conn,key)
    try:
        try:
            migrate(Failure())
        except RuntimeError:
            pass
        else:
            raise AssertionError('failure injection did not run')
        assert conn.execute('PRAGMA user_version').fetchone()[0]==2
        assert conn.execute('SELECT count(*) FROM memory_block').fetchone()[0]==3
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_block_new'").fetchone()
    finally:
        conn.close()
    conn=init_db(path);conn.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory() as tmp:
        verify(Path(tmp)/'migration.db')
        rollback(Path(tmp)/'rollback.db')
    print('[OK] runtime removal, summary preservation, backup, SQL constraint and migration rollback')
