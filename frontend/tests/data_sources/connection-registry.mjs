import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Use the same service that supplies GET /data_sources/{type}/fields. These
// are schema definitions only; no saved connection or credentials are loaded.
export function loadConnectionRegistry() {
  const backend = fileURLToPath(new URL('../../../backend/', import.meta.url));
  const result = execFileSync(`${backend}.venv/bin/python`, ['-W', 'ignore', '-c', `
import asyncio, json
from app.services.data_source_service import DataSourceService
from app.schemas.data_source_registry import list_available_data_sources
async def main():
    available = list_available_data_sources()
    fields = {}
    service = DataSourceService()
    for entry in available:
        fields[entry['type']] = await service.get_data_source_fields(None, entry['type'], None, None, auth_policy='system_only')
    print('REGISTRY_JSON=' + json.dumps({'available': available, 'fields': fields}))
asyncio.run(main())
`], { cwd: backend, encoding: 'utf8', env: { ...process.env, TESTING: 'true' }, maxBuffer: 20 * 1024 * 1024 });
  return JSON.parse(result.split('REGISTRY_JSON=')[1]);
}
