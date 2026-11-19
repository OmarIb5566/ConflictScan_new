"""
ConflictScan administration commands
====================================
    python manage.py init                        create / upgrade the database schema
    python manage.py status                      settings, database, schema revision, health
    python manage.py sql [--url URL] [--out F]   the migrations as plain SQL (for a DBA);
                                                 --url picks the dialect, e.g. mssql+pyodbc://
                                                 or postgresql:// (no connection is made)
    python manage.py create-admin USER [--password P] [--name "Full Name"]
    python manage.py export OUT.zip              all data, engine-neutral, with checksums
    python manage.py import IN.zip [--replace]   load an export into DATABASE_URL and verify it
    python manage.py verify IN.zip               compare the database with an export
    python manage.py backup OUT.zip              export + every uploaded/generated file
    python manage.py restore IN.zip [--replace]  restore a backup (database + files)
    python manage.py purge                       apply the retention settings now
    python manage.py check-files                 list run files missing from storage

Moving to the host company's database:
    1. python manage.py backup move.zip                         (on the workstation)
    2. set DATABASE_URL (and CONFLICTSCAN_FILES_ROOT) for the new server
    3. python manage.py restore move.zip                        (on the server)
    The restore stops and changes nothing if any table's checksum differs.

Windows compatible.
"""

import argparse
import getpass
import os
import sys


def main(argv=None):
    p = argparse.ArgumentParser(prog="manage.py", description="ConflictScan administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    s = sub.add_parser("sql")
    s.add_argument("--url")
    s.add_argument("--schema")
    s.add_argument("--out")
    s.add_argument("--from-rev", default=None)
    a = sub.add_parser("create-admin")
    a.add_argument("username")
    a.add_argument("--password")
    a.add_argument("--name")
    for name in ("export", "backup"):
        e = sub.add_parser(name)
        e.add_argument("out")
    for name in ("import", "restore"):
        i = sub.add_parser(name)
        i.add_argument("src")
        i.add_argument("--replace", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("src")
    sub.add_parser("purge")
    sub.add_parser("check-files")
    args = p.parse_args(argv)

    if args.cmd == "sql":
        # Offline: only the dialect matters, nothing is connected to.
        if args.url:
            os.environ["DATABASE_URL"] = args.url
        if args.schema:
            os.environ["CONFLICTSCAN_DB_SCHEMA"] = args.schema
        os.environ["CONFLICTSCAN_OFFLINE"] = "1"
        import io
        from contextlib import redirect_stdout
        from alembic import command
        import storage
        cfg = storage.alembic_config()
        buf = io.StringIO()
        with redirect_stdout(buf):
            command.upgrade(cfg, f"{args.from_rev}:head" if args.from_rev else "head", sql=True)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(buf.getvalue())
            print(f"Wrote {args.out}")
        else:
            sys.stdout.write(buf.getvalue())
        return 0

    import config
    import db
    import storage
    storage.init_db()

    if args.cmd == "init":
        print(f"Database ready: {db.describe()} - schema revision {storage.schema_revision()}")
    elif args.cmd == "status":
        from adapters import files as file_store, identity, model_servers, reference
        print(f"ConflictScan {config.VERSION}")
        print(f"Settings file : {config.settings_path()} ({'found' if os.path.isfile(config.settings_path()) else 'not found - defaults'})")
        print(f"Database      : {db.describe()} - revision {storage.schema_revision()}")
        print(f"Files         : {file_store.store().health()['detail']}")
        print(f"Sign-in       : {identity.health()['detail']}")
        print(f"Reference     : {reference.health()['detail']}")
        for s in model_servers.health():
            print(f"Model server  : {s['name']} {s['url']} - " + (f"{len(s['models'])} models" if s["ok"] else s.get("error", "")))
        print(f"Users         : {storage.count_users()} ({storage.count_admins()} admin)")
    elif args.cmd == "create-admin":
        from werkzeug.security import generate_password_hash
        password = args.password or getpass.getpass("Password: ")
        existing = storage.get_user_by_username(args.username)
        if existing:
            storage.update_user(existing["id"], role="admin", status="active",
                                password_hash=generate_password_hash(password))
            print(f"{args.username} is now an active admin with the new password.")
        else:
            storage.create_user(args.username, args.name or args.username, generate_password_hash(password), role="admin")
            print(f"Created admin {args.username}.")
        storage.audit("admin.cli_create_admin", None, "user", None, summary=f"manage.py create-admin {args.username}")
    elif args.cmd in ("export", "backup"):
        import datamove
        m = datamove.export_zip(args.out, include_files=args.cmd == "backup")
        print(f"Wrote {args.out}: {sum(t['rows'] for t in m['tables'].values())} rows"
              + (f", {m.get('file_count', 0)} files" if args.cmd == "backup" else ""))
    elif args.cmd in ("import", "restore"):
        import datamove
        datamove.import_zip(args.src, replace=args.replace, restore_files=args.cmd == "restore")
        storage.audit("admin.cli_" + args.cmd, None, summary=f"manage.py {args.cmd} {os.path.basename(args.src)}")
    elif args.cmd == "verify":
        import datamove
        problems = datamove.verify_against(args.src)
        print("Identical." if not problems else "Differences:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    elif args.cmd == "purge":
        import maintenance
        print(maintenance.run_all())
    elif args.cmd == "check-files":
        import datamove
        missing = datamove.missing_files()
        print("All run files are present." if not missing else "Missing:\n  " + "\n  ".join(missing))
        return 1 if missing else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
