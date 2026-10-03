"""Stand-in for a connection whose client could not be built for this user.

DataSourceService.construct_clients(connection_errors=[...]) builds each
connection of a data source independently; a connection that fails (e.g. the
viewer has no credential for it) is replaced by this placeholder instead of
taking its sibling connections down with it.

The placeholder keeps the client key present, so step code sees the same keys
the report owner does and code that touches every client can never silently
compute on a partial set. It fails only when actually queried, with the
connection's own error.
"""


class ConnectionUnavailableError(Exception):
    """Raised when step code queries a connection that could not be built."""


class UnavailableConnectionClient:
    def __init__(self, error: Exception):
        self._bow_unavailable_error = error

    def _raise_unavailable(self, *args, **kwargs):
        error = self._bow_unavailable_error
        raise ConnectionUnavailableError(str(getattr(error, "detail", None) or error))

    execute_query = _raise_unavailable
    query = _raise_unavailable
