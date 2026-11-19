# Questions for the host company's IT

ConflictScan is ready to connect to your systems. Each item below is already
built and tested against a local stand-in, so your answers become settings,
not development work.

**1. Database**
- Which engine and version: PostgreSQL, SQL Server, or other?
- Server name and port.
- Do we get our own database, or a schema inside a shared one? If shared,
  what name?
- Who creates the tables:
  - our installer (needs CREATE/ALTER rights during installs); or
  - your DBA from the SQL script we supply?
- Collation (any case-insensitive Unicode collation works).
- Authentication: SQL login or Windows / integrated?
- A test database to rehearse the migration on.
- Your backup policy for it. Do you back it up, or should our nightly
  `manage.py backup` be enough?

**2. File storage** (uploaded documents and generated RFIs)
- Which of: a local drive on the A100 server, a network share (UNC path), or
  an S3-compatible store such as MinIO? For S3: the endpoint, bucket and
  access key.
- Expected space: plan on 50–200 GB to start. Drawings make up most of it.

**3. Sign-in**
- Can we use Active Directory / LDAP? We need:
  - the LDAPS address and the domain (NetBIOS name);
  - the base DN;
  - the AD groups that should map to ConflictScan admin, manager, engineer
    and viewer.
- Must every user be in one of those groups, or may other domain users get
  read-only access?

**4. Reference data** (optional, read-only)
- Is there a project register we may read (code, name, client, consultant,
  location)? If so: the database and table or view, and a read-only login.
- Is there an employee directory beyond AD?

**5. The A100 servers**
- How many GPUs, and 40 GB or 80 GB each?
- Windows Server or Linux? Is the NVIDIA driver installed, and which CUDA
  version?
- Is Ollama acceptable, or should we use vLLM?
- Can the servers download the model files once, or must we bring them in
  offline? About 60 GB in total.
- Will scans and the web app run on the same machine, or the web app on a
  separate VM?

**6. Network and security**
- The address users will use (for example `https://conflictscan.<company>`),
  and who issues its certificate.
- IIS on the server as the HTTPS front end, or your load balancer?
- A service account for the two Windows services, and its permissions on the
  database and file store.
- Your rules for logging, audit retention and data retention, if any. Our
  defaults are 2 years of audit log and 30 days of trash.
- Antivirus exclusions for the data and scratch folders (scans write many
  temporary images).

**7. Operations**
- Who receives the admin role on day one?
- Maintenance windows for upgrades.
- Monitoring: `/api/health` returns 200 / 503 for your monitoring tool.
