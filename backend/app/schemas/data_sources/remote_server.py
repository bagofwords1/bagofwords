"""Registry-driven remote server forms. Credentials never contain local file paths."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OPERATION_UI = {
    "ui:type": "checkboxes",
    "ui:enumLabelKeys": {
        name: f"data.remoteOperations.{name}"
        for name in ("list_directory", "read_file", "search_file", "system_status")
    },
}


class ShellRule(BaseModel):
    pattern: str = Field(min_length=1, max_length=16384)
    action: Literal["allow", "ask", "deny"]
    match: Literal["exact", "glob"] = "exact"
    working_directory: str | None = Field(None, max_length=1024)


class RemoteLimits(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    allowed_paths: list[str] = Field(
        default_factory=list,
        title="Allowed log/file roots",
        description="Folders or wildcard patterns, e.g. /var/log/*.log or SERVERLOGS/*.log. * allows all paths this account can read; empty denies file access.",
    )
    allowed_operations: list[Literal["list_directory", "read_file", "search_file", "system_status"]] = Field(
        default=["list_directory", "read_file", "search_file"],
        title="Inspection operations",
        json_schema_extra=OPERATION_UI,
    )
    connection_idle_seconds: int = Field(120, ge=10, le=900, title="Connection idle timeout (seconds)")
    timeout_seconds: int = Field(30, ge=1, le=120, title="Timeout (seconds)")
    max_output_bytes: int = Field(65536, ge=1024, le=1048576, title="Maximum output bytes")
    max_lines: int = Field(200, ge=1, le=2000, title="Maximum lines or entries")
    host: str = Field(..., title="Server hostname", min_length=1)

    @field_validator("host")
    @classmethod
    def host_only(cls, value):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", value):
            raise ValueError("Use a hostname or IP address, without a URL, username, or options")
        return value


class RemoteSSHConfig(RemoteLimits):
    inspection_mode: Literal["agentless", "restricted_reader"] = Field(
        "agentless",
        title="Inspection mode",
        description="Agentless runs in memory using existing Python 3 on Linux or PowerShell on Windows. Restricted reader requires an administrator-installed helper.",
        json_schema_extra={
            "ui:enumLabels": {
                "agentless": "Agentless (no installation)",
                "restricted_reader": "Installed restricted reader",
            }
        },
    )
    port: int = Field(22, ge=1, le=65535)
    shell_policy: Literal["policy", "strict", "allow", "deny"] = Field(
        "policy",
        title="Shell permissions",
        description="Always allow applies to all authorized users. Policy controlled uses saved personal choices and command rules; unmatched commands ask.",
        json_schema_extra={
            "ui:enumLabels": {
                "policy": "Policy controlled",
                "strict": "Require approval every time",
                "allow": "Always allow for everyone",
                "deny": "Always deny",
            }
        },
    )
    shell_rules: list[dict] = Field(
        default_factory=list,
        title="Shell command rules",
        description='JSON rules: [{"pattern":"docker ps*","action":"allow","match":"glob"}]. Actions: allow, ask, deny. Patterns are not a sandbox.',
    )

    @field_validator("shell_rules")
    @classmethod
    def validate_rules(cls, values):
        if len(values) > 200:
            raise ValueError("At most 200 command rules")
        return [ShellRule.model_validate(v).model_dump() for v in values]

    allowed_operations: list[Literal["list_directory", "read_file", "search_file", "system_status"]] = Field(
        default=["list_directory", "read_file", "search_file", "system_status"],
        title="Inspection operations",
        json_schema_extra=OPERATION_UI,
    )
    operating_system: Literal["linux", "windows"] = Field("linux", title="Operating system")
    linux_shell: Literal["bash", "sh"] = Field("bash", title="Linux command shell")
    host_public_key: str = Field(
        ...,
        title="Verified SSH host public key",
        description="Trusted server key in 'ssh-ed25519 AAAA…' or 'ssh-rsa AAAA…' format. Obtain through an independent trusted channel.",
        json_schema_extra={"ui:type": "textarea"},
    )
    reader_path: str = Field(
        "",
        title="Installed reader path (optional)",
        description="Default: /usr/local/lib/bow/reader.py on Linux; C:\\ProgramData\\BOW\\reader.ps1 on Windows. Install the administrator-owned reader first.",
    )
    enable_shell: bool = Field(
        True,
        title="Enable maintenance shell",
        description="Arbitrary Bash/PowerShell with this account's privileges. Execution follows the shell permissions and saved user choices.",
    )

    @field_validator("host_public_key")
    @classmethod
    def public_key(cls, value):
        import base64

        parts = value.strip().split()
        if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-")):
            raise ValueError("Supply a server public key, not a fingerprint or private key")
        base64.b64decode(parts[1], validate=True)
        return " ".join(parts[:2])

    @field_validator("reader_path")
    @classmethod
    def fixed_reader(cls, value):
        if not value:
            return value
        if not re.fullmatch(r"[A-Za-z0-9_./:\\ -]+", value) or ".." in value:
            raise ValueError("Reader path must be an absolute path without shell syntax")
        if not (value.startswith("/") or re.match(r"^[A-Za-z]:\\", value)):
            raise ValueError("Reader path must be absolute")
        return value


class RemoteCredentials(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)


class SSHPasswordCredentials(RemoteCredentials):
    username: str = Field(..., min_length=1, title="SSH username")
    password: str = Field(..., min_length=1, title="Password", json_schema_extra={"ui:type": "password"})


class SSHKeyCredentials(RemoteCredentials):
    username: str = Field(..., min_length=1, title="SSH username")
    private_key: str = Field(..., min_length=1, title="SSH private key", json_schema_extra={"ui:type": "textarea"})
    passphrase: str = Field("", title="Key passphrase", json_schema_extra={"ui:type": "password"})
    certificate: str = Field("", title="Optional SSH user certificate", json_schema_extra={"ui:type": "textarea"})


class RemoteKerberosCredentials(RemoteCredentials):
    username: str = Field(..., min_length=1, title="Remote username")
    principal: str = Field(
        ...,
        min_length=1,
        title="Kerberos principal",
        description="Service account principal, for example svc_bow@EXAMPLE.COM",
    )
    keytab_base64: str = Field(
        ...,
        min_length=1,
        title="Base64 keytab",
        description="Dedicated service-account keytab. Requires the deployment's krb5.conf, DNS, clock and KDC access.",
        json_schema_extra={"ui:type": "password"},
    )


class RemoteSMBConfig(RemoteLimits):
    allowed_operations: list[Literal["list_directory", "read_file", "search_file"]] = Field(
        default=["list_directory", "read_file", "search_file"],
        title="Inspection operations",
        json_schema_extra=OPERATION_UI,
    )
    port: int = Field(445, ge=1, le=65535)
    share: str = Field(
        ...,
        min_length=1,
        title="Read-only share",
        description="Dedicated log share, such as BOWLogs. Avoid administrative drive shares.",
    )
    require_encryption: bool = Field(True, title="Require SMB encryption")

    @field_validator("share")
    @classmethod
    def share_name(cls, value):
        if any(c in value for c in "/\\:\x00") or value in (".", ".."):
            raise ValueError("Supply one share name, not a path")
        return value

    @field_validator("allowed_operations")
    @classmethod
    def file_operations(cls, value):
        if "system_status" in value:
            raise ValueError("SMB supports file operations only")
        return value


class SMBPasswordCredentials(RemoteCredentials):
    username: str = Field(..., min_length=1, title="Account", description="DOMAIN\\user or SERVER\\localuser")
    password: str = Field(..., min_length=1, title="Password", json_schema_extra={"ui:type": "password"})
