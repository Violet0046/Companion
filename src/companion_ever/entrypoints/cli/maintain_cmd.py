"""Database backup without starting the application or migrating the source."""
from datetime import datetime
from pathlib import Path
import sqlite3


def cmd_backup(args) -> int:
    path = Path(args.db).resolve()
    if not path.is_file():
        print('[FAIL] 数据库不存在')
        return 1
    output = path.with_name(path.name + '.bak-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        target = sqlite3.connect(output)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    print(f'[OK] 备份完成: {output} ({output.stat().st_size:,} 字节)')
    return 0
