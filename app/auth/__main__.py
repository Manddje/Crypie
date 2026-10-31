"""Command line helpers for a locked-out user: ``python -m app.auth reset-password | reset-mfa``."""

from __future__ import annotations

import argparse
import getpass
import sys

from app.auth.service import validate_new_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_pw = sub.add_parser("reset-password", help="set a new password for the account")
    p_pw.add_argument("password", nargs="?", help="omit to be prompted without echo")
    sub.add_parser(
        "reset-mfa", help="forget the authenticator and recovery codes; re-enrol at next login"
    )
    sub.add_parser("delete-account", help="remove the account so /setup can be used again")
    args = parser.parse_args(argv)

    from app.auth.service import AuthService
    from app.config import get_settings
    from app.db import SessionLocal, init_db

    init_db()
    auth = AuthService(get_settings(), "cli")
    with SessionLocal() as session:
        user = auth.account(session)
        if user is None:
            print("er is nog geen account; maak het aan op /setup", file=sys.stderr)
            return 1
        if args.command == "reset-password":
            password = args.password or getpass.getpass("Nieuw wachtwoord: ")
            repeat = password if args.password else getpass.getpass("Nog een keer: ")
            if error := validate_new_password(password, repeat):
                print(error, file=sys.stderr)
                return 2
            auth.change_password(session, password)
            print(f"wachtwoord van {user.username!r} gewijzigd")
        elif args.command == "reset-mfa":
            auth.reset_totp(session)
            print(f"authenticator en herstelcodes van {user.username!r} gewist; koppel opnieuw")
        elif args.command == "delete-account":
            session.delete(user)
            session.commit()
            print(f"account {user.username!r} verwijderd; ga naar /setup voor een nieuw account")
    return 0


if __name__ == "__main__":
    sys.exit(main())
