"""Command line helpers.

    python -m signage.cli set-password [--username admin]
"""
import argparse
import getpass
import os
import sys

from . import db
from .config import Config
from .web import set_credentials


def main(argv=None):
    parser = argparse.ArgumentParser(prog="signage")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password", help="set the admin username and password")
    sp.add_argument("--username", default=None)
    sp.add_argument("--password-stdin", action="store_true",
                    help="read the password from standard input")
    args = parser.parse_args(argv)

    config = Config()
    config.ensure_dirs()
    conn = db.connect(config.db_path)

    if args.cmd == "set-password":
        username = args.username or input("Admin username [admin]: ").strip() or "admin"
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = os.environ.get("SIGNAGE_ADMIN_PASSWORD") or getpass.getpass("New password: ")
            if "SIGNAGE_ADMIN_PASSWORD" not in os.environ and password != getpass.getpass("Repeat password: "):
                sys.exit("Passwords don't match.")
        try:
            set_credentials(conn, username, password)
        except ValueError as exc:
            sys.exit(f"Error: {exc}")
        print(f"Saved login for '{username}'.")


if __name__ == "__main__":
    main()
