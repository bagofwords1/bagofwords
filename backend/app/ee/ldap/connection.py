# LDAP Connection Manager
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import logging
import ssl
import re
import time
import hashlib
import json
import uuid
from urllib.parse import urlsplit
from typing import Optional, List, Dict, Any

from app.settings.bow_config import LDAPConfig

logger = logging.getLogger(__name__)


class LDAPSecurityError(RuntimeError):
    """The directory configuration or response is not trustworthy."""


class LDAPAdmissionError(LDAPSecurityError):
    """A complete directory answer denies this object's access."""


class LDAPConnectionManager:
    """Shared LDAP connection layer used by both group sync and bind auth.

    ldap3 is imported lazily so the module loads even when ldap3 is not installed.
    """

    def __init__(self, config: LDAPConfig):
        self.config = config
        self._ldap3 = None

    @property
    def ldap3(self):
        if self._ldap3 is None:
            try:
                import ldap3 as _ldap3
                self._ldap3 = _ldap3
            except ImportError:
                raise ImportError(
                    "ldap3 is required for LDAP integration. Install it with: pip install ldap3"
                )
        return self._ldap3

    def _build_server(self):
        if self.config.use_ssl == self.config.start_tls:
            raise LDAPSecurityError("Configure exactly one of LDAPS or StartTLS")
        url = urlsplit(self.config.url if "://" in self.config.url else
                       ("ldaps://" if self.config.use_ssl else "ldap://") + self.config.url)
        if (url.scheme != ("ldaps" if self.config.use_ssl else "ldap") or
                not url.hostname or url.username or url.password or
                url.path not in ("", "/") or url.query or url.fragment):
            raise LDAPSecurityError("Invalid directory endpoint")
        tls_config = self.ldap3.Tls(validate=ssl.CERT_REQUIRED,
            ca_certs_file=self.config.ca_certs_file or None)

        return self.ldap3.Server(
            url.hostname,
            port=url.port or (636 if self.config.use_ssl else 389),
            use_ssl=self.config.use_ssl,
            tls=tls_config,
            get_info=self.ldap3.NONE,
            connect_timeout=self.config.connection_timeout,
        )

    def get_connection(self):
        """Create a bound service-account connection for search operations."""
        return self._bind(self.config.bind_dn, self.config.bind_password)

    def _bind(self, dn, password):
        if not dn or not password:
            raise LDAPSecurityError("Directory bind credentials are required")
        conn = self.ldap3.Connection(
            self._build_server(),
            user=dn,
            password=password,
            authentication=self.ldap3.SIMPLE,
            auto_bind=False,
            auto_referrals=False,
            auto_range=True,
            read_only=True,
            receive_timeout=self.config.connection_timeout,
            raise_exceptions=True,
        )
        try:
            conn.open()
            if self.config.start_tls and not conn.start_tls():
                raise LDAPSecurityError("Directory TLS upgrade rejected")
            if not conn.bind():
                raise LDAPSecurityError("Directory bind rejected")
            return conn
        except BaseException:
            conn.unbind()
            raise

    def bind_user(self, user_dn: str, password: str) -> bool:
        """Attempt LDAP bind with user's own credentials. Returns True on success."""
        if not user_dn or not password:
            return False
        try:
            conn = self._bind(user_dn, password)
            conn.unbind()
            return True
        except self.ldap3.core.exceptions.LDAPInvalidCredentialsResult:
            return False

    def find_user_dn(self, email: str) -> Optional[str]:
        """Search for a user by email and return their DN."""
        search_base = self.config.user_search_base or self.config.base_dn
        from ldap3.utils.conv import escape_filter_chars
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", self.config.user_email_attribute):
            raise LDAPSecurityError("Invalid directory login attribute")
        search_filter = f"(&{self.config.user_search_filter}({self.config.user_email_attribute}={escape_filter_chars(email)}))"

        conn = self.get_connection()
        try:
            conn.search(
                search_base=search_base,
                search_filter=search_filter,
                search_scope=self.ldap3.SUBTREE,
                attributes=[self.config.user_email_attribute],
                size_limit=2,
            )
            if conn.result.get("result") != 0 or len(conn.entries) > 1:
                raise LDAPSecurityError("Directory lookup was incomplete or ambiguous")
            if conn.entries:
                return str(conn.entries[0].entry_dn)
            return None
        finally:
            conn.unbind()

    @property
    def provider_id(self):
        """Changing directory or admission scope invalidates old identity bindings."""
        return hashlib.sha256(json.dumps([
            self.config.url.lower(), self.config.base_dn.lower(),
            self.config.organization_id, self.config.admission_group_dn,
            self.config.user_search_filter, self.config.kerberos_realm,
        ]).encode()).hexdigest()

    def read_identity(self, dn: str):
        """Read an admitted, enabled AD object over the verified lookup connection."""
        from ldap3.utils.conv import escape_filter_chars
        if not self.config.organization_id or not self.config.admission_group_dn:
            raise LDAPSecurityError("Explicit organization and admission group are required")
        attrs = ["objectGUID", "objectSid", "userPrincipalName", "sAMAccountName",
                 "userAccountControl", "accountExpires", "displayName", self.config.user_email_attribute]
        conn = self.get_connection()
        try:
            conn.search(search_base=dn, search_scope=self.ldap3.BASE,
                search_filter="(&(objectClass=user)(memberOf:1.2.840.113556.1.4.1941:=" +
                    escape_filter_chars(self.config.admission_group_dn) + "))",
                attributes=attrs, time_limit=self.config.connection_timeout)
            if conn.result.get("result") != 0:
                raise LDAPSecurityError("Directory admission lookup failed")
            if len(conn.entries) != 1:
                raise LDAPAdmissionError("Directory admission rejected")
            e = conn.entries[0]
            raw = e.entry_raw_attributes
            guid = str(uuid.UUID(bytes_le=bytes(raw["objectGUID"][0])))
            sid = bytes(raw["objectSid"][0])
            if len(sid) < 8 or sid[0] != 1 or len(sid) != 8 + sid[1] * 4:
                raise LDAPSecurityError("Invalid AD SID")
            uac = int(e["userAccountControl"].value)
            if uac & 2:
                raise LDAPAdmissionError("Directory account disabled")
            expires = int(raw.get("accountExpires", [b"0"])[0])
            if expires not in (0, 9223372036854775807) and expires <= int((time.time() + 11644473600) * 10000000):
                raise LDAPAdmissionError("Directory account expired")
            upn = str(e["userPrincipalName"].value or "")
            realm = (self.config.kerberos_realm or "").upper()
            if not realm or not re.fullmatch(r"[A-Z0-9.-]+", realm):
                raise LDAPSecurityError("A Kerberos realm is required")
            # AD's sAMAccountName is authoritative within the configured realm;
            # email and alternate UPN suffixes are not Kerberos realm mappings.
            sam = str(e["sAMAccountName"].value or "")
            if not sam or any(c in sam for c in "@/\\\x00"):
                raise LDAPSecurityError("Invalid AD account name")
            return {"provider": self.provider_id, "guid": guid, "sid_hex": sid.hex(),
                    "upn": upn, "principal": f"{sam}@{realm}", "dn": str(e.entry_dn),
                    "email": str(e[self.config.user_email_attribute].value or "").lower(),
                    "name": str(e["displayName"].value or sam),
                    "organization_id": self.config.organization_id}
        finally:
            conn.unbind()

    def authenticate_identity(self, email: str, password: str):
        if not password:
            return None
        dn = self.find_user_dn(email)
        if not dn:
            return None
        before = self.read_identity(dn)
        if not self.bind_user(dn, password):
            return None
        after = self.read_identity(dn)
        if before["guid"] != after["guid"] or before["sid_hex"] != after["sid_hex"]:
            raise LDAPSecurityError("Directory object changed during authentication")
        return after

    def search_users(self, filter_override: Optional[str] = None) -> List[Dict[str, Any]]:
        """Search for all users. Returns list of dicts with dn, email, name."""
        search_base = self.config.user_search_base or self.config.base_dn
        search_filter = filter_override or self.config.user_search_filter
        attrs = [
            self.config.user_email_attribute,
            self.config.user_name_attribute,
        ]

        conn = self.get_connection()
        try:
            entries = self._paged_search(conn,
                search_base=search_base,
                search_filter=search_filter,
                attributes=attrs,
            )

            users = []
            for entry in entries:
                email_val = entry[self.config.user_email_attribute].value if self.config.user_email_attribute in entry else None
                name_val = entry[self.config.user_name_attribute].value if self.config.user_name_attribute in entry else None
                if email_val:
                    users.append({
                        "dn": str(entry.entry_dn),
                        "email": str(email_val).lower(),
                        "name": str(name_val) if name_val else None,
                    })
            return users
        finally:
            conn.unbind()

    def search_groups(self) -> List[Dict[str, Any]]:
        """Search for all groups. Returns list of dicts with dn, name, members."""
        search_base = self.config.group_search_base or self.config.base_dn
        attrs = [
            self.config.group_name_attribute,
            self.config.group_member_attribute,
        ]

        conn = self.get_connection()
        try:
            entries = self._paged_search(conn,
                search_base=search_base,
                search_filter=self.config.group_search_filter,
                attributes=attrs,
            )

            groups = []
            for entry in entries:
                name_val = entry[self.config.group_name_attribute].value if self.config.group_name_attribute in entry else None
                member_attr = entry[self.config.group_member_attribute] if self.config.group_member_attribute in entry else None
                members = []
                if member_attr and member_attr.value:
                    raw = member_attr.value
                    members = raw if isinstance(raw, list) else [raw]

                if name_val:
                    groups.append({
                        "dn": str(entry.entry_dn),
                        "name": str(name_val),
                        "members": [str(m) for m in members],
                    })
            return groups
        finally:
            conn.unbind()

    def _paged_search(self, conn, **kwargs):
        """Return only complete snapshots. Partial results must never revoke access."""
        if not 1 <= self.config.page_size <= 1000:
            raise LDAPSecurityError("Directory page size must be between 1 and 1000")
        entries, cookies = [], set()
        cookie = None
        deadline = time.monotonic() + 120
        while True:
            if time.monotonic() >= deadline:
                raise LDAPSecurityError("Directory snapshot deadline exceeded")
            conn.search(**kwargs, search_scope=self.ldap3.SUBTREE,
                        paged_size=self.config.page_size, paged_cookie=cookie,
                        paged_criticality=True, time_limit=self.config.connection_timeout)
            if conn.result.get("result") != 0 or conn.result.get("referrals"):
                raise LDAPSecurityError("Directory snapshot incomplete")
            control = conn.result.get("controls", {}).get("1.2.840.113556.1.4.319", {})
            if "cookie" not in control.get("value", {}):
                raise LDAPSecurityError("Directory omitted paging control")
            entries.extend(conn.entries)
            cookie = control["value"]["cookie"]
            if not cookie:
                return entries
            if cookie in cookies:
                raise LDAPSecurityError("Directory repeated paging cookie")
            cookies.add(cookie)

    def test_connection(self) -> Dict[str, Any]:
        """Test LDAP connectivity and return status info."""
        try:
            conn = self.get_connection()
            server_info = {
                "connected": True,
                "server": self.config.url,
                "vendor": str(conn.server.info.vendor_name) if conn.server.info and conn.server.info.vendor_name else None,
            }
            conn.unbind()
            return server_info
        except Exception as e:
            return {
                "connected": False,
                "server": self.config.url,
                "error": type(e).__name__,
            }
