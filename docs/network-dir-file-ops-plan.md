# network_dir — file-operations plan

Design note for extending the `network_dir` connector with the file
inspection primitives sysadmins expect, plus an agent-level "auto-activate
new files" flag. **No implementation yet — this is scope + approach for
review.**

Anchors: `backend/app/data_sources/clients/network_dir_client.py`,
`backend/app/ai/tools/schemas/file_tools.py`,
`backend/app/ai/tools/implementations/{list_files,read_file,search_files}.py`,
`backend/app/services/data_source_service.py`,
`backend/app/services/connection_indexing_service.py`.

---

## 1. Guiding principle: typed operations, not a shell

The connector's entire security model is that `_resolve`
(`network_dir_client.py:122`) is the **single chokepoint** confining every
read/write to `root_path` — it rejects `..`, absolute escapes, and
out-of-root symlinks. These are customer SMB/NFS mounts, not a dev sandbox.

We therefore expose sysadmin capabilities as **typed parameters on the
existing file tools**, never as a bash/shell surface. Reasons:

1. **Confinement.** A shell hands the agent a command string → shell
   injection, glob expansion, `$(…)`, path escape all become live and must
   be re-defended per call. Typed params reuse the one `_resolve` chokepoint.
2. **Doc-format awareness.** The connector already extracts text from
   pdf/docx/pptx/xlsx (`_file_text`, `extract_document_text`). `grep`/`tail`
   are byte-level and blind to those — strictly worse for the mixed catalog.
3. **Structured output.** Every tool is typed pydantic in/out and composes
   with the stack (`session_file_id` attach, catalog metadata, inspect_data).
   Raw stdout composes with none of it.
4. **Portability.** GNU vs BusyBox vs macOS flag differences; minimal
   containers. Python (`itertools.islice`, `collections.deque`) is uniform.

Even a "safe" no-shell fixed-arg subprocess (`["tail","-n",n,path]`) loses
here: still byte-level, still unstructured, still a portability/maintenance
cost, for a 2-line Python equivalent.

## 2. Scope boundary: inspect/discover vs. transform

Unix file verbs split into two buckets. **Only the first is in scope.**

- **Bucket A — discover/inspect** (`ls`, `find`, `grep`, `head`/`tail`,
  `wc`, `du`/`stat`): legitimately a file-connector concern → add as typed
  params (Section 3).
- **Bucket B — transform** (`sort`, `uniq`, `cut`, `awk`, `join`): **already
  owned by the analysis stack.** Once a file is attached via `read_file`
  (`session_file_id`), `inspect_data`/`create_data`/pandas do this better
  than a pipe. Do **not** rebuild in the connector.

## 3. Verb inventory

| Unix | Need | Mechanism | Status |
|------|------|-----------|--------|
| `ls` | list a folder | `list_files` | **exists** (`list_files.py`) |
| `ls`+glob | filter by name | `list_files.name_pattern` (fnmatch) | **exists** (`list_files.py:105`) |
| `ls -lS` | sort by size | `list_files.sort_by=size` | **add** |
| `ls -t` | sort by mtime | `list_files.sort_by=modified` | **add** |
| `find -size +N` | filter by size | `list_files.min_size`/`max_size` | **add** |
| `find -mtime` | filter by date | `list_files.modified_after`/`_before` | **add** |
| `find -name` recursive | recursive glob | `list_files.recursive` + `name_pattern` | **exists** |
| `du -sh` | folder total | size/count summary on `list_files` output | **add** (optional) |
| `stat` | per-file metadata | `_entry` (size, mtime, mime) | **exists** (`network_dir_client.py:158`) |
| `cat` | read whole file | `read_file` | **exists** (`network_dir_client.py:214`) |
| `head` (bytes) | leading slice | `read_file.max_chars` | **exists** (`file_tools.py:94`) |
| `head -n` / `sed -n a,b` | line range | `read_file.line_offset`+`line_limit` | **add** |
| `tail -n` | trailing lines | `read_file.tail_lines` | **add** |
| `wc -l` | line count | field on `read_file`/`stat` output | **add** (optional) |
| `grep -ril` | file-level content search | `search_files` | **exists** (`network_dir_client.py:309`) |
| `grep -n -C` | matched lines + context | `search_files` line-level mode | **add** |
| `sort`/`uniq`/`cut`/`awk` | transform contents | inspect_data / create_data / pandas | **downstream** |

## 4. Proposed API additions (Bucket A only)

Typed params on the three existing schemas in `file_tools.py`. Each is
optional and backward-compatible.

**File sources only.** These params ride on `list_files`/`read_file`/
`search_files`, which are gated by the `LIST_FILES`/`READ_FILE`/
`SEARCH_FILES` capabilities (`requires_capability` on each tool's metadata).
Only file-based clients declare those capabilities, so the new params are
inherently unavailable to SQL / MCP / other non-file connectors — no extra
gating needed.

**`ReadFileInput`** (`file_tools.py:65`)
- `line_offset: int = 0`, `line_limit: int | None` — return lines
  `[offset, offset+limit)` (covers `head -n`, `sed -n`).
- `tail_lines: int | None` — return the last N lines (covers `tail`).
- Applies to the text/extracted-text branch only; tabular/doc semantics
  unchanged. Mutually exclusive with each other; validate.

**`ListFilesInput`** (`file_tools.py:24`)
- `sort_by: name|size|modified = name`, `order: asc|desc = asc`.
- `min_size`/`max_size` (bytes), `modified_after`/`modified_before` (ISO).
- Optional `include_summary: bool` → total bytes + count (`du`-style).
- All filters run against cached catalog rows (`list_files.py` reads the
  cache), so no extra disk walk.

**`SearchFilesInput`** (`file_tools.py:130`)
- `line_mode: bool = False` → when set, return per-match line number +
  `context_before`/`context_after` window instead of file-level entries.
- Runs over extracted text (so pdf/docx grep still works).

## 5. No consolidation — params on existing tools

**Decision:** add the Bucket-A capabilities as params on the three existing
tools (`list_files`/`read_file`/`search_files`). Do **not** introduce a
consolidated `inspect_files` tool. This keeps the tool surface stable and
each op typed, `_resolve`-confined, and doc-extraction-aware. Still not bash.

## 6. Agent-level "auto-activate new files" flag

**Decision: the flag lives on the `DataSource` (agent), not the Connection.**

Rationale:
- `is_active` is already a per-agent property (`DataSourceTable.is_active`);
  the connection catalog (`ConnectionTable`) has no activation concept.
- One connection fans out to many agents: reindex loops over
  `connection_snapshot.data_sources` and calls
  `sync_domain_tables_from_connection` **once per DataSource**
  (`connection_indexing_service.py:643-677`). A connection-level flag would
  force one policy on every agent; an agent-level flag lets each choose.

**Current behavior (the gap):** on reindex, new files are added as
`DataSourceTable` rows but `is_active=should_activate`
(`data_source_service.py:4380`), and the reindex path passes
`max_auto_select=ONBOARDING_MAX_TABLES` (`connection_indexing_service.py:676`)
which is `0` (`data_source_service.py:3392`) → `should_activate = total <= 0`
→ **False**. New files land **inactive**; a human must toggle them on.
Existing files preserve `is_active` (`data_source_service.py:4345`).

**Change:**
- Add `auto_activate_new_tables: bool` to `DataSource`.
- In the reindex fan-out, compute activation from the DS flag instead of the
  hardcoded `ONBOARDING_MAX_TABLES` (e.g. flag on → activate newly-created
  rows; flag off → current inactive behavior). Only affects *new* rows;
  existing selection is still preserved.
- **Default:** ON for file-shaped connectors (`data_shape == "files"`), OFF
  for SQL — a folder that auto-includes matches expectation; a 500-table
  warehouse should not auto-dump.
- **UI:** may be surfaced in ConnectionDetailModal for the common
  1-connection↔1-agent case, but persisted on the agent. For a connection
  with >1 agent the control must be per-agent (or clearly scoped), or a
  single toggle silently rewrites every agent's policy.

## 7. Explicitly out of scope

- **Bash / shell tools** — Section 1.
- **Bucket B transforms** (`awk`/`sort`/`cut`) — already downstream.
- **`.qvd` (and other proprietary binaries) content parsing** — `read_file`
  returns raw bytes for unknown extensions
  (`network_dir_client.py:264-268`); QVD needs a dedicated parser. Listing/
  attaching qvd already works via `allowed_extensions`.
- **Path-glob on `root_path`** (e.g. `sales/**/*.qvd` as connection config)
  — not present today (only per-call `name_pattern`). Deferred unless a
  concrete need appears; extension filtering (`allowed_extensions`) covers
  the common "only .qvd" case with no work. Keep `root_path` a literal dir so
  the `_resolve` chokepoint stays intact.

## 8. Open questions

1. ~~Consolidate into one `inspect_files` tool?~~ **Resolved: no — params on
   the existing three tools (Section 5).**
2. `du`/`wc` summaries — worth the extra fields, or defer?
3. Auto-activate default — confirm ON-for-files / OFF-for-SQL, or make it an
   explicit choice at agent creation with no implicit default?
4. Does auto-activate need a cap (activate up to N new files per reindex) to
   avoid a huge folder flooding an agent's active set in one sync?
