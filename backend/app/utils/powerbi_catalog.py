"""Power BI catalog identity is independent of its human-readable name."""


def powerbi_identity(metadata):
    pbi = (metadata or {}).get("powerbi") if isinstance(metadata, dict) else None
    if not isinstance(pbi, dict) or not pbi.get("datasetId") or not pbi.get("tableName"):
        return None
    return (str(pbi.get("workspaceId") or ""), str(pbi["datasetId"]), str(pbi["tableName"]))


def qualified_powerbi_name(name, metadata):
    pbi = metadata["powerbi"]
    identity = powerbi_identity(metadata)
    return f"{name} [{pbi.get('workspaceName') or identity[0]}; {identity[0]}/{identity[1]}/{identity[2]}]"


def reconcile_powerbi_names(incoming, existing):
    """Match by identity, preserving legacy row IDs without adopting a namesake.

    Incoming values are payload dicts; existing values are rows/payloads with
    metadata_json. Incoming names already
    distinguish collisions in a single crawl; existing names also reserve the
    names of models visible only to a different identity.
    """

    def meta(row):
        return row.get("metadata_json") if isinstance(row, dict) else row.metadata_json

    by_identity = {
        powerbi_identity(meta(row)): name for name, row in existing.items() if powerbi_identity(meta(row)) is not None
    }
    result = {}
    renames = {}
    for name, payload in incoming.items():
        original_name = name
        identity = powerbi_identity(meta(payload))
        if identity is not None:
            old_name = by_identity.get(identity)
            if old_name is not None and (name == old_name or name.startswith(old_name + " [")):
                # Preserve the row's name until all consumers can use IDs. A
                # qualified incoming name is still safe to use on user overlays.
                name = old_name
            elif (name in existing and powerbi_identity(meta(existing[name])) != identity) or name in result:
                name = qualified_powerbi_name(name, meta(payload))
        result[name] = payload
        if identity is not None:
            renames[(identity[:2], original_name)] = name
    for payload in result.values():
        identity = powerbi_identity(meta(payload))
        if identity is None:
            continue
        for fk in payload.get("fks") or []:
            if isinstance(fk, dict):
                old = fk.get("references_name")
                fk["references_name"] = renames.get((identity[:2], old), old)
    return result
