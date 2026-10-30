"""Verify an existing database on an isolated SQLite backup; source is opened read-only."""
import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from companion_ever.storage.db import init_db
from companion_ever.config import AppConfig


def verify(path):
    path=path.resolve()
    if not path.is_file():
        raise ValueError('数据库不存在；验证不会初始化原数据库')
    source=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    source.row_factory=sqlite3.Row
    try:
        with tempfile.TemporaryDirectory(prefix='verify-migration-') as tmp:
            target=Path(tmp)/'copy.db'
            backup=sqlite3.connect(target)
            source.backup(backup)
            backup.close()
            before=sqlite3.connect(target)
            before.row_factory=sqlite3.Row
            version=before.execute('PRAGMA user_version').fetchone()[0]
            tables=[r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'note_fts%'")]
            snapshots={t:[dict(r) for r in before.execute('SELECT * FROM '+t)] for t in tables}
            before.close()
            conn=init_db(target)
            try:
                for table,rows in snapshots.items():
                    if table=='memory_block':
                        rows=[r for r in rows if r['block_type']!='persona_runtime']
                    if table=='note':
                        for old in rows:
                            definition=old.get('type')=='persona_basics'
                            new_table='persona_definition' if definition else 'note'
                            new=conn.execute('SELECT * FROM '+new_table+' WHERE id=?',(old['id'],)).fetchone()
                            assert new is not None, 'migration lost a note/definition ID'
                            columns=('id','user_id','persona_id','content','created_at','updated_at','is_archived') if definition else [k for k in old if k!='type']
                            assert all(old[k]==new[k] for k in columns), 'migration changed historical note fields'
                        continue
                    if version<5 and table in ('memory_job','summary_job'):
                        rows=[{**r,'status':'queued','attempt_count':0,'lease_token':None,'lease_expires_at':None,'retry_at':None}
                              if r['status']=='failed' and r['last_error'] in ('执行租约到期，重试次数已用尽','摘要任务租约到期，重试次数已用尽') else r for r in rows]
                    after=[dict(r) for r in conn.execute('SELECT * FROM '+table)]
                    # Added columns may differ, but every existing column and every row must survive.
                    columns=list(rows[0]) if rows else []
                    freeze=lambda rs:sorted(json.dumps({k:r[k] for k in columns},sort_keys=True,ensure_ascii=False) for r in rs)
                    assert freeze(rows)==freeze(after), 'migration changed historical '+table
                assert not conn.execute('PRAGMA foreign_key_check').fetchall()
                assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                assert 'type' not in {r['name'] for r in conn.execute('PRAGMA table_info(note)')}
                assert conn.execute('SELECT count(*) FROM note_fts').fetchone()[0]==conn.execute('SELECT count(*) FROM note').fetchone()[0]
                result={'source_version':version,'copy_version':conn.execute('PRAGMA user_version').fetchone()[0],'preserved_tables':len(snapshots),
                        'notes':conn.execute('SELECT count(*) FROM note').fetchone()[0],
                        'definitions':conn.execute('SELECT count(*) FROM persona_definition').fetchone()[0],
                        'foreign_keys':'ok','integrity':'ok','source_mode':'read-only'}
            finally:
                conn.close()
            conn=init_db(target);conn.close()  # Idempotent reopen.
            return result
    finally:
        source.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.db or AppConfig.from_env().db_path),ensure_ascii=False))
