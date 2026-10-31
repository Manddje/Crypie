"""Command line helpers: ``python -m app.auth hash-password | secret | reset-mfa``."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from app.auth.passwords import hash_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_hash = sub.add_parser("hash-password", help="print an AUTH_PASSWORD_HASH for a password")
    p_hash.add_argument("password", nargs="?", help="omit to be prompted without echo")
    sub.add_parser("secret", help="print a random SESSION_SECRET")
    p_reset = sub.add_parser(
        "reset-mfa", help="forget the authenticator and recovery codes; re-enrol at next login"
    )
    p_reset.add_argument("--username", default=None, help="defaults to AUTH_USERNAME")
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        password = args.password or getpass.getpass("Wachtwoord: ")
        if not password:
            print("leeg wachtwoord", file=sys.stderr)
            return 2
        if not args.password and password != getpass.getpass("Nog een keer: "):
            print("de wachtwoorden verschillen", file=sys.stderr)
            return 2
        print(hash_password(password))
        return 0
    if args.command == "secret":
        print(secrets.token_urlsafe(32))
        return 0
    if args.command == "reset-mfa":
        from app.config import get_settings
        from app.db import AuthUser, SessionLocal, init_db

        init_db()
        username = args.username or get_settings().auth_username
        with SessionLocal() as session:
            row = session.get(AuthUser, username)
            if row is None:
                print(f"geen authenticator bekend voor {username!r}; niets te doen")
                return 0
            session.delete(row)
            session.commit()
        print(f"authenticator en herstelcodes van {username!r} gewist; koppel opnieuw bij de login")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
