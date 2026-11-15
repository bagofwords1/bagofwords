# Feedback loop — “How does the BOW agent know which TShark fields to ask for?”

Connecting a mixed network directory starts background indexing in **Full text** (default) or **Keywords** mode. BOW runs TShark over each PCAP or PCAPNG, writes one Parquet file per capture, and publishes the discovered columns on the agent's `packets` table. QVDs each get their own table. Documents and images stay accessible through Files.

## Root cause on the baseline

At commit `1ef70323f559c0b902809d255d0cd6e16431e9d5`, `backend/app/data_sources/clients/network_dir_client.py:1117` gave every cataloged file an empty column list. At line 1154, `execute_query` treated its argument as a file identifier. A folder containing a DNS capture could be listed, but its schema exposed zero DNS columns.

## Replay the failure and the fix

Run from the repository root in a fresh checkout. Use Python 3.13, uv, TShark on PATH, and Rust for the existing QVD converter. On Debian or Ubuntu the decoder package is `tshark`. On macOS Homebrew supplies it in `wireshark`. The fixture generates two small captures locally. The tests use a disposable application database and require no network traffic or model credentials.

```bash
repo=$PWD
(cd backend && uv sync --frozen --extra dev)
cargo build --release --locked --manifest-path tools/qvd2parquet/Cargo.toml
export QVD2PARQUET_BIN="$repo/tools/qvd2parquet/target/release/qvd2parquet"
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
py="$repo/backend/.venv/bin/python"
testcase=tests/unit/test_network_dir_packet_index.py::test_mixed_capture_schemas_and_refresh

trial=$(mktemp -d)
git worktree add --detach "$trial/baseline" 1ef70323f559c0b902809d255d0cd6e16431e9d5
cp backend/tests/fixtures/packet_captures.py "$trial/baseline/backend/tests/fixtures/"
cp backend/tests/unit/test_network_dir_packet_index.py "$trial/baseline/backend/tests/unit/"
mkdir -p "$trial/baseline/backend/db" "$repo/backend/db"
(cd "$trial/baseline/backend" && "$py" -m pytest -q --db=sqlite "$testcase")
```

The baseline fails on the capture's discovered schema.

```text
E       AssertionError: assert 'dns.a' in set()
```

Run the same assertion and the surrounding regressions in the changed checkout.

```bash
cd "$repo/backend"
"$py" -m pytest -q --db=sqlite "$testcase"
"$py" -m pytest -q --disable-warnings --db=sqlite \
  tests/unit/test_network_dir_{client,incremental_index,packet_index}.py \
  tests/unit/test_qvd_client.py \
  tests/e2e/test_network_dir_{packet_index,e2e,incremental_e2e,workbook_sheets}.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  tests/unit/test_fast_{client_introspection_guard,sql_dialect}.py tests/e2e/test_connection_indexing.py
git -C "$repo" worktree remove --force "$trial/baseline"
```

Observed on the changed checkout with TShark 4.6.9, DuckDB 1.5.4, and PyArrow 23.0.1: **195 passed**. The API-to-agent test also passed on a disposable PostgreSQL 14 database. To repeat that leg with Docker available, run `"$py" -m pytest -q --db=postgres tests/e2e/test_network_dir_packet_index.py`.

## Observable behavior

- The two capture formats contribute rows to the same table. A quoted field such as `"dns.a"` is NULL for an ARP packet. Both answers in a DNS response survive in order as a list. `capture_id` and `frame_number` identify the original packet. The regression also puts the first DNS packet after 1,024 ARP packets to exercise late column discovery.
- `SELECT file_id, status, reason FROM indexed_files` reports coverage for captures and QVDs in scope. Editing a capture makes its new version pending until refresh. Deleting one removes it from queries. An empty capture is ready with zero rows, and a corrupt capture reports failure. An unchanged capture remains queryable from cache with TShark off the path.
- The mixed-file test reads real QVD rows, Word text and JPEG bytes through their respective interfaces. Live and File list indexing modes skip conversion. File and directory limits still apply, including the default 100 MiB per file.
- The end-to-end test creates the connection through the API, pauses the decoder, verifies documents and pending captures are already cataloged, and then attaches an agent. Its prompt includes four frame columns. The agent discovers `dns.a` using `duckdb_columns()` and queries three packets, including two DNS packets. It then disables the packet table and verifies the agent loses both schema visibility and query access. Verification used the planner context and SQL client directly. Large production captures and model-generated chat answers were outside this run.

- The payload regression compares an independent TShark decode to the queryable field catalog and reads raw bytes through SQL. PyArrow without a key is unable to open the encrypted file. Both packet captures and QVDs use Parquet modular encryption with BOW's artifact key wrapping. Files introduced after the catalog walk appear on the next refresh. If a walk hits its file cap, `indexed_files` includes a `truncated` row.
- Linux uses Landlock, libseccomp, CPU and address-space limits. macOS uses Seatbelt, a CPU limit and resident-memory monitoring. The sandbox receives the capture on stdin with an isolated home directory. A platform that cannot install the sandbox reports a failed conversion.
- Landlock checks execute permission on the dynamic loader as well as on `tshark`, so the launcher also allows the system ELF loaders. Without that, every decode failed with `Permission denied` on kernel 6.18 (Ubuntu 24.04, TShark 4.2.2). Any failure to start the decoder exits with status 78 and is reported as the sandbox being unavailable on this host.
- Each failure records a reason and whether retrying can help. Malformed captures and time, memory or crash limits are kept for that file version and not decoded again until the file changes. A missing decoder or a sandbox that cannot start is retried on the next refresh. `indexed_files.reason` names the cause, for example the time limit or the sandbox being unavailable on this host. Staging, including QVD plaintext, stays inside the connection's private cache directory.

## Browser evidence

The same seeded mixed share was opened at the agent's **Select Tables** step in Chromium, at 1440 × 900. Before the change only Files was available. After indexing, Tables shows `packets`, the QVD table and `indexed_files`.

[Before](../../media/pr/network-dir-tables/before.png) · [After](../../media/pr/network-dir-tables/after.png) · [Switch between Files and Tables](../../media/pr/network-dir-tables/tabs.gif)
