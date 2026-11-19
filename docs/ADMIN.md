# ConflictScan administrator's manual

Audience: Rowad's ConflictScan administrators. Most of this happens in the
**Admin console** (sidebar → Admin). The rest is done with `manage.py` on the
server.

## People and access

**Accounts.** Admin console → Users → *Add user* creates an account with a
temporary password. The person chooses their own at first sign-in. Open
sign-up is off. If you turn it on (`auth.allow_signup`), new accounts wait
for your approval.

**Company directory.** With `auth.mode = "ldap"` or `"ldap+local"`, people
sign in with their Windows account.

- `[auth.ldap] groups` maps AD groups to roles, re-applied at every sign-in.
- Keep one *local* admin account as a break-glass login for when the
  directory is down.

**Roles:**

| Role | Can |
|---|---|
| Admin | everything, including this console |
| Project manager | manage every team project: members, settings, deleting, approving and issuing RFIs |
| Engineer | upload, scan, triage, edit RFIs, add RFIs to the log, record responses |
| Viewer | read and download only |

A project's **Members** dialog gives someone a different role in that one
project. *Private* projects are visible to their members (and admins) only.

**Leavers.** *Disable* the account: it is signed out at once, and its work
and history stay. *Reset password* issues a new temporary password and also
lifts a lock caused by too many failed sign-ins.

## The scan queue

Admin console → **Scan queue** lists:

- every waiting and running scan;
- the worker running each one, and the attempt number;
- recent results.

You can *Move to top* or *Cancel* a scan.

Scans survive restarts, and a worker that dies has its scans picked up by
another. If a model server stops answering, the scan retries with back-off
(`scans.max_attempts`). Problems with the documents themselves are not
retried; the engineer sees the reason on the scan.

## The AI fleet

Admin console → **AI fleet** shows:

- the active profile;
- which model each job uses, and the server it runs on;
- the last 24 hours of calls, with average time, cache hits and failures.

From there you can:

- **switch the profile:**
  - `baseline` is the single model, as before the fleet;
  - `workstation` is the fleet on one GPU;
  - `a100` keeps every model loaded and adds the second drawing reader and
    a vote on critical findings;
- **override one role** with any model installed on your servers (cloud
  models are refused);
- **test** a role.

Changes apply to the next scan; no restart is needed. The full role list is in
`config/fleet.toml`.

**Benchmark** before changing defaults:

```powershell
python -m bench.run --cases 10 --no-cache --profile workstation --profile a100
python -m bench.run --compare data\bench\*
```

It scans real past RFIs from the training folder and reports, per profile:
- how often the engineer's actual issue was found;
- findings per case;
- disputed findings;
- time per case.

**RFI memory.** `python manage.py index-rfis "<folder of past RFIs>"` adds
past RFIs and client replies to the company memory, which the drafter uses
for house style. Issued and answered RFIs, and imported RFI logs, are added
automatically.

## System health

Admin console → **System health** shows:

- database (with its schema revision);
- file store (free space);
- sign-in source;
- reference data;
- each model server, with its loaded models and GPU memory;
- the job queue and workers.

Anything red shows the error. *Local stand-in* means a link that still uses
the local substitute until IT connects the real system (for example the
project register).

From a server prompt, `python manage.py status` prints the same checks.

## Audit log and usage

- **Audit log.** Sign-ins (including failed ones), account and role changes,
  project and member changes, scans, triage, RFI edits, the RFI log, every
  download and every settings change. Filter by person or action. Kept for
  `retention.audit_days`.
- **Usage.** Scans, pages and model minutes per person and project.
  `scans.daily_page_limit` sets a fair-use cap per person per day (admins are
  exempt); 0 means no cap.

## Backups and retention

Back up nightly, plus your database's own backups if you prefer them:

```powershell
# scheduled task, as the service account
D:\ConflictScan\.venv\Scripts\python D:\ConflictScan\manage.py backup "D:\Backups\conflictscan-$(Get-Date -f yyyyMMdd).zip"
```

To restore into an empty database:

```powershell
python manage.py restore D:\Backups\conflictscan-20261119.zip
```

The restore checks every table's checksum and changes nothing if one
differs. `verify` compares a live database with a backup.

Retention (`[retention]` in the settings) runs hourly in the workers, or on
demand with `python manage.py purge`:

| Setting | Default | What |
|---|---|---|
| `trash_days` | 30 | deleted projects, revisions and RFIs, then removed for good |
| `audit_days` | 730 | audit log entries |
| `finished_jobs_days` | 30 | finished rows of the job queue |
| `debug_days` | 30 | drawing-reader diagnostic logs |
| `scratch_hours` | 24 | leftover scratch folders |

## Settings

All deployment settings are in `conflictscan.toml`; Admin console →
**Settings** shows the values in force, with secrets masked. Change the file,
then restart the services. The exception is the AI fleet, which is changed in
the console.

## Security notes

- Sessions end after `auth.idle_minutes` without activity (480). Sign-in
  locks a username after `auth.login_max_fails` wrong passwords (5) for 15
  minutes.
- Every change request carries an anti-forgery token. Pages cannot be framed
  and load nothing from the internet.
- Uploads are checked:
  - the content must match the extension;
  - size, page and image limits apply;
  - password-protected PDFs and unsafe Office files are refused.
  Flagged files block the scan.
- Behind IIS, set `server.https` and `server.behind_proxy` to `true`.
