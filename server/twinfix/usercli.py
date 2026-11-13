"""`twinfix-user`：本地账号管理（开启鉴权前先建一个管理员）。

    twinfix-user create admin                 # 交互输入密码，默认 admin 角色
    twinfix-user create alice --role maintainer
    twinfix-user passwd alice
    twinfix-user list

密码也可用 `--password` 或环境变量 `TWINFIX_USER_PASSWORD`（便于容器内非交互执行）。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from twinfix.api.auth import hash_password
from twinfix.config import Settings
from twinfix.db import Store, make_engine, use_selector_loop_on_windows
from twinfix.domain.enums import UserRole
from twinfix.domain.models import User
from twinfix.events import MemoryPublisher


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twinfix-user", description="TwinFix 本地账号管理")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="创建账号")
    c.add_argument("name")
    c.add_argument("--password", help="不传则交互输入或用 TWINFIX_USER_PASSWORD")
    c.add_argument("--email")
    c.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.ADMIN.value)

    w = sub.add_parser("passwd", help="修改密码")
    w.add_argument("name")
    w.add_argument("--password", help="不传则交互输入或用 TWINFIX_USER_PASSWORD")

    sub.add_parser("list", help="列出账号")
    return p


def _password(args: argparse.Namespace) -> str:
    password = args.password or os.environ.get("TWINFIX_USER_PASSWORD") or getpass.getpass("密码: ")
    if len(password) < 8:
        raise SystemExit("密码至少 8 位")
    return password


async def _run(args: argparse.Namespace) -> int:
    store = Store(make_engine(Settings().database_url), MemoryPublisher())
    try:
        if args.command == "list":
            async with store.read() as s:
                users = (await s.scalars(select(User).order_by(User.name))).all()
            for u in users:
                print(f"{u.name}\t{u.role.value}\t{u.email or '-'}")
            return 0

        async with store.tx() as tx:
            user = await tx.session.scalar(select(User).where(User.name == args.name))
            if args.command == "create":
                if user is not None:
                    print(f"账号 {args.name} 已存在", file=sys.stderr)
                    return 1
                user = User(
                    name=args.name,
                    email=args.email,
                    role=UserRole(args.role),
                    password_hash=hash_password(_password(args)),
                )
                tx.session.add(user)
                print(f"已创建 {args.name}（{args.role}）")
            else:
                if user is None:
                    print(f"账号 {args.name} 不存在", file=sys.stderr)
                    return 1
                user.password_hash = hash_password(_password(args))
                print(f"已更新 {args.name} 的密码")
    except IntegrityError as exc:
        print(f"写入失败（用户名或邮箱重复）: {exc.orig}", file=sys.stderr)
        return 1
    finally:
        await store.close()
    return 0


def main() -> None:
    use_selector_loop_on_windows()
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))
