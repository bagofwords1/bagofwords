"""Symmetric encryption for small secrets stored in JSON config.

Org SMTP credentials live in ``OrganizationSettings.config.smtp`` (plain JSON),
so the password is Fernet-encrypted with the same key ``ExternalPlatform`` uses
(``settings.bow_config.encryption_key``) and stored as ``password_enc`` — never
in plaintext.
"""
from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet

from app.settings.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.bow_config.encryption_key)


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001 — corrupt/rotated key → treat as unset
        return None
