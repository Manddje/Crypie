"""Command line helpers for the account, run on the server: ``python -m app.auth <command>``.

Passwords are never taken as arguments (they would end up in the shell history): they are
asked for without echo, or read from standard input when that is not a terminal.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.auth.service import ENROL_TOKEN_SECONDS, validate_new_password


def _read_new_password() -> tuple[str, str]:
    """Twice without echo on a terminal; else the first line of standard input."""
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass("Nieuw wachtwoord: "), getpass.getpass("Nog een keer: ")
    line = sys.stdin.readline() if sys.stdin is not None else ""
    password = line.rstrip("\r\n")
    return password, password


def _print_enrol_token(token: str) -> None:
    print(
        f"eenmalige code om een authenticator te koppelen (geldig "
        f"{ENROL_TOKEN_SECONDS // 60} minuten): {token}\n"
        "log in met je wachtwoord en voer deze code in op de pagina 'Authenticator instellen'"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.auth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_create = sub.add_parser("create-account", help="create the account (instead of /setup)")
    p_create.add_argument("username")
    sub.add_parser("reset-password", help="set a new password; logs out every browser")
    sub.add_parser(
        "reset-mfa",
        help="forget the authenticator and recovery codes; prints a one-time code to re-enrol",
    )
    sub.add_parser("revoke-sessions", help="log out every browser")
    sub.add_parser("clear-throttle", help="forget all failed login attempts")
    p_del = sub.add_parser("delete-account", help="remove the account and log out every browser")
    p_del.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    from app.auth.service import AuthService, load_or_create_session_secret
    from app.config import get_settings
    from app.db import SessionLocal, init_db

    init_db()
    settings = get_settings()
    with SessionLocal() as session:
        # Same key as the server: enrol codes and recovery codes are hashed with it.
        auth = AuthService(
            settings, settings.session_secret or load_or_create_session_secret(session)
        )
        auth.ensure_account_id(session)
        user = auth.account(session)
        if args.command == "clear-throttle":
            print(f"{auth.throttle.clear(session)} geblokkeerde adressen/accounts vrijgegeven")
            return 0
        if args.command == "create-account":
            if user is not None:
                print(f"er is al een account ({user.username!r})", file=sys.stderr)
                return 1
            username = args.username.strip()
            if not 2 <= len(username) <= 64:
                print("kies een gebruikersnaam van 2 tot 64 tekens", file=sys.stderr)
                return 2
            password, repeat = _read_new_password()
            if error := validate_new_password(password, repeat):
                print(error, file=sys.stderr)
                return 2
            user = auth.create_account(session, username, password)
            print(f"account {user.username!r} aangemaakt")
            _print_enrol_token(auth.issue_enrol_token(session))
            return 0
        if user is None:
            print(
                "er is nog geen account; maak het aan met: python -m app.auth create-account "
                "<gebruikersnaam>",
                file=sys.stderr,
            )
            return 1
        if args.command == "reset-password":
            password, repeat = _read_new_password()
            if error := validate_new_password(password, repeat):
                print(error, file=sys.stderr)
                return 2
            auth.change_password(session, password)
            print(f"wachtwoord van {user.username!r} gewijzigd; alle browsers zijn uitgelogd")
        elif args.command == "reset-mfa":
            auth.reset_totp(session)
            print(f"authenticator en herstelcodes van {user.username!r} gewist")
            _print_enrol_token(auth.issue_enrol_token(session))
        elif args.command == "revoke-sessions":
            auth.revoke_sessions(session)
            print(f"alle browsers van {user.username!r} zijn uitgelogd")
        elif args.command == "delete-account":
            if not args.yes:
                if not (sys.stdin is not None and sys.stdin.isatty()):
                    print("bevestig met --yes", file=sys.stderr)
                    return 2
                answer = input(f"typ '{user.username}' om het account te verwijderen: ")
                if answer.strip() != user.username:
                    print("niet verwijderd", file=sys.stderr)
                    return 2
            # Cookies carry the account id, which dies with the row: every browser is out.
            auth.revoke_sessions(session)
            session.delete(user)
            session.commit()
            print(
                f"account {user.username!r} verwijderd en alle browsers uitgelogd; maak een "
                "nieuw account met: python -m app.auth create-account <gebruikersnaam>, of open "
                "/setup en gebruik de setup-link die de server dan in de logs zet"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
