#!/usr/bin/env python3
"""Reset a web account password (or create a super admin) from the shell.

    python3 tools/reset_admin.py                     # reset the first super admin
    python3 tools/reset_admin.py --user johir         # reset a specific account
    python3 tools/reset_admin.py --user Johir --create --role superadmin
    python3 tools/reset_admin.py --user Johir --password 'new-secret'

Passwords are read with ``getpass`` unless ``--password`` is given (useful for
automation, but then the password ends up in your shell history).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from olt_monitor import auth, db  # noqa: E402
from olt_monitor.config import db_path as resolve_db_path  # noqa: E402
from olt_monitor.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="username (default: first super admin)")
    parser.add_argument("--password", default=None, help="new password (prompted when omitted)")
    parser.add_argument("--create", action="store_true", help="create the user when it does not exist")
    parser.add_argument("--role", default="superadmin", choices=list(auth.ROLES))
    parser.add_argument("--display-name", default="")
    args = parser.parse_args()

    cfg = load_config()
    db.configure(cfg)
    db.init_db(resolve_db_path(cfg))

    users = auth.list_users(None)
    if not users:
        print("No users exist yet - open the web UI and use the /setup wizard.", file=sys.stderr)
        return 2

    username = args.user
    if not username:
        admins = [u for u in users if u["role"] == "superadmin"]
        if not admins:
            print("No super admin found. Pass --user <name> --create --role superadmin.", file=sys.stderr)
            return 2
        username = admins[0]["username"]

    user = auth.get_user_by_username(None, username)
    if not user:
        if not args.create:
            print(f"user {username!r} does not exist (use --create)", file=sys.stderr)
            return 2
        password = args.password or getpass.getpass("New password: ")
        confirm = password if args.password else getpass.getpass("Repeat password: ")
        try:
            created = auth.create_user(
                None, username, password, args.role, display_name=args.display_name
            )
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 2
        auth.audit(None, created["username"], "user.create-cli", f"role={created['role']}", "localhost")
        print(f"[ok] created {created['username']} ({created['role']})")
        return 0

    password = args.password or getpass.getpass(f"New password for {user['username']}: ")
    confirm = password if args.password else getpass.getpass("Repeat password: ")
    try:
        auth.validate_password(password, confirm)
        auth.set_password(None, int(user["id"]), password)
    except ValueError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    if not user.get("active") or user.get("role") not in auth.ROLES:
        auth.update_user(None, int(user["id"]), active=True)
    auth.audit(None, user["username"], "user.password-cli", "password reset from shell", "localhost")
    print(f"[ok] password updated for {user['username']} ({user['role']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
