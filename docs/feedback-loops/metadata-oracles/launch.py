"""Sandbox launcher: uvicorn on a non-default port (a sibling stack may hold :8000).

    cd backend && TESTING=true ENVIRONMENT=production \
      TEST_DATABASE_URL=sqlite:///<db> .venv/bin/python ../docs/feedback-loops/metadata-oracles/launch.py 8047
"""
import sys

import uvicorn

sys.path.insert(0, ".")
import main  # noqa: E402
from app.settings.config import settings  # noqa: E402

# Same relaxation as tests/e2e/rbac/conftest.py: a second org is needed for
# the cross-org probe.
settings.bow_config.features.allow_multiple_organizations = True
settings.bow_config.features.allow_uninvited_signups = True

uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8047)
