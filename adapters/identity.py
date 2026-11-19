"""
Identity adapter - who can sign in
==================================
CONFLICTSCAN_AUTH chooses the sources, tried in order:

    local         accounts created in ConflictScan (default)
    ldap          Active Directory / LDAP only
    ldap+local    directory first; local accounts still work (keep one local
                  admin as a break-glass account if the directory is down)

Directory sign-in (needs the ldap3 package):

    CONFLICTSCAN_LDAP_URL            ldaps://dc01.rowad.local:636
    CONFLICTSCAN_LDAP_DOMAIN         ROWAD           (users sign in as ROWAD\\user;
                                     a UPN like user@rowad.local also works)
    CONFLICTSCAN_LDAP_BASE_DN        DC=rowad,DC=local
    CONFLICTSCAN_LDAP_USER_FILTER    (sAMAccountName={username})
    CONFLICTSCAN_LDAP_GROUPS         JSON: {"CN=ConflictScan-Admins,OU=...": "admin",
                                            "CN=ConflictScan-Engineers,OU=...": "engineer"}
    CONFLICTSCAN_LDAP_DEFAULT_ROLE   role for users in none of the groups (default viewer)
    CONFLICTSCAN_LDAP_REQUIRE_GROUP  1 = refuse users in none of the groups

The directory's permanent id (objectGUID, or entryUUID) is stored as the
user's external_id, so a renamed account keeps its projects and history.
Roles from groups are re-applied at every sign-in; an admin can still set a
stricter status (disabled) in ConflictScan.

Windows compatible.
"""

import json
import os
from typing import Any, Dict, Optional, Tuple

from werkzeug.security import check_password_hash

import storage

ROLE_ORDER = ("viewer", "engineer", "manager", "admin")


def mode() -> str:
    return os.environ.get("CONFLICTSCAN_AUTH", "local").lower()


def sources() -> Tuple[str, ...]:
    m = mode()
    if m == "ldap":
        return ("ldap",)
    if m in ("ldap+local", "local+ldap"):
        return ("ldap", "local")
    return ("local",)


class AuthResult:
    def __init__(self, user: Optional[Dict[str, Any]] = None, error: str = None):
        self.user = user
        self.error = error


def authenticate(username: str, password: str) -> AuthResult:
    """Check the credentials against each configured source; returns the ConflictScan user record."""
    if not username or not password:
        return AuthResult(error="Wrong username or password.")
    last_error = "Wrong username or password."
    for source in sources():
        if source == "local":
            user = storage.get_user_by_username(_bare(username))
            if user and user.get("auth_source", "local") == "local" and user.get("password_hash") \
                    and check_password_hash(user["password_hash"], password):
                return AuthResult(user=user)
        elif source == "ldap":
            try:
                info = ldap_lookup(username, password)
            except LdapUnavailable as exc:
                last_error = f"The company directory could not be reached ({exc}). Try again, or ask an administrator."
                continue
            if info:
                result = _sync_directory_user(info)
                if result.user or result.error:
                    return result
    return AuthResult(error=last_error)


def _bare(username: str) -> str:
    username = username.strip()
    if "\\" in username:
        username = username.split("\\", 1)[1]
    return username


# ---------------------------------------------------------------------------
# LDAP / Active Directory
# ---------------------------------------------------------------------------

class LdapUnavailable(Exception):
    pass


def _group_map() -> Dict[str, str]:
    raw = os.environ.get("CONFLICTSCAN_LDAP_GROUPS") or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {k.lower(): v for k, v in data.items() if v in ROLE_ORDER}


def role_from_groups(groups) -> Optional[str]:
    mapping = _group_map()
    best = None
    for g in groups or []:
        role = mapping.get(str(g).lower())
        if role and (best is None or ROLE_ORDER.index(role) > ROLE_ORDER.index(best)):
            best = role
    return best


def ldap_lookup(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Bind as the user and read their entry: {external_id, username, display_name, email, groups} or None."""
    url = os.environ.get("CONFLICTSCAN_LDAP_URL")
    if not url:
        raise LdapUnavailable("not configured")
    try:
        import ldap3
    except ImportError as exc:
        raise LdapUnavailable("the ldap3 package is not installed") from exc
    bare = _bare(username)
    domain = os.environ.get("CONFLICTSCAN_LDAP_DOMAIN", "")
    if "@" in bare:
        bind_name = bare
        bare = bare.split("@", 1)[0]
    elif domain:
        bind_name = f"{domain}\\{bare}"
    else:
        bind_name = bare
    try:
        server = ldap3.Server(url, get_info=ldap3.NONE, connect_timeout=5)
        conn = ldap3.Connection(server, user=bind_name, password=password,
                                authentication=ldap3.NTLM if "\\" in bind_name else ldap3.SIMPLE,
                                receive_timeout=10)
        if not conn.bind():
            return None
    except ldap3.core.exceptions.LDAPSocketOpenError as exc:
        raise LdapUnavailable(str(exc)) from exc
    except ldap3.core.exceptions.LDAPException:
        return None
    try:
        flt = os.environ.get("CONFLICTSCAN_LDAP_USER_FILTER", "(sAMAccountName={username})")
        flt = flt.replace("{username}", ldap3.utils.conv.escape_filter_chars(bare))
        conn.search(os.environ.get("CONFLICTSCAN_LDAP_BASE_DN", ""), flt,
                    attributes=["objectGUID", "entryUUID", "displayName", "mail", "memberOf", "sAMAccountName"])
        if not conn.entries:
            return None
        e = conn.entries[0]
        attrs = e.entry_attributes_as_dict
        ext = (attrs.get("objectGUID") or attrs.get("entryUUID") or [e.entry_dn])[0]
        return {
            "external_id": str(ext),
            "username": (attrs.get("sAMAccountName") or [bare])[0],
            "display_name": (attrs.get("displayName") or [bare])[0],
            "email": (attrs.get("mail") or [None])[0],
            "groups": attrs.get("memberOf") or [],
        }
    finally:
        conn.unbind()


def _sync_directory_user(info: Dict[str, Any]) -> AuthResult:
    role = role_from_groups(info.get("groups"))
    require = os.environ.get("CONFLICTSCAN_LDAP_REQUIRE_GROUP", "0").lower() not in ("0", "false", "no", "")
    if role is None:
        if require:
            return AuthResult(error="Your directory account is not in a ConflictScan group. Ask IT for access.")
        role = os.environ.get("CONFLICTSCAN_LDAP_DEFAULT_ROLE", "viewer")
    user = storage.get_user_by_external("ldap", info["external_id"])
    if user is None:
        taken = storage.get_user_by_username(info["username"])
        if taken and taken.get("auth_source") == "local":
            # A local account with the same name: link it to the directory identity.
            storage.update_user(taken["id"], external_id=info["external_id"], auth_source="ldap", role=role,
                                display_name=info["display_name"], email=info.get("email"))
            return AuthResult(user=storage.get_user(taken["id"]))
        username = info["username"] if not taken else f"{info['username']}.{info['external_id'][:6]}"
        user = storage.create_user(username, info["display_name"], None, role=role, auth_source="ldap",
                                   external_id=info["external_id"], email=info.get("email"))
        return AuthResult(user=user)
    storage.update_user(user["id"], role=role, display_name=info["display_name"], email=info.get("email"))
    return AuthResult(user=storage.get_user(user["id"]))


def health() -> Dict[str, Any]:
    if "ldap" not in sources():
        return {"ok": True, "state": "local", "detail": "Local ConflictScan accounts (directory sign-in not configured)"}
    url = os.environ.get("CONFLICTSCAN_LDAP_URL")
    if not url:
        return {"ok": False, "state": "not configured", "detail": "CONFLICTSCAN_AUTH includes ldap but no LDAP URL is set"}
    try:
        import ldap3
        server = ldap3.Server(url, connect_timeout=3)
        conn = ldap3.Connection(server)
        conn.open()
        conn.unbind()
        return {"ok": True, "state": "live", "detail": f"Directory {url} reachable"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "state": "unreachable", "detail": f"Directory {url}: {exc}"}
