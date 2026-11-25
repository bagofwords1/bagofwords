"""Per-thread HTTP sessions authenticated with Windows credentials.

Shared by the on-premises connectors that speak to servers inside a Windows
domain. One place owns the rules those connectors must all follow:

* every connection is validated and pinned (no rebinding to another address);
* no ambient proxy or ``.netrc`` credentials are picked up;
* internal certificate authorities come from the deployment's trust store,
  never from a path submitted through a connection form;
* a session is never shared between threads, and a Kerberos session is rebuilt
  before its ticket runs out.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from requests_ntlm import HttpNtlmAuth

from app.core.egress import pinned_session

#: Rebuild a Kerberos session when its credentials have less than this left.
_KERBEROS_RENEW_BELOW_SECONDS = 120


class WindowsAuthSessions:
    """Hands each worker thread its own authenticated ``requests.Session``."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        *,
        kerberos: bool = False,
        principal: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        if not kerberos and (not username or not password):
            raise ValueError("NTLM requires a domain username and password.")
        self._username, self._password = username, password
        self._kerberos, self._principal = kerberos, principal
        self._headers = dict(headers or {})
        self._local = threading.local()

    def get(self):
        """The calling thread's session, built on first use."""
        if self._kerberos and hasattr(self._local, "session") and self._local.creds.lifetime < _KERBEROS_RENEW_BELOW_SECONDS:
            self.reset()
        if not hasattr(self._local, "session"):
            session = pinned_session()
            session.trust_env = False  # no ambient proxy / .netrc credentials
            if self._kerberos:
                try:
                    import gssapi
                    from requests_gssapi import HTTPSPNEGOAuth, REQUIRED
                except ImportError as exc:
                    raise ValueError("Install the kerberos extra and mount krb5.conf plus a client keytab.") from exc
                keytab = os.environ.get("KRB5_CLIENT_KTNAME")
                name = gssapi.Name(self._principal, gssapi.NameType.kerberos_principal) if self._principal else None
                # Explicit credentials: never mutate the process Kerberos environment.
                creds = gssapi.Credentials(usage="initiate", name=name,
                                           **({"store": {"client_keytab": keytab}} if keytab else {}))
                self._local.creds = creds
                session.auth = HTTPSPNEGOAuth(creds=creds, mutual_authentication=REQUIRED,
                                              mech=gssapi.MechType.kerberos,
                                              opportunistic_auth=True)
            else:
                session.auth = HttpNtlmAuth(self._username, self._password)
            session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or True
            session.headers.update(self._headers)
            self._local.session = session
        return self._local.session

    def reset(self) -> None:
        """Drop the calling thread's session so the next call starts clean."""
        session = getattr(self._local, "session", None)
        if session is not None:
            session.close()
            del self._local.session
