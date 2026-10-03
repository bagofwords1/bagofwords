"""Bounded historical artifact facts; never a substitute for current authorization."""
ARTIFACT_DIGEST_TOOLS = {'create_artifact', 'edit_artifact', 'read_artifact', 'manage_artifact_resources'}


def digest_artifact(tool_execution):
    raw = tool_execution.result_json or {}
    data = {**raw, **(raw.get('observation') or {})}
    args = getattr(tool_execution, 'arguments_json', None) or {}
    parts = []
    for key in ('title', 'mode', 'artifact_id', 'resource_artifact_id', 'version'):
        if data.get(key) is not None:
            parts.append(f'{key}: {str(data[key])[:120]}')
    viz_ids = data.get('visualization_ids') or (data.get('artifact_preview') or {}).get('visualization_ids') or []
    if viz_ids:
        parts.append('viz_ids: ' + ', '.join(map(str, viz_ids)))
    if data.get('diff_applied') is not None:
        parts.append('diff' if data['diff_applied'] else 'rewrite')
    if data.get('resources_enabled') is False:
        parts.append('resources disabled at that time')
    if tool_execution.tool_name == 'manage_artifact_resources':
        for key in ('action', 'name', 'kind', 'id', 'revision', 'committed'):
            value = data.get(key)
            if value is None and key == 'action':
                value = args.get('action')
            if value is None and key == 'name':
                value = args.get('resource') or (args.get('definition') or {}).get('name')
            if value is None and key in ('id', 'revision'):
                resource = data.get('resource')
                value = resource.get(key) if isinstance(resource, dict) else None
            if value is not None:
                parts.append(f'{key}: {value}')
        if not data.get('resource_artifact_id') and args.get('artifact_id'):
            parts.append(f"resource_artifact_id: {args['artifact_id']}")
        if data.get('changed_sections'):
            parts.append('changed: ' + ', '.join(data['changed_sections']))
        definition = data.get('definition') or {}
        if definition.get('fields'):
            fields = list(definition['fields'].items())
            parts.append('fields: ' + ', '.join(f"{k}:{v.get('type')}" for k, v in fields[:12]) + (' …' if len(fields)>12 else ''))
        if definition.get('permissions'):
            import json
            parts.append('historical permissions: ' + json.dumps(definition['permissions'], separators=(',', ':'))[:700])
    resources = data.get('resources') or []
    if resources:
        parts.append('resources: ' + ', '.join(f"{v.get('name')} ({v.get('kind')}, revision {v.get('revision')})" for v in resources[:10]))
        if len(resources)>10:
            parts.append(f'{len(resources)-10} more resources; read artifact for definitions')
    if data.get('success') is False or data.get('error') or getattr(tool_execution, 'status', None) == 'error':
        parts.append('FAILED: ' + str(data.get('error') or getattr(tool_execution, 'error_message', None) or 'Change rejected')[:250])
    if data.get('resource_artifact_id') or tool_execution.tool_name == 'manage_artifact_resources':
        parts.append('Re-read current definitions before schema/permission changes')
    return '; '.join(parts)
