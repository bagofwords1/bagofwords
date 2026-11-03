# Feedback loop: Neo4j connector, full sandbox pass

Date: 2026-11-03 · Branch: `feat/neo4j-connector` · Evidence: `media/pr/neo4j-connector/`

## What there is to test against

Neo4j ships official images, so everything below runs against a real server:

- **`neo4j:5.26-community`**: the LTS line, supported into 2028. Most self-hosted installs are on it.
- **`neo4j:2026.10-community`**: the newest calendar-versioned release. Aura runs this line.
- 4.4 has been out of support since Nov 2025 and isn't covered.

Not verified: a real **Aura** instance (`neo4j+s://`), and a real **cluster**. The routing-table check is exercised on a single server addressed as `neo4j://`.

## Verified, layer by layer

| Layer | Check | Result |
|---|---|---|
| Registry | `resolve_client_class('neo4j')`; discovery cancels before any I/O (`test_discovery_progress_contract`'s check, run on `neo4j` alone) | `Neo4jClient`; cancels |
| Unit | `tests/unit/test_neo4j_client.py`, with the driver replaced at `_read` | 57 passed |
| Unit | Cypher guard: 12 read queries pass, 24 write / admin / outbound forms refused (string literals, `//` and `/* */` comments, backslash escapes, backticks, mixed case, `CALL {…} IN TRANSACTIONS`, `apoc.*` in CALL and function position, `genai.*`) | all as expected |
| Unit | SSRF: `test_neo4j_uri_never_contacts_metadata`, 5 metadata forms × `bolt` / `neo4j+s` | refused before any connect |
| Break tests | guard removed from the executor → 2 tests fail; `native_query_params` off → the params test fails | as expected |
| Live (both images) | `tests/integrations/test_neo4j_live.py`: connection; catalog with patterns; plain values (node, path, date); params through the executor; **the server refuses a write with our guard switched off**; `LOAD CSV` refused by the guard; a slow query stopped by the server's transaction timeout; the row cap while reading; a `neo4j://` URI checks every routing-table address, and fails closed when one is refused | 18 passed |
| Integration suite | `ds_clients` container entry: the suite's own flow (container, seed, `test_connection`, `get_schemas`) | passed |
| Existing suites | every unit file for the touched areas (registry, egress, guards, params, identity bind, executor, create_data); `tests/e2e/test_data_source.py`, `test_connection.py` and the connection-payload e2e files | 1,308 + 12 + 29 passed. `test_every_registered_catalog_can_cancel_before_external_io` fails locally on main too: ms_fabric needs libodbc, which CI has |
| Frontend | `tests/unit/catalogNouns.mjs` | passed |

## Live UI pass

Desktop sandbox: the branch's backend on :8002 with a fresh SQLite database, the frontend on :3002, `neo4j:5.26-community` with a 22-node / 34-relationship movies graph (a few names and one film in Hebrew), and Anthropic Claude Sonnet 5.5.

1. Onboarding → Connect data → search "neo4j": the tile and icon show (`catalog-tile.png`).
2. The form is schema-generated: URI with help text, Database (default `neo4j`), Username, and a password-type Password (`connect-form.png`).
3. **Test connection**: "Connected successfully. Found 5 node and relationship types." (`test-connection.png`).
4. **Save and Continue**: the selector lists `(:Movie)`, `(:Person)`, `[:ACTED_IN]`, `[:DIRECTED]` and `[:FOLLOWS]` (`discovered-types.png`); a type expands to its properties (`type-expanded.png`).
5. Questions:
   - "Which actors appeared in the most movies? Show a bar chart." A correct chart and table (Hugo Weaving and Keanu Reeves 4, …), from `MATCH (p:Person)-[:ACTED_IN]->(m:Movie) RETURN p.name AS actor, count(DISTINCT m) …` (`chat-top-actors-chart.png`).
   - "Who directed the movies that Keanu Reeves acted in?" Correct. It says John Wick has no `DIRECTED` relationship in the graph rather than inventing a director. The query used `$name` with `params={"name": "Keanu Reeves"}`, so native params went through the code executor end to end (`chat-two-hops.png`).
   - A question in Hebrew: answered in Hebrew, with the right director. It said honestly that the graph has no nationality property to find "Israeli actors" by.
   - "Delete all Movie nodes released before 2000…": the agent declines (it only reads). The graph still has 22 nodes and the 3 pre-2000 films (`chat-delete-declined.png`).

## Found and fixed in this loop

- **The plan review** (a fresh read-only agent, since Codex was unavailable) found 3 blocking issues before any code was written:
  - the executor rendering `params=` as SQL breaks Cypher (`(:Person)` was read as a placeholder);
  - a deny-list guard missed outbound and admin forms;
  - `limit_row_count` applies only after the client has loaded everything.

  All three are designed in.
- **The guard hook matched the class by name**, so a subclass skipped the executor's guard. It now uses `isinstance`.
- **`db.schema.visualization()`'s virtual relationships** returned empty labels through `startNode()` on one server version. Patterns are now matched by element id in Python.
- **UI:**
  - In a Hebrew conversation, the tool chips showed `(:Movie)` as `(Movie:)`. Table names are now `<bdi dir="ltr">` in the CreateData, InspectData and DescribeTables chips (`chat-hebrew-before-bidi-fix.png` / `chat-hebrew-names-after-bidi-fix.png`).
  - The onboarding selector's own noun code said "Search collections…". It now uses the shared `nounFor` with the connector's `catalog_nouns`. This screen wasn't re-captured after the fix, because the sandbox org had finished onboarding. The logic is the one `catalogNouns.mjs` tests.

## Not covered / follow-ups

- Aura and a real cluster, as above.
- Table scope isn't enforced at query time for Cypher, as for every non-SQL connector (`table_scope.py`): a hand-written query can read deselected labels.
- Type annotations, APOC and GDS are refused by design in v1.
- The expanded relationship row shows its properties but not the `(:A)-[:R]->(:B)` patterns. The agent gets them in its schema.
- The DescribeTables chip says "Searched" in English inside a Hebrew conversation. That predates this branch.

## Re-run

```bash
cd backend
uv run pytest tests/unit/test_neo4j_client.py tests/unit/test_egress_package_b.py -k neo4j -q
uv run pytest tests/integrations/test_neo4j_live.py -v   # needs Docker
```
