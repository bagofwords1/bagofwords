"""`python main.py --config <file>` must be the config the process runs with.

`main.py` copies `--config` into `BOW_CONFIG_PATH` before the settings
singleton is built. The TLS policy hook has to run before the first app import
(aiohttp caches its SSL context at import), which puts a settings import near
the top of the module; if it lands above the argparse block, `--config` is
silently ignored for the whole config, not just the TLS flag (caught in review
of #1453). This boots the module in a subprocess with `--config` on argv and
checks that a value only the custom file sets is what the process sees, and
that the TLS policy was applied from it.
"""
import os
import subprocess
import sys
import textwrap

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.timeout(180)
def test_config_flag_wins_over_default_file(tmp_path, monkeypatch):
    custom = tmp_path / "custom.yaml"
    custom.write_text("security:\n  tls_strict_x509: false\n")
    env = dict(os.environ)
    env.pop("BOW_CONFIG_PATH", None)
    env.pop("BOW_TLS_STRICT_X509", None)
    script = textwrap.dedent(f"""
        import sys, ssl
        sys.argv = ["main.py", "--config", {str(custom)!r}]
        import main
        from app.settings.config import settings
        print("STRICT_CFG", settings.bow_config.security.tls_strict_x509)
        print("STRICT_SSL", bool(ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT))
    """)
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=170,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = dict(line.split(" ", 1) for line in proc.stdout.splitlines() if line.startswith("STRICT_"))
    assert out["STRICT_CFG"] == "False", "the --config file was not the one loaded"
    assert out["STRICT_SSL"] == "False", "TLS policy was not applied from the --config file"
