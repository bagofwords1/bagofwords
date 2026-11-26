"""Names of the ENVIRONMENT values that mean local development.

Kept apart from `config.py` so modules that must not load the settings (the
code sandbox reads ENVIRONMENT from the process environment) can share the
same list. Compare against the lower-cased, stripped value.
"""

DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "dev", "test", "testing"})
