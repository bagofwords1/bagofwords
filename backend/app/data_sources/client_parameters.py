"""Parameter handling for connections with registry-selected auth forms."""
import inspect


def merge_client_credentials(config, credentials):
    """Keep the form selector out of driver arguments; credentials may supply one."""
    params = dict(config or {})
    params.pop("auth_type", None)
    params.update(credentials or {})
    if params.get("auth_type") in (None, ""):
        params.pop("auth_type", None)
    return params


def filter_client_parameters(client_class, params, *, only=None):
    """Drop unsupported arguments, retaining forwarders and opaque signatures."""
    try:
        signature = inspect.signature(client_class.__init__)
    except (TypeError, ValueError):
        return params
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return params
    return {
        key: value for key, value in params.items()
        if (only is not None and key not in only)
        or (key in signature.parameters and key != "self")
    }
