"""命令行入口：init-db / create-admin / verify-sources / test-email / run-once。"""

from __future__ import annotations

import argparse
import sys
from getpass import getpass

from app.core.logging import get_logger, setup_logging

log = get_logger("cli")


def cmd_init_db(args: argparse.Namespace) -> int:
    from app.core.db import init_db
    from app.sources.registry import sync_sources_to_db

    init_db()
    sync_sources_to_db()
    print("数据库已初始化")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app.core.db import SessionLocal
    from app.core.security import hash_password, password_strength_ok
    from app.core.utils import utc_iso
    from app.models.user import User

    password = args.password or getpass("密码: ")
    ok, msg = password_strength_ok(password)
    if not ok:
        print(f"密码不符合要求：{msg}")
        return 1

    with SessionLocal() as session:
        exists = session.query(User).filter(User.email == args.email).first()
        if exists:
            exists.is_admin = True
            exists.password_hash = hash_password(password)
            session.commit()
            print(f"已提升为管理员：{args.email}")
            return 0
        user = User(
            email=args.email,
            password_hash=hash_password(password),
            display_name=args.name or args.email.split("@")[0],
            timezone=args.timezone,
            is_admin=True,
            is_active=True,
            email_verified=True,
            created_at=utc_iso(),
        )
        session.add(user)
        session.commit()
        print(f"已创建管理员：{args.email}")
    return 0


def cmd_verify_sources(args: argparse.Namespace) -> int:
    from scripts.verify_sources import main as verify_main

    return verify_main()


def cmd_test_email(args: argparse.Namespace) -> int:
    from app.pipeline.deliver import send_test_email

    ok, info = send_test_email(args.to)
    print(info)
    return 0 if ok else 1


def cmd_run_once(args: argparse.Namespace) -> int:
    from app.scheduler.jobs import run_once

    return run_once(args.task, args.arg)


def cmd_setup(args: argparse.Namespace) -> int:
    from app.core.db import get_engine, init_db
    from app.setup import console

    get_engine()
    init_db()
    if args.check:
        return _console_check()
    return console.run_wizard(start_step=args.step or None)


def _console_check() -> int:
    """终端版系统自检（不进入向导）。"""
    from app.setup.checks import run_all
    from app.setup.console import _short, rule

    results = run_all()
    print()
    rule("系统自检")
    print()
    bad = 0
    for name, r in results.items():
        if r["ok"]:
            print(f"  \033[32m✓\033[0m {name:<12} \033[2m{_short(r['detail'])}\033[0m")
        else:
            bad += 1
            print(f"  \033[33m!\033[0m {name:<12} \033[2m{_short(r['detail'])}\033[0m")
    rule()
    print(f"\n  {len(results) - bad} / {len(results)} 项正常\n")
    return 0


def main() -> int:
    setup_logging()
    from app.core.db import get_engine

    get_engine()
    parser = argparse.ArgumentParser(prog="paperpulse", description="PaperPulse CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="终端图形化配置向导（无浏览器环境用）")
    p_setup.add_argument("--step", type=int, default=0, help="从第 N 步开始（默认续配未完成步骤）")
    p_setup.add_argument("--check", action="store_true", help="只跑系统自检")
    p_setup.set_defaults(func=cmd_setup)

    p_init = sub.add_parser("init-db", help="初始化数据库与内置源")
    p_init.set_defaults(func=cmd_init_db)

    p_admin = sub.add_parser("create-admin", help="创建管理员")
    p_admin.add_argument("--email", required=True)
    p_admin.add_argument("--password", default="")
    p_admin.add_argument("--name", default="")
    p_admin.add_argument("--timezone", default="Asia/Shanghai")
    p_admin.set_defaults(func=cmd_create_admin)

    p_verify = sub.add_parser("verify-sources", help="校验数据源连通性")
    p_verify.set_defaults(func=cmd_verify_sources)

    p_email = sub.add_parser("test-email", help="发送测试邮件")
    p_email.add_argument("--to", required=True)
    p_email.set_defaults(func=cmd_test_email)

    p_once = sub.add_parser("run-once", help="手动跑一次任务")
    p_once.add_argument("task", choices=["fetch", "dispatch", "digest", "purge", "revise"])
    p_once.add_argument("arg", nargs="?", default="")
    p_once.set_defaults(func=cmd_run_once)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
