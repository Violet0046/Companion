"""Local operator commands; credentials are never supplied as command arguments."""
import getpass
from pathlib import Path

from companion_ever.accounts.service import AccountService, MIN_PASSWORD
from companion_ever.config import AppConfig
from companion_ever.infrastructure.database.connection import init_db


def cmd_account(args):
    path = Path(args.db) if args.db else AppConfig.from_env().db_path
    if not path.is_file():
        print('[FAIL] 数据库不存在，请核对 --db；账号命令不会新建数据库')
        return 1
    conn = init_db(path)
    try:
        service = AccountService(conn)
        action = args.account_cmd
        if action == 'list':
            for row in service.list_accounts():
                print(f"{row['id']}\t{row['login_name'] or '未绑定'}\t{'停用' if row['disabled'] else '启用'}")
        elif action in ('create', 'reset-password'):
            password = getpass.getpass(f'密码（至少 {MIN_PASSWORD} 个字符，输入不回显）: ')
            if password != getpass.getpass('再次输入: '):
                raise ValueError('两次密码不一致')
            if action == 'create':
                user_id = service.provision(args.username, password, user_id=args.bind_user, temporary=not args.permanent)
                print(f'[OK] 账号已绑定用户 {user_id}' + ('；首次登录需要改密' if not args.permanent else ''))
            else:
                service.reset_password(args.username, password)
                print('[OK] 密码已重置，旧登录全部失效，首次登录需要改密')
        elif action == 'revoke-sessions':
            service.revoke_sessions(args.username)
            print('[OK] 已撤销全部登录与通知订阅')
        else:
            service.set_disabled(args.username, action == 'disable')
            print('[OK] 账号状态已更新')
        return 0
    except ValueError as exc:
        print(f'[FAIL] {exc}')
        return 1
    finally:
        conn.close()
