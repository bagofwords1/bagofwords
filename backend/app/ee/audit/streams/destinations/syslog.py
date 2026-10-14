# Syslog audit stream destination: RFC 5424 messages over TCP (+TLS) with
# RFC 5425/6587 octet-counting framing; optional CEF payloads.
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import asyncio
import json
import os
import socket
import ssl
import tempfile
from typing import List, Optional

from app.ee.audit.streams.destinations.base import (
    INVALID,
    RETRYABLE,
    Destination,
    FieldSpec,
    SendResult,
    short_error,
)

_FACILITY_LOG_AUDIT = 13
_SEVERITY_INFO = 6
_PRI = _FACILITY_LOG_AUDIT * 8 + _SEVERITY_INFO  # <110>


def _hostname() -> str:
    try:
        return socket.gethostname()[:255] or "-"
    except Exception:
        return "-"


def _cef_header(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("|", "\\|")


def _cef_ext(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("=", "\\=").replace("\r", "\\r").replace("\n", "\\n")


def format_cef(e: dict, version: str = "1") -> str:
    actor = e.get("actor") or {}
    ctx = e.get("context") or {}
    target = (e.get("targets") or [{}])[0] if e.get("targets") else {}
    ext = {
        "rt": e.get("occurred_at"),
        "externalId": e.get("id"),
        "suser": actor.get("email") or actor.get("id"),
        "suid": actor.get("id"),
        "src": ctx.get("ip_address"),
        "requestClientApplication": ctx.get("user_agent"),
        "cs1Label": "actorType", "cs1": actor.get("type"),
        "cs2Label": "resourceType", "cs2": target.get("type"),
        "cs3Label": "resourceId", "cs3": target.get("id"),
        "cs4Label": "organizationId", "cs4": (e.get("organization") or {}).get("id"),
        "msg": json.dumps(e.get("metadata") or {}, separators=(",", ":"), default=str),
    }
    exts = " ".join(f"{k}={_cef_ext(v)}" for k, v in ext.items() if v not in (None, ""))
    action = e.get("action", "")
    return f"CEF:0|Bag of Words|Bag of Words|{_cef_header(version)}|{_cef_header(action)}|{_cef_header(action)}|3|{exts}"


def format_rfc5424(e: dict, *, app_name: str, fmt: str, hostname: Optional[str] = None) -> bytes:
    msgid = "".join(ch for ch in (e.get("action") or "-") if 33 <= ord(ch) <= 126)[:32] or "-"
    msg = format_cef(e) if fmt == "cef" else json.dumps(e, separators=(",", ":"), default=str)
    line = f"<{_PRI}>1 {e.get('occurred_at') or '-'} {hostname or _hostname()} {app_name[:48] or '-'} - {msgid} - {msg}"
    return line.encode("utf-8")


def frame(payload: bytes) -> bytes:
    """Octet-counting framing (RFC 5425 §4.3 / RFC 6587 §3.4.1)."""
    return str(len(payload)).encode() + b" " + payload


class SyslogDestination(Destination):
    type = "syslog"
    max_batch = 500
    fields = [
        FieldSpec("host", "text", required=True),
        FieldSpec("port", "number", required=True, default=6514),
        FieldSpec("tls", "bool", default=True),
        FieldSpec("format", "select", default="json", options=["json", "cef"]),
        FieldSpec("ca_cert", "textarea", advanced=True),
        FieldSpec("app_name", "text", default="bagofwords", advanced=True),
        FieldSpec("client_cert", "textarea", secret=True, advanced=True),
        FieldSpec("client_key", "textarea", secret=True, advanced=True),
    ]

    def _tls(self) -> bool:
        v = self.cfg("tls", True)
        return not (v is False or str(v).lower() in ("false", "0", "no"))

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cadata=self.cfg("ca_cert") or None)
        if self.secrets.get("client_cert") and self.secrets.get("client_key"):
            # load_cert_chain only reads files; keep them for the call only.
            with tempfile.TemporaryDirectory() as d:
                cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
                with open(cp, "w") as f:
                    f.write(self.secrets["client_cert"])
                with open(kp, "w") as f:
                    f.write(self.secrets["client_key"])
                os.chmod(kp, 0o600)
                ctx.load_cert_chain(cp, kp)
        return ctx

    async def send(self, events: List[dict]) -> SendResult:
        host = self.cfg("host")
        port = int(self.cfg("port"))
        fmt = self.cfg("format")
        app_name = self.cfg("app_name")
        hostname = _hostname()
        data = b"".join(frame(format_rfc5424(e, app_name=app_name, fmt=fmt, hostname=hostname)) for e in events)
        writer = None
        try:
            ssl_ctx = self._ssl_context() if self._tls() else None
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx, server_hostname=host if ssl_ctx else None),
                timeout=10,
            )
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=20)
        except ssl.SSLCertVerificationError as e:
            return SendResult(INVALID, short_error(f"TLS verification failed: {e}"))
        except ssl.SSLError as e:
            return SendResult(INVALID, short_error(f"TLS error: {e}"))
        except (asyncio.TimeoutError, OSError) as e:
            return SendResult(RETRYABLE, short_error(f"{type(e).__name__}: {e}"))
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=5)
                except Exception:
                    pass
        # Syslog has no application-level ack: success means the bytes were
        # accepted by the TCP/TLS stream.
        return SendResult.success()
