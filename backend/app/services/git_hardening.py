"""Shared safety settings for git commands the app runs against remote repos.

Two layers:

* Credentials (used by every git network call):
  - HTTPS tokens travel as a per-command ``http.extraHeader`` set through the
    environment (``GIT_CONFIG_*``), so they never land in a clone's
    ``.git/config`` the way a ``https://user:token@host`` remote URL does.
  - SSH verifies the server's host key: GitHub, GitLab and Bitbucket keys are
    pinned (from their official docs), other hosts are trusted on first use
    and then pinned (``accept-new``) — never ``StrictHostKeyChecking=no``.

* Untrusted-content hardening (``hardened_args`` / ``hardened_env``), for
  long-lived read-only clones of repos we don't control: no system/global git
  config, no hooks, no filter/LFS drivers, no fsmonitor, no submodules, no
  symlinks, no exec bits, https-only transport, no redirects, no prompts.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Official host keys (known_hosts format), copied 2026-10 from:
#   GitHub    https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
#   GitLab    https://docs.gitlab.com/user/gitlab_com/
#   Bitbucket https://bitbucket.org/site/ssh
PINNED_KNOWN_HOSTS = """\
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
gitlab.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAfuCHKVTjquxvt6CM6tdG4SLp1Btn/nOeHHE5UOzRdf
gitlab.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBFSMqzJeV9rUzU4kWitGjeR4PWSa29SPqJ1fVkhtj3Hw9xjLVXVYrU9QlYWrOLXBpQ6KWjbjTDTdDkoohFzgbEY=
gitlab.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCsj2bNKTBSpIYDEGk9KxsGh3mySTRgMtXL583qmBpzeQ+jqCMRgBqB98u3z++J1sKlXHWfM9dyhSevkMwSbhoR8XIq/U0tCNyokEi/ueaBMCvbcTHhO7FcwzY92WK4Yt0aGROY5qX2UKSeOvuP4D6TPqKF1onrSzH9bx9XUf2lEdWT/ia1NEKjunUqu1xOB/StKDHMoX4/OKyIzuS0q/T1zOATthvasJFoPrAjkohTyaDUz2LN5JoH839hViyEG82yB+MjcFV5MU3N1l1QL3cVUCh93xSaua1N85qivl+siMkPGbO5xR/En4iEY6K2XPASUEMaieWVNTRCtJ4S8H+9
bitbucket.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIazEu89wgQZ4bqs3d63QSMzYVa0MuJ2e2gKTKqu+UUO
bitbucket.org ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBPIQmuzMBuKdWeF4+a2sjSSpBK0iqitSQ+5BM9KhpexuGt20JpTVM7u5BDZngncgrqDMbWdxMWWOGtZ9UgbqgZE=
bitbucket.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDQeJzhupRu0u0cdegZIa8e86EG2qOCsIsD1Xw0xSeiPDlCr7kq97NLmMbpKTX6Esc30NuoqEEHCuc7yWtwp8dI76EEEB1VqY9QJq6vk+aySyboD5QF61I/1WeTwu+deCbgKMGbUijeXhtfbxSxm6JwGrXrhBdofTsbKRUsrN1WoNgUa8uqN1Vx6WAJw1JHPhglEGGHea6QICwJOAr/6mrui/oB7pkaWKHj3z7d1IC4KWLtY47elvjbaTlkN04Kc/5LFEirorGYVbt15kAUlqGM65pk6ZBxtaO3+30LVlORZkxOh+LKL/BvbZ/iRNhItLqNyieoQj/uh/7Iv4uyH/cV/0b4WDSd3DptigWq84lJubb9t/DnZlrJazxyDCulTmKdOR7vs9gMTo+uoIrPSb8ScTtvw65+odKAlBj59dhnVp9zd7QUojOpXlL62Aw56U4oO+FALuevvMjiWeavKhJqlR7i5n9srYcrNV7ttmDw7kf/97P5zauIhxcjX+xHv4M=
"""

# Where host keys learned on first use (self-hosted servers) are kept.
_STATE_DIR = Path(os.environ.get("BOW_GIT_STATE_DIR", "uploads/git_state"))


def _known_hosts_files() -> Tuple[Path, Path]:
    """(learned, pinned) known_hosts files, created on first use, 0600."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_STATE_DIR, 0o700)
    except OSError:
        pass
    pinned = _STATE_DIR / "known_hosts.pinned"
    if not pinned.exists() or pinned.read_text() != PINNED_KNOWN_HOSTS:
        pinned.write_text(PINNED_KNOWN_HOSTS)
        os.chmod(pinned, 0o600)
    learned = _STATE_DIR / "known_hosts.learned"
    if not learned.exists():
        learned.touch(mode=0o600)
    return learned, pinned


def ssh_command(key_path: str) -> str:
    """GIT_SSH_COMMAND that verifies the server's host key.

    Pinned keys reject an impostor for the big three hosts; any other host is
    learned on first contact (``accept-new``) and must keep that key after.
    ssh appends new keys to the FIRST UserKnownHostsFile (the learned one).
    """
    learned, pinned = _known_hosts_files()
    return (
        f"ssh -i {key_path} -o IdentitiesOnly=yes -o BatchMode=yes "
        f"-o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile='{learned} {pinned}' -o GlobalKnownHostsFile=/dev/null"
    )


def _append_config(env: Dict[str, str], key: str, value: str) -> None:
    """Add one ``-c key=value`` through the environment (never on disk)."""
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    env[f"GIT_CONFIG_KEY_{n}"] = key
    env[f"GIT_CONFIG_VALUE_{n}"] = value
    env["GIT_CONFIG_COUNT"] = str(n + 1)


def basic_auth_header(username: str, secret: str) -> str:
    pair = f"{username}:{secret}".encode("utf-8")
    return "Authorization: Basic " + base64.b64encode(pair).decode("ascii")


def with_https_auth(env: Dict[str, str], username: str, secret: str) -> Dict[str, str]:
    """Return ``env`` plus an Authorization header for git's HTTPS requests.

    Equivalent to ``https://username:secret@host`` (what providers expect for
    token auth) without writing the secret into the remote URL.
    """
    out = dict(env)
    _append_config(out, "http.extraHeader", basic_auth_header(username, secret))
    return out


# --- untrusted-content hardening (read-only clones) ---------------------------

HARDENED_CONFIG: List[Tuple[str, str]] = [
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("core.attributesFile", os.devnull),
    ("core.symlinks", "false"),
    ("core.fileMode", "false"),
    ("core.protectNTFS", "true"),
    ("core.protectHFS", "true"),
    ("init.templateDir", ""),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("http.followRedirects", "false"),
    ("filter.lfs.smudge", ""),
    ("filter.lfs.process", ""),
    ("filter.lfs.required", "false"),
    ("credential.helper", ""),
    ("advice.detachedHead", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
]


def hardened_args() -> List[str]:
    args: List[str] = []
    for key, value in HARDENED_CONFIG:
        args += ["-c", f"{key}={value}"]
    return args


# Environment variables passed through to git. Everything else (HOME-based
# config, GIT_* overrides from the app's own environment) is dropped.
_PASSTHROUGH = (
    "PATH", "LANG", "LC_ALL", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "GIT_SSL_CAINFO", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
)


def hardened_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    src = os.environ if base is None else base
    env = {k: src[k] for k in _PASSTHROUGH if k in src}
    # Keep any host pin (http.curloptResolve) the caller already added.
    for k, v in src.items():
        if k == "GIT_CONFIG_COUNT" or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env[k] = v
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "HOME": os.devnull,
    })
    return env
