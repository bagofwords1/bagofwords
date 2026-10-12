"""Write a sandbox bow-config: the repo's bow-config.yaml + SMTP pointed at the
local sink (smtp_sink.py). Nothing else changes.

    python make_config.py <repo_root>/bow-config.yaml <out.yaml> <smtp_port>

Then start the backend with BOW_CONFIG_PATH=<out.yaml>.
"""
import sys

import yaml

src, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = yaml.safe_load(open(src)) or {}
cfg["smtp_settings"] = {
    "host": "127.0.0.1", "port": port, "from_name": "BOW sandbox",
    "from_email": "noreply@bow-sandbox.com", "use_tls": False, "use_ssl": False,
    "validate_certs": False, "use_credentials": False,
}
with open(dst, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
print(dst)
