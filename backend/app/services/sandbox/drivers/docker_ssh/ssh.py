"""The SSH layer of the docker-ssh driver.

Rules it enforces, in one place:

* **Key authentication and a pinned host key.** A server is only logged in
  to once its host key is pinned; before that, the key can be *read* (without
  authenticating) so an admin can confirm its fingerprint.
* **Commands are argument lists.** Every command is a fixed template whose
  arguments are shell-quoted here. Nothing a model wrote is ever part of a
  command line: code and files travel through ``sandboxd``, and secrets
  travel on standard input.
* **root, or ``sudo -n``.** The mode is detected once per connection. When the
  SSH user is not root, the ``sudo -n`` prefix is added here and nowhere else,
  so both modes share every command template. ``sudo -n`` never prompts: if
  sudo would ask for a password the command fails at once.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from app.services.sandbox.drivers.base import SandboxDriverError

# asyncssh logs every connection, channel and full command line at INFO. That
# is dozens of lines per sandbox run, and command lines do not belong in the
# application log. Warnings and errors still come through.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

MODE_ROOT = "root"
MODE_SUDO = "sudo"

CONNECT_TIMEOUT = 15
DEFAULT_TIMEOUT = 60


class SSHError(SandboxDriverError):
    pass


@dataclass
class Result:
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_status == 0

    def detail(self, limit: int = 400) -> str:
        text = (self.stderr or self.stdout or "").strip()
        return text[-limit:]


def generate_keypair(comment: str = "bow-sandbox") -> tuple:
    """A new ed25519 key pair: (private key, public key), both in OpenSSH format."""
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    return key.export_private_key("openssh").decode("ascii"), key.export_public_key("openssh").decode("ascii").strip()


def public_key_of(private_key: str) -> str:
    """The OpenSSH public key for a pasted private key. Raises ``SSHError`` if it is not one."""
    import asyncssh

    try:
        key = asyncssh.import_private_key(private_key)
    except Exception as exc:  # asyncssh.KeyImportError and friends; never echo the input
        raise SSHError("The SSH private key could not be read. Paste an unencrypted OpenSSH or PEM private key.",
                       code="invalid_credentials") from exc
    return key.export_public_key("openssh").decode("ascii").strip()


def fingerprint_of(public_key: str) -> str:
    import asyncssh

    return asyncssh.import_public_key(public_key).get_fingerprint("sha256").rstrip("=")


class SSHHost:
    """One SSH connection to one server, opened on first use."""

    def __init__(self, address: str, port: int, user: str, private_key: Optional[str],
                 pinned_host_key: Optional[str] = None):
        self.address = address
        self.port = port
        self.user = user
        self._private_key = private_key
        self.pinned_host_key = pinned_host_key
        self._conn = None
        self.mode: Optional[str] = None

    # ── host key ──

    async def fetch_host_keys(self) -> list:
        """The server's host keys (OpenSSH public keys), read without logging in.

        A server has one key per algorithm and presents whichever the client
        asks for first, so each is asked for in turn: ED25519 first, which is
        the one ``ssh-keygen -l`` and cloud consoles usually show.
        """
        import asyncssh

        keys, last_error = [], None
        for algorithms in _HOST_KEY_ALGORITHMS:
            try:
                key = await asyncio.wait_for(
                    asyncssh.get_server_host_key(self.address, self.port, server_host_key_algs=algorithms),
                    timeout=CONNECT_TIMEOUT,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                raise SSHError(f"Could not reach {self.address}:{self.port} over SSH ({_why(exc)})", code="unreachable") from exc
            except asyncssh.Error as exc:  # the server has no key of this type
                last_error = exc
                continue
            if key is not None:
                keys.append(key.export_public_key("openssh").decode("ascii").strip())
        if not keys:
            raise SSHError(
                f"The server at {self.address}:{self.port} did not present an SSH host key ({_why(last_error) if last_error else 'no key'})",
                code="unreachable",
            )
        return keys

    async def fetch_host_key(self) -> str:
        """The server's preferred host key (ED25519 when it has one)."""
        return (await self.fetch_host_keys())[0]

    # ── connecting ──

    async def connect(self, *, password: Optional[str] = None):
        if self._conn is not None:
            return self._conn
        import asyncssh

        if not self.pinned_host_key:
            raise SSHError(
                "The server's SSH host key is not confirmed yet. Run Test connection and confirm its fingerprint.",
                code="host_key_unconfirmed",
            )
        pinned = asyncssh.import_public_key(self.pinned_host_key)
        options = dict(
            host=self.address, port=self.port, username=self.user,
            known_hosts=([pinned], [], []),
            # Ask for the pinned key's type, or the server may present another of its keys.
            server_host_key_algs=_algorithms_for(pinned),
            connect_timeout=CONNECT_TIMEOUT, keepalive_interval=30, agent_path=None, config=None,
        )
        if password is not None:
            options.update(password=password, client_keys=None, preferred_auth="password,keyboard-interactive")
        else:
            if not self._private_key:
                raise SSHError("This compute has no SSH private key", code="invalid_credentials")
            try:
                options.update(client_keys=[asyncssh.import_private_key(self._private_key)],
                               preferred_auth="publickey")
            except Exception as exc:
                raise SSHError("The stored SSH private key could not be read", code="invalid_credentials") from exc
        try:
            self._conn = await asyncio.wait_for(asyncssh.connect(**options), timeout=CONNECT_TIMEOUT + 5)
        except asyncssh.HostKeyNotVerifiable as exc:
            raise SSHError(
                "The server's SSH host key does not match the pinned one. If the server was reinstalled, "
                "confirm its new fingerprint; otherwise do not connect.", code="host_key_mismatch",
            ) from exc
        except asyncssh.PermissionDenied as exc:
            what = "the one-time password" if password is not None else "BoW's SSH key"
            raise SSHError(f"The server refused {what} for user '{self.user}'", code="auth_failed") from exc
        except (OSError, asyncio.TimeoutError, asyncssh.Error) as exc:
            raise SSHError(f"Could not connect to {self.address}:{self.port} over SSH ({_why(exc)})", code="unreachable") from exc
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            try:
                await asyncio.wait_for(self._conn.wait_closed(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    # ── running commands ──

    async def _exec(self, command: str, *, input: Optional[Union[str, bytes]], timeout: float) -> Result:
        import asyncssh

        conn = await self.connect()
        if isinstance(input, bytes):
            input = input.decode("utf-8")
        try:
            completed = await asyncio.wait_for(conn.run(command, input=input, check=False), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise SSHError(f"A command on the server did not finish within {int(timeout)} seconds", code="timeout") from exc
        except (OSError, asyncssh.Error) as exc:
            self._conn = None
            raise SSHError(f"The SSH connection failed ({_why(exc)})", code="unreachable") from exc
        return Result(
            exit_status=completed.exit_status if completed.exit_status is not None else 255,
            stdout=completed.stdout or "", stderr=completed.stderr or "",
        )

    async def detect_mode(self) -> str:
        """``root`` or ``sudo``. A user who is neither is refused, with the reason."""
        if self.mode is not None:
            return self.mode
        uid = await self._exec("id -u", input=None, timeout=20)
        if uid.ok and uid.stdout.strip() == "0":
            self.mode = MODE_ROOT
            return self.mode
        sudo = await self._exec("sudo -n true", input=None, timeout=20)
        if sudo.ok:
            self.mode = MODE_SUDO
            return self.mode
        raise SSHError(
            f"User '{self.user}' is not root and cannot use sudo without a password. BoW needs root, or a user "
            "with passwordless sudo (a user who is only in the docker group is not supported).",
            code="no_privileges",
        )

    async def run(self, argv: Sequence[str], *, input: Optional[Union[str, bytes]] = None,
                  timeout: float = DEFAULT_TIMEOUT, check: bool = True, as_user: bool = False) -> Result:
        """Run one command given as an argument list. ``as_user`` skips the sudo
        prefix (for the few things that belong to the SSH user itself)."""
        command = shlex.join([str(part) for part in argv])
        if not as_user and await self.detect_mode() == MODE_SUDO:
            command = "sudo -n -- " + command
        result = await self._exec(command, input=input, timeout=timeout)
        if check and not result.ok:
            label = " ".join(str(part) for part in argv[:2])
            raise SSHError(
                f"`{label}` failed on the server (exit {result.exit_status}): {result.detail()}",
                code="command_failed", detail=result.detail(2000),
            )
        return result

    async def write_file(self, path: str, content: Union[str, bytes], *, mode: str = "600") -> None:
        """Create or replace a root-owned file. The content travels on standard input."""
        await self.run(["install", "-D", "-m", mode, "/dev/stdin", path], input=content)

    async def read_file(self, path: str) -> Optional[str]:
        result = await self.run(["cat", path], check=False)
        return result.stdout if result.ok else None

    # ── first login with a one-time password ──

    async def install_public_key(self, password: str, public_key: str) -> None:
        """Log in once with a password and add BoW's public key to the user's
        ``authorized_keys``. The password is used for this one session only."""
        await self.close()
        await self.connect(password=password)
        try:
            existing = await self._exec("cat ~/.ssh/authorized_keys 2>/dev/null", input=None, timeout=20)
            body = public_key.split(" ")[1] if " " in public_key else public_key
            if body not in existing.stdout:
                added = await self._exec(
                    "umask 077; mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys", input=public_key.strip() + "\n", timeout=20,
                )
                if not added.ok:
                    raise SSHError(f"Could not install the public key: {added.detail()}", code="command_failed")
        finally:
            await self.close()
        # All later connections use the key: prove that it works now.
        await self.connect()


# Host key types asked for, in order of preference.
_HOST_KEY_ALGORITHMS = (
    ["ssh-ed25519"],
    ["ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"],
    ["rsa-sha2-512", "rsa-sha2-256"],
)


def _algorithms_for(key) -> list:
    name = key.get_algorithm() if hasattr(key, "get_algorithm") else ""
    name = name.decode() if isinstance(name, bytes) else str(name)
    for group in _HOST_KEY_ALGORITHMS:
        if name in group or (name == "ssh-rsa" and group[0].startswith("rsa-")):
            return list(group)
    return [name] if name else []


def _why(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.splitlines()[0][:160]
