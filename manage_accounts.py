#!/usr/bin/env python3
"""
manage_accounts.py

Add / list / login / remove twscrape accounts.

Works both locally (interactive) and in GitHub Actions (reads from env vars).

--- USAGE ---

Add an account interactively:
    python manage_accounts.py add --username alice --password s3cr3t --email alice@example.com

Add from environment variables (for GitHub Actions):
    TW_ACCOUNTS="alice:pass1:alice@mail.com,bob:pass2:bob@mail.com" python manage_accounts.py add-from-env

Login all pending accounts:
    python manage_accounts.py login

List all accounts:
    python manage_accounts.py list

Remove an account:
    python manage_accounts.py remove --username alice
"""

import asyncio
import argparse
import os
import sys

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TWSCRAPE_DB = os.environ.get("TWSCRAPE_DB", os.path.join(BASE_DIR, "twscrape_accounts.db"))


async def cmd_add(args):
    from twscrape import API
    tw = API(TWSCRAPE_DB)
    await tw.pool.add_account(
        username=args.username,
        password=args.password,
        email=args.email,
        email_password=args.email_password or "",
    )
    print(f"✅ Added @{args.username}")


async def cmd_add_from_env(_args):
    """
    Read TW_ACCOUNTS env var (format: user:pass:email,user2:pass2:email2)
    and add all accounts to the pool. Use this in GitHub Actions.
    """
    from twscrape import API
    raw = os.environ.get("TW_ACCOUNTS", "").strip()
    if not raw:
        print("❌ TW_ACCOUNTS env var is empty or not set.")
        print("   Format: user:pass:email,user2:pass2:email2")
        sys.exit(1)

    tw = API(TWSCRAPE_DB)
    added = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 3:
            print(f"⚠️  Skipping malformed entry: {entry!r}  (expected user:pass:email)")
            continue
        username, password, email = parts[0], parts[1], parts[2]
        email_password = parts[3] if len(parts) > 3 else ""
        await tw.pool.add_account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
        )
        print(f"✅ Added @{username}")
        added += 1

    print(f"\n✅ {added} account(s) added. Run 'python manage_accounts.py login' next.")


async def cmd_login(_args):
    from twscrape import API
    tw = API(TWSCRAPE_DB)
    print("🔑 Logging in all pending accounts...")
    print("   (If Twitter asks for a verification code, enter it when prompted)")
    await tw.pool.login_all()
    print("✅ Login complete")

    # Show status
    accounts = await tw.pool.get_all()
    active   = [a for a in accounts if getattr(a, "active", False)]
    print(f"   Active: {len(active)}/{len(accounts)}")
    if len(active) == 0:
        print("⚠️  No active accounts! Twitter may have challenged them.")
        print("   Try logging in interactively on a real browser with these accounts first,")
        print("   then re-run this script.")


async def cmd_list(_args):
    from twscrape import API
    tw = API(TWSCRAPE_DB)
    accounts = await tw.pool.get_all()
    if not accounts:
        print("No accounts found.")
        return
    print(f"{'Username':<20} {'Active':<8} {'Last used'}")
    print("-" * 55)
    for a in accounts:
        print(f"{a.username:<20} {str(getattr(a, 'active', '?')):<8} {getattr(a, 'lastUsed', 'never') or 'never'}")


async def cmd_remove(args):
    from twscrape import API
    tw = API(TWSCRAPE_DB)
    await tw.pool.delete_accounts([args.username])
    print(f"🗑️  Removed @{args.username}")


def main():
    p = argparse.ArgumentParser(description="Manage twscrape account pool")
    sub = p.add_subparsers(dest="cmd", required=True)

    # add
    a = sub.add_parser("add", help="Add an account manually")
    a.add_argument("--username",        required=True)
    a.add_argument("--password",        required=True)
    a.add_argument("--email",           required=True)
    a.add_argument("--email-password",  dest="email_password", default="")

    # add-from-env
    sub.add_parser("add-from-env",
                   help="Add accounts from TW_ACCOUNTS env var (for CI)")

    # login
    sub.add_parser("login", help="Login all pending accounts")

    # list
    sub.add_parser("list", help="List all accounts and their status")

    # remove
    r = sub.add_parser("remove", help="Remove an account")
    r.add_argument("--username", required=True)

    args = p.parse_args()
    handlers = {
        "add":          cmd_add,
        "add-from-env": cmd_add_from_env,
        "login":        cmd_login,
        "list":         cmd_list,
        "remove":       cmd_remove,
    }
    asyncio.run(handlers[args.cmd](args))


if __name__ == "__main__":
    main()
