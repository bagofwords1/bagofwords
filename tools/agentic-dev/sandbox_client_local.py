import os
from pathlib import Path

import requests

REST = f"http://localhost:8080/v1"

# Upload code over REST
requests.put(
    f"{REST}/files/main.py",
    data=Path("/Users/dkartsev/code/bagofwords1/bagofwords/tools/agentic-dev/k8s-agent-sandbox-infra/install.sh").read_bytes(),
    headers={"Content-Type": "application/octet-stream"},
).raise_for_status()
