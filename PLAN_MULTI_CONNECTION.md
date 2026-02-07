# Multi-Connection Per Domain Implementation Plan

## Decisions Made

| Decision | Choice |
|----------|--------|
| `ds_clients` key pattern | `{domain_name}:{connection_name}` |
| API response structure | Breaking: `connections[]` array only (remove singular `connection`) |
| Schema context format | Nested: `<connection>` tags inside `<data_source>` |
| Frontend UI | Connection filter in TablesSelector |

---

## Phase 1: Core Data Models & Schemas

### 1.1 Extend `Table` class with connection fields

**File**: `backend/app/ai/prompt_formatters.py` (lines 161-188)

```python
class Table(BaseModel):
    # ... existing fields ...
    connection_id: Optional[str] = None      # NEW
    connection_name: Optional[str] = None    # NEW
    connection_type: Optional[str] = None    # NEW (e.g., "snowflake", "postgres")
```

### 1.2 Update API Schemas

**File**: `backend/app/schemas/data_source_schema.py`

| Schema | Change |
|--------|--------|
| `DataSourceSchema` | Remove `connection`, add `connections: List[ConnectionEmbedded]` |
| `DataSourceReportSchema` | Same |
| `DataSourceListItemSchema` | Same |
| `DataSourceMinimalEmbeddedSchema` | Same |
| `DataSourceSummarySchema` | Remove `type` field (no single type for domain) |

### 1.3 Update Mention/Context Schemas

**Files**:
- `backend/app/ai/context/sections/mentions_section.py` - Add `connection_id/name/type` to `TableMentionItem`
- `backend/app/ai/tools/schemas/describe_tables.py` - Add connection to `TablePreview`
- `backend/app/schemas/mention_schema.py` - Update table response schema

---

## Phase 2: Schema Context Builder

### 2.1 Load connection relationships

**File**: `backend/app/ai/context/builders/schema_context_builder.py`

**Line 74-77**: Update query to load connection info:
```python
ds_tables_result = await self.db.execute(
    select(DataSourceTable)
    .options(
        selectinload(DataSourceTable.connection_table)
        .selectinload(ConnectionTable.connection)
    )
    .where(DataSourceTable.datasource_id == str(ds.id))
)
```

**Lines 140-154**: Extract connection info in normalized output:
```python
normalized.append({
    # ... existing fields ...
    "connection_id": str(t.connection_table.connection_id) if t.connection_table else None,
    "connection_name": t.connection_table.connection.name if t.connection_table and t.connection_table.connection else None,
    "connection_type": t.connection_table.connection.type if t.connection_table and t.connection_table.connection else None,
})
```

**Lines 199-212**: Set connection fields on `PromptTable`:
```python
tbl = PromptTable(
    # ... existing fields ...
    connection_id=item.get("connection_id"),
    connection_name=item.get("connection_name"),
    connection_type=item.get("connection_type"),
)
```

**Lines 308-320**: Restructure output to group by connection (see Phase 3).

---

## Phase 3: XML Rendering (Nested Connections)

### 3.1 Update TablesSchemaContext structure

**File**: `backend/app/ai/context/sections/tables_schema_section.py`

Change from:
```xml
<data_source name="Sales" type="snowflake" id="ds-123">
  <table name="orders">...</table>
  <table name="customers">...</table>
</data_source>
```

To:
```xml
<data_source name="Sales" id="ds-123">
  <description>Combined sales data</description>
  <connection name="snowflake_prod" type="snowflake" id="conn-1">
    <table name="orders">...</table>
    <table name="inventory">...</table>
  </connection>
  <connection name="crm_db" type="postgres" id="conn-2">
    <table name="customers">...</table>
    <table name="contacts">...</table>
  </connection>
</data_source>
```

**Changes needed**:
1. Add `Connection` inner class to `TablesSchemaContext.DataSource`
2. Group tables by connection in render methods
3. Update `_render_gist()`, `_render_names()`, `_render_digest()`, `render_combined()`

### 3.2 Update usage snapshot

**Lines 271-332**: Update `SchemaUsageSnapshot` and `DataSourceUsage` to track per-connection.

---

## Phase 4: Client Construction

### 4.1 Update `construct_client()` to `construct_clients()`

**File**: `backend/app/services/data_source_service.py` (line 1179-1202)

```python
async def construct_clients(self, db: AsyncSession, data_source: DataSource, current_user: User | None) -> Dict[str, Any]:
    """Construct clients for ALL connections in the domain.

    Returns:
        Dict keyed by "{domain_name}:{connection_name}" -> client
    """
    if not data_source.connections:
        raise HTTPException(status_code=400, detail="Data source has no associated connections")

    clients = {}
    for conn in data_source.connections:
        key = f"{data_source.name}:{conn.name}"
        ClientClass = resolve_client_class(conn.type)
        config = json.loads(conn.config) if isinstance(conn.config, str) else (conn.config or {})
        creds = await self.resolve_credentials_for_connection(db=db, connection=conn, current_user=current_user)
        params = {**(config or {}), **(creds or {})}
        # ... rest of client construction logic ...
        clients[key] = ClientClass(**allowed)

    return clients
```

### 4.2 Add connection-specific credential resolution

```python
async def resolve_credentials_for_connection(self, db: AsyncSession, connection: Connection, current_user: User | None) -> dict:
    """Resolve credentials for a specific connection."""
    # ... credential resolution logic moved from resolve_credentials ...
```

---

## Phase 5: Update All `ds_clients` Construction Sites (10 places)

### 5.1 Services using `ds.get_client()`

| File | Line | Change |
|------|------|--------|
| `step_service.py` | 123 | Use `construct_clients()` |
| `query_service.py` | 211 | Use `construct_clients()` |
| `query_service.py` | 324 | Use `construct_clients()` |
| `entity_service.py` | 410 | Use `construct_clients()` |
| `entity_service.py` | 466 | Use `construct_clients()` |

**Pattern change**:
```python
# Before
db_clients = {ds.name: ds.get_client() for ds in report.data_sources}

# After
db_clients = {}
for ds in report.data_sources:
    ds_clients = await data_source_service.construct_clients(db, ds, current_user)
    db_clients.update(ds_clients)
```

### 5.2 Completion service

**File**: `backend/app/services/completion_service.py` (line 274-276)

```python
# Before
clients = {}
for data_source in report.data_sources:
    clients[data_source.name] = await self.data_source_service.construct_client(db, data_source, current_user)

# After
clients = {}
for data_source in report.data_sources:
    ds_clients = await self.data_source_service.construct_clients(db, data_source, current_user)
    clients.update(ds_clients)
```

### 5.3 MCP context

**File**: `backend/app/ai/tools/mcp/context.py` (line 97)

Same pattern as above.

### 5.4 Entity describe tool

**File**: `backend/app/ai/tools/implementations/describe_entity.py` (line 304)

Same pattern.

---

## Phase 6: Update Coder Prompts

### 6.1 Update all `ds_clients` documentation

**File**: `backend/app/ai/agents/coder/coder.py`

**Lines to update**: 152-155, 220, 255-275, 466-470, 595, 690, 725-745, 824

**Change documentation from**:
```
- Use `ds_clients[data_source_name].execute_query("SOME QUERY")` to query data sources.
```

**To**:
```
- Use `ds_clients["domain_name:connection_name"].execute_query("SOME QUERY")` to query data sources.
- The key format is "{domain_name}:{connection_name}" where:
  - domain_name: The name of the data source/domain
  - connection_name: The name of the specific database connection
- Example: `ds_clients["Sales Analytics:snowflake_prod"].execute_query("SELECT * FROM orders")`
```

### 6.2 Update data source descriptions in prompts

**Lines 155-158**:
```python
# Before
data_source_descriptions.append(
    f"data_source_name: {data_source_name}\ndescription: {client.description}"
)

# After
# Parse key to extract domain and connection
domain_name, connection_name = client_key.rsplit(":", 1)
data_source_descriptions.append(
    f"client_key: {client_key}\n"
    f"domain: {domain_name}\n"
    f"connection: {connection_name}\n"
    f"description: {client.description}"
)
```

---

## Phase 7: Remove All `connections[0]` Usages (21 places)

### 7.1 Data Source Model

**File**: `backend/app/models/data_source.py`

| Line | Method | Change |
|------|--------|--------|
| 115 | `get_client()` | Deprecate or remove - callers should use `construct_clients()` |
| 123 | `get_credentials()` | Deprecate or remove |

### 7.2 Data Source Service (14 usages)

**File**: `backend/app/services/data_source_service.py`

| Line | Context | Change |
|------|---------|--------|
| 71 | `_build_connection_embedded()` | Build list of `ConnectionEmbedded` |
| 340 | `create_data_source()` | Return all connections in response |
| 557 | `get_data_source()` | Return all connections |
| 606 | `get_active_data_sources()` | Return all connections per item |
| 682 | `get_data_sources()` | Return all connections per item |
| 760 | `get_available_data_sources()` | Return all connections per item |
| 813 | `test_data_source_connection()` | Test all connections or accept connection_id param |
| 996 | `test_new_data_source_connection()` | Keep as-is (testing new single connection) |
| 1019 | Response building | Return full connection info |
| 1124 | `test_data_source_connection()` | Iterate all connections or accept param |
| 1275 | `update_data_source()` | Update specific connection or all |
| 1290 | Refresh schema | Refresh all connections (already done in some paths) |
| 1375 | Auth policy | Combine policies or per-connection |

### 7.3 Other Services

| File | Line | Change |
|------|------|--------|
| `user_data_source_credentials_service.py` | 31 | Check credentials for all connections |
| `demo_data_source_service.py` | 381 | Handle demo with multiple connections |
| `report_service.py` | 722 | Return all connections in report response |
| `schema_context_builder.py` | 313-314 | Already addressed in Phase 2 |

---

## Phase 8: Mentions API

### 8.1 Update table response in mentions

**File**: `backend/app/services/mention_service.py` (lines 224-233)

```python
# Before
tables.append({
    'id': str(table.id),
    'type': 'datasource_table',
    'name': table.name,
    'datasource_id': str(ds.id),
    'data_source_name': ds.name,
    'data_source_type': ds.type,
    ...
})

# After
tables.append({
    'id': str(table.id),
    'type': 'datasource_table',
    'name': table.name,
    'datasource_id': str(ds.id),
    'data_source_name': ds.name,
    'connection_id': str(table.connection_table.connection_id) if table.connection_table else None,
    'connection_name': table.connection_table.connection.name if table.connection_table else None,
    'connection_type': table.connection_table.connection.type if table.connection_table else None,
    ...
})
```

---

## Phase 9: Frontend Changes

### 9.1 TypeScript Types

**File**: `frontend/composables/useDomain.ts` (lines 7-18)

```typescript
interface Domain {
  id: string
  name: string
  description?: string
  connections: Array<{  // Changed from single connection?
    id: string
    name: string
    type: string
    table_count?: number
    auth_policy?: string
  }>
}
```

### 9.2 TablesSelector - Add Connection Filter

**File**: `frontend/components/datasources/TablesSelector.vue`

Add new filter section (after Schema filter, lines 94-122):

```vue
<!-- Connection filter section -->
<div class="py-1 border-b border-gray-100">
  <div class="px-2 py-1 text-[10px] font-medium text-gray-400 uppercase tracking-wider flex items-center justify-between">
    <span>Connection</span>
    <button v-if="selectedConnections.length > 0" type="button" @click.stop="clearConnectionFilter" class="text-[9px] text-gray-400 hover:text-gray-600">
      Clear
    </button>
  </div>
  <div v-if="availableConnections.length === 0" class="px-2 py-1 text-xs text-gray-400">No connections</div>
  <div v-else class="max-h-32 overflow-y-auto">
    <label v-for="conn in availableConnections" :key="conn.id" class="flex items-center px-2 py-1 text-xs hover:bg-gray-50 cursor-pointer">
      <input type="checkbox" :checked="selectedConnections.includes(conn.id)" @change="toggleConnectionFilter(conn.id)" class="mr-1.5 h-3 w-3 rounded border-gray-300" />
      <DataSourceIcon :type="conn.type" class="w-3 h-3 mr-1" />
      <span class="truncate">{{ conn.name }}</span>
    </label>
  </div>
</div>
```

Add state:
```typescript
const selectedConnections = ref<string[]>([])
const availableConnections = ref<Array<{id: string, name: string, type: string}>>([])
```

### 9.3 Update DomainSelector

**File**: `frontend/components/DomainSelector.vue` (lines 132-137)

Change from single connection display to connection count or list:

```vue
<!-- Before -->
<DataSourceIcon v-if="hoveredDomainDetails?.connection?.type" :type="hoveredDomainDetails.connection.type" />
<span>{{ hoveredDomainDetails?.connection?.name || 'No connection' }}</span>

<!-- After -->
<div v-if="hoveredDomainDetails?.connections?.length" class="flex items-center gap-2">
  <div class="flex -space-x-1">
    <DataSourceIcon v-for="conn in hoveredDomainDetails.connections.slice(0, 3)" :key="conn.id" :type="conn.type" class="w-4 h-4 ring-1 ring-white rounded" />
  </div>
  <span class="text-xs text-gray-500">
    {{ hoveredDomainDetails.connections.length }} connection{{ hoveredDomainDetails.connections.length > 1 ? 's' : '' }}
  </span>
</div>
```

### 9.4 Update connection.vue pages

**Files**:
- `frontend/pages/data/[id]/connection.vue`
- `frontend/pages/integrations/[id]/connection.vue`

Change from single connection view to connection list management:

```vue
<!-- List of connections with add/remove -->
<div v-for="conn in integration?.connections" :key="conn.id" class="border rounded-lg p-4 mb-3">
  <div class="flex items-center justify-between">
    <div class="flex items-center gap-3">
      <DataSourceIcon :type="conn.type" class="h-6" />
      <div>
        <div class="font-semibold">{{ conn.name }}</div>
        <div class="text-xs text-gray-500">{{ conn.type }}</div>
      </div>
    </div>
    <div class="flex items-center gap-2">
      <ConnectionStatusBadge :status="conn.user_status?.connection" />
      <UButton size="xs" @click="testConnection(conn)">Test</UButton>
      <UButton size="xs" color="gray" @click="editConnection(conn)">Edit</UButton>
      <UButton size="xs" color="red" variant="ghost" @click="removeConnection(conn)">Remove</UButton>
    </div>
  </div>
</div>

<!-- Add connection button -->
<UButton @click="showAddConnectionModal = true">
  <UIcon name="heroicons-plus" /> Add Connection
</UButton>
```

### 9.5 Update MentionInput

**File**: `frontend/components/prompt/MentionInput.vue` (lines 42-48)

Use `connection_type` instead of `data_source_type` for table icons:

```vue
<DataSourceIcon
  v-if="category.name === 'tables'"
  :type="item.connection_type || item.icon_type"
  class="w-3.5 h-3.5"
/>
```

### 9.6 Update data index page

**File**: `frontend/pages/data/index.vue` (lines 241-304)

Update to show connection count per domain:

```vue
<span class="text-xs text-gray-500">
  {{ ds.connections?.length || 0 }} connection{{ (ds.connections?.length || 0) !== 1 ? 's' : '' }}
</span>
```

---

## Phase 10: Tests

### 10.1 Update Fixtures

**File**: `backend/tests/fixtures/data_source.py`

Add multi-connection test helpers:
```python
@pytest.fixture
def create_domain_with_connections(test_client):
    """Create a domain with multiple connections."""
    def _create(*, connection_ids: List[str], ...):
        ...
    return _create
```

### 10.2 Update E2E Tests

**Files**:
- `backend/tests/e2e/test_domains.py`
- `backend/tests/e2e/test_data_source.py`
- `backend/tests/e2e/test_schema_drift.py`
- `backend/tests/e2e/test_connection.py`

Add tests for:
- Domain with multiple connections
- Schema refresh for all connections
- Code execution with multi-connection clients
- API response with connections array

---

## Phase 11: Migration

### 11.1 Database Migration

No schema changes needed - M:N relationship already exists.

### 11.2 Data Migration

Existing domains have 1 connection each - no data migration needed.

### 11.3 Frontend Migration

Update API client types and component props incrementally.

---

## Implementation Order

```
Week 1: Core Backend
├── Phase 1: Data models & schemas
├── Phase 2: Schema context builder
└── Phase 3: XML rendering

Week 2: Client Construction
├── Phase 4: construct_clients()
├── Phase 5: Update all ds_clients sites
└── Phase 6: Coder prompts

Week 3: Cleanup & API
├── Phase 7: Remove connections[0] usages
├── Phase 8: Mentions API
└── Phase 10: Tests

Week 4: Frontend
├── Phase 9.1-9.2: Types & TablesSelector
├── Phase 9.3-9.4: DomainSelector & connection pages
└── Phase 9.5-9.6: MentionInput & index
```

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Breaking generated code | Feature flag to toggle key format during transition |
| API consumers break | Version API or deprecation period |
| Test failures | Run tests continuously during implementation |
| Incomplete migration | Checklist tracking all 21 `connections[0]` removals |

---

## Definition of Done

- [ ] All 21 `connections[0]` usages removed
- [ ] All 10 `ds_clients` construction sites updated
- [ ] All coder prompts updated with new key pattern
- [ ] API returns `connections[]` array for all domain endpoints
- [ ] Frontend shows/filters by connection
- [ ] Schema context includes connection per table
- [ ] Mentions include connection per table
- [ ] All tests pass
- [ ] Code execution works with multi-connection domains
