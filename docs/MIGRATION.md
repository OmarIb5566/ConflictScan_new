# Moving ConflictScan from the workstation to the host servers

Audience: whoever moves the data (Rowad's admin with the host's IT /
DBA). The move copies every project, revision, RFI, comment, user and file
from the workstation (SQLite + a local folder) to the host's database and
file store, and proves the copy is exact before anything is switched over.

## How it stays safe

- The export is **engine-neutral**: JSON rows per table, plus every stored
  file. It loads into PostgreSQL, SQL Server or SQLite.
- Each table's row count and SHA-256 checksum are recorded at export time.
  The import loads everything in **one transaction**, then re-reads the
  target and compares. Any difference **rolls the whole import back**, so the
  target stays as it was.
- File paths in the database are relative keys (`runs/<id>/inputs/...`), so
  files move to a new drive, share or object store without editing a row.
- The workstation is not changed by an export. You can repeat the move as
  often as you like.

## Steps

**1. On the workstation: freeze and back up.**
- Let running scans finish (Admin console → Scan queue is empty).
- Stop the app.
- Run:

```powershell
python manage.py backup D:\Moves\conflictscan-move.zip      # database + every file, with checksums
```

**2. On the host: prepare an empty target.** Do sections 3–5 of
`INSTALL.md`: code, settings and database schema. Point `DATABASE_URL` /
`[database]` at the host database and `[storage] files_root` at the host file
store. Do **not** create users yet; they come with the data.

**3. On the host: restore and verify.**

```powershell
python manage.py restore D:\Moves\conflictscan-move.zip
#   users: 12 rows loaded ... Verified: every table's row count and checksum match the export.
python manage.py verify D:\Moves\conflictscan-move.zip      # "Identical."
python manage.py check-files                                # "All run files are present."
```

If the target already holds data (for example a test run), the restore stops
and changes nothing. Add `--replace` only if you mean to overwrite it.

**4. Switch over.**
- Start the services.
- Sign in and open a few projects, their findings and RFIs, and download an
  RFI PDF.
- Point users at the new address.
- Keep the workstation copy read-only for a few weeks.

## Moving between servers later

Use the same three commands, for example from PostgreSQL to SQL Server or to a
new file server. If only the file store moves, copy the folder tree as it is
and change `files_root`; the database needs no change.

## If something goes wrong

| Symptom | Meaning / fix |
|---|---|
| `Verification failed - nothing was imported` + table names | The target engine changed some values (for example a collation that alters text). Send the message to the developer; the target is unchanged. |
| `The target database already has N rows` | Restore into an empty database, or add `--replace`. |
| `check-files` lists missing files | The file store root is wrong, or files were not copied. Fix `files_root` and run `restore` again. |
| Permission errors creating tables | The DBA creates the schema from `manage.py sql` first; restore then only inserts rows. |

## Database requirements to give the DBA

- One database or schema named `conflictscan` (configurable), Unicode, with
  any case-insensitive collation.
- A login with **SELECT/INSERT/UPDATE/DELETE** on the schema for daily use,
  plus **CREATE/ALTER** during installs and upgrades (or the DBA runs the
  scripts).
- The design: text primary keys, UTC timestamps, JSON stored as text. There
  are no stored procedures, triggers or engine-specific types.
- Typical size: a few MB of rows per project. Uploaded documents live in the
  file store, not the database.
