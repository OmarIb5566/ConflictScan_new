# Installing ConflictScan on the A100 server

Audience: the IT administrator hosting ConflictScan for Rowad.
Everything runs on your own servers:

- the web application;
- the scan workers;
- the AI models.

Nothing is sent to the internet, there are no cloud services and no Docker.
The commands are for Windows Server in PowerShell; Linux works the same way
with `python3` and systemd units.

## 1. What runs where

| Part | Process | Needs |
|---|---|---|
| Web application | `serve.py` (waitress), Windows service `ConflictScanWeb` | Python 3.11+, network access to the database and file store |
| Scan worker | `worker.py`, Windows service `ConflictScanWorker` (one per GPU server) | Python, access to the model servers, database and file store |
| Model servers | Ollama, one instance per GPU (or vLLM) | NVIDIA driver, the models below |
| Database | Any of: PostgreSQL, SQL Server, SQLite (single server only) | One empty database / schema |
| File store | A folder, a network share (UNC) or an S3-compatible store (MinIO) | Modify rights for the service account |
| HTTPS | IIS with URL Rewrite + Application Request Routing (or your load balancer) | The company certificate |

On a single A100 server, all of these can run on the same machine.

## 2. Prerequisites

1. Install **Python 3.12 (64-bit)** for all users and tick "Add to PATH".
2. Install the **NVIDIA driver** and check that `nvidia-smi` lists every GPU.
3. Install **Ollama** for Windows.
4. Install the **driver for your database**:
   - PostgreSQL: nothing extra.
   - SQL Server: *Microsoft ODBC Driver 18 for SQL Server*.
5. Create a **service account**, e.g. `DOMAIN\svc-conflictscan`. It needs:
   - read access to the code folder;
   - modify access to the data folder and the file store;
   - a login on the database with rights to create tables in its schema
     (only for the first install and upgrades).

## 3. Code and Python packages

```powershell
# copy the release (or git clone) to e.g. D:\ConflictScan
cd D:\ConflictScan
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pip install pywin32            # Windows services
.\.venv\Scripts\python -m pip install psycopg2-binary    # PostgreSQL   - or -
.\.venv\Scripts\python -m pip install pyodbc             # SQL Server
.\.venv\Scripts\python -m pip install ldap3              # only if users sign in with Active Directory
```

If the server has no internet access:

1. Run `pip download -r requirements.txt -d wheels` on a machine that has it.
2. Copy the `wheels` folder to the server.
3. Install with `pip install --no-index --find-links wheels -r requirements.txt`.

## 4. Settings

Copy `conflictscan.example.toml` to `conflictscan.toml` in the code folder and
fill it in. Every option is explained in the file. The minimum for
production:

```toml
[server]
host = "127.0.0.1"          # IIS in front; use 0.0.0.0 only without a reverse proxy
port = 5000
https = true
behind_proxy = true
embedded_worker = false     # scans run in the ConflictScanWorker service
secret_key = "<64 random hex characters>"   # python -c "import secrets; print(secrets.token_hex(32))"

[database]
url = "postgresql+psycopg2://conflictscan:PASSWORD@dbserver:5432/conflictscan"
# url = "mssql+pyodbc://conflictscan:PASSWORD@dbserver/conflictscan?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
schema = "conflictscan"

[storage]
data_dir = "D:/ConflictScan/data"          # scratch space, logs
files_root = "\\\\fileserver\\ConflictScan" # uploads and outputs (or a local folder)

[auth]
mode = "ldap+local"         # or "local"; see [auth.ldap]

[fleet]
profile = "a100"
```

Environment variables override the file (names in `config.py`). That is
useful for keeping passwords out of the file. For example, set
`DATABASE_URL` as a machine environment variable for the service account.

## 5. Database

Pick one of two routes.

**ConflictScan creates its own tables** (needs rights to create tables in the
schema):

```powershell
.\.venv\Scripts\python manage.py init
```

**Your DBA creates the tables from a script:**

```powershell
.\.venv\Scripts\python manage.py sql --url "mssql+pyodbc://" --out conflictscan_schema.sql
.\.venv\Scripts\python manage.py sql --url "postgresql://" --out conflictscan_schema.sql
```

The script creates the `conflictscan` schema, 19 tables and the
`alembic_version` table that records the schema revision. Every upgrade ships
with a new migration, and `manage.py sql --from-rev <current>` produces only
the change.

Then create the first administrator:

```powershell
.\.venv\Scripts\python manage.py create-admin admin --name "ConflictScan Admin"
.\.venv\Scripts\python manage.py status       # checks settings, database, files, sign-in, model servers
```

## 6. Models on the A100s

Run **one Ollama instance per GPU**, each on its own port, so every model can
stay loaded on its GPU. Example for 4 GPUs (as Windows services via NSSM, or
scheduled tasks at start-up):

```powershell
# GPU 0
$env:CUDA_VISIBLE_DEVICES="0"; $env:OLLAMA_HOST="127.0.0.1:11434"; $env:OLLAMA_NUM_PARALLEL="4"; $env:OLLAMA_MAX_LOADED_MODELS="4"; ollama serve
# GPU 1 -> 11435, GPU 2 -> 11436, GPU 3 -> 11437
```

Pull the models once on each instance that will serve them:

```powershell
$env:OLLAMA_HOST="127.0.0.1:11434"
ollama pull qwen2.5vl:7b; ollama pull glm-ocr; ollama pull gemma4:e4b; ollama pull bge-m3
$env:OLLAMA_HOST="127.0.0.1:11435"
ollama pull qwen3.6:27b; ollama pull qwen3:14b; ollama pull gpt-oss
```

List the servers in `conflictscan.toml` and pin roles to GPUs:

```toml
[[model_servers]]
name = "gpu0-vision"
url = "http://127.0.0.1:11434"
gpus = [0]
roles = ["page_triage", "ocr", "drawing_overview", "drawing_reader", "drawing_second", "embedder"]

[[model_servers]]
name = "gpu1-reasoning"
url = "http://127.0.0.1:11435"
gpus = [1]
roles = ["adjudicator", "drafter"]

[[model_servers]]
name = "gpu2-checks"
url = "http://127.0.0.1:11436"
gpus = [2]
roles = ["extractor", "verifier"]
```

How much fits on each card:

| Card | Fits at once | Suggested split |
|---|---|---|
| 80 GB | the whole fleet (~75 GB with qwen3.6:27b + gpt-oss + qwen3:14b + qwen2.5vl:7b + gemma4 + bge-m3) | 2 GPUs: vision on one, reasoning on the other, for headroom and parallel requests |
| 40 GB | qwen3.6:27b + qwen2.5vl:7b, or gpt-oss + qwen3:14b + bge-m3 | one card per row of the table above |

`python manage.py fleet` shows which model each role will actually use.
**Admin console → AI fleet** shows the same, with a *Test* button per role.

## 7. Windows services

```powershell
# elevated PowerShell, in D:\ConflictScan
.\service\install_services.ps1 -Python D:\ConflictScan\.venv\Scripts\python.exe -ServiceAccount "DOMAIN\svc-conflictscan" -ServicePassword (Read-Host -AsSecureString)
```

This installs, configures and starts two services:

- **ConflictScanWeb** runs the web app on 127.0.0.1:5000.
- **ConflictScanWorker** runs the scans.

Both start automatically and restart if they stop. Logs are in
`<data_dir>\logs\web.log` and `worker.log`.

On other GPU servers, run only the worker service. Use the same settings file,
database and file store, with the `[[model_servers]]` entries of that machine.
The workers share the queue through the database.

## 8. HTTPS with IIS

1. Install the IIS features *URL Rewrite* and *Application Request Routing*.
   In *ARR → Server Proxy Settings*, tick *Enable proxy*.
2. Create a site bound to `https://conflictscan.<company>` with the company
   certificate, pointing at an empty folder.
3. Copy `service\iis-web.config` into that folder as `web.config`.
4. Allow the `HTTP_X_FORWARDED_PROTO` server variable
   (*URL Rewrite → View Server Variables → Add*).
5. Set `maxAllowedContentLength` to match `scans.max_upload_mb`.

## 9. Check the installation

1. `python manage.py status`: every line should be OK.
2. Open `https://conflictscan.<company>/`, sign in as the admin, and open
   **Admin console → System health**. Everything should be green or *local
   stand-in*.
3. Run a small scan with two short PDFs. It should finish, and
   **Admin console → Scan queue** shows which worker ran it.
4. Set up the backup (see `ADMIN.md`).

## 10. Upgrades

```powershell
Stop-Service ConflictScanWeb, ConflictScanWorker
.\.venv\Scripts\python manage.py backup D:\Backups\before-upgrade.zip
# replace the code (keep conflictscan.toml and data\)
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python manage.py init          # applies new database migrations
Start-Service ConflictScanWorker, ConflictScanWeb
```
