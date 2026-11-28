"""The sandbox driver contract, run against a real Docker host.

Skipped unless a test server is named. Run it from the live feedback loop:

    BOW_SANDBOX_TEST_HOST=<address> \\
    BOW_SANDBOX_TEST_USER=ubuntu \\
    BOW_SANDBOX_TEST_KEY=/path/to/private-key \\
    BOW_SANDBOX_TEST_FINGERPRINT=SHA256:... \\
    uv run pytest tests/contracts/sandbox_driver/test_docker_ssh_live.py

Optional: ``BOW_SANDBOX_TEST_BOW_ADDRESS`` (restrict the sandbox port to this
address), ``BOW_SANDBOX_TEST_PRELOADED=1`` (images are already on the server),
``BOW_SANDBOX_TEST_IMAGE`` (the sandbox image; needs ``python``), and the
``BOW_SANDBOX_*_IMAGE`` overrides for unpublished support images.

It bootstraps the server as its own BoW instance, so run it on a server no
other instance is using. Sandboxes get a 1 GB memory limit and there are
never more than two at once.
"""
import pytest

from tests.contracts.sandbox_driver.contract import *  # noqa: F401,F403 - the contract tests
from tests.contracts.sandbox_driver.live_support import LIVE, harness  # noqa: F401 - the fixture

pytestmark = LIVE
