"""
Ready-to-connect adapters
=========================
Each one wraps an outside system the host company's IT controls, behind a
small interface, with a working local stand-in. Switching to the real system
is a settings change (conflictscan.toml), not a code change:

    identity.py       who can sign in: local accounts, Active Directory / LDAP
    files.py          where uploads and outputs live: local folder, network
                      share, S3-compatible object store (MinIO)
    reference.py      read-only company data: project register, employees
    model_servers.py  the GPU servers running the local models

The admin health page reports each adapter as live, local stand-in, or not
configured.
"""
