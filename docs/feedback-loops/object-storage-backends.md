# Feedback loop — S3 / Azure Blob file storage, verified from a pod that never saw the files

Every durable file used to live only on the pod's disk (`backend/uploads/`). The
Helm chart mounts no volume there, so replacing a pod lost every upload,
thumbnail, deck, logo and avatar while the database rows pointing at them
survived, and a second replica could not see the first one's files.
`storage.backend: s3 | azure` makes the bucket the source of truth; `local`
stays the default and behaves exactly as before. Design and as-built notes:
`docs/design/s3-storage.md`.

## The loop: "a new pod" = an empty uploads/ in its own mount namespace

`object-storage/run_fresh_pod.sh` starts the backend with a tmpfs mounted over
`backend/uploads` inside `unshare -m` — what a new replica with an emptyDir
sees — against the same database. Each run printed `uploads at start: 0 files`.
The other scripts drive the real UI with Playwright (Claude Haiku as the LLM):

```sh
cd backend && uv run python ../tools/agent/seed_org.py --db-path db/<db>.db
ANTHROPIC_API_KEY=... uv run python ../tools/agent/setup_haiku_llm.py
cd ../docs/feedback-loops/object-storage
node 01_login.js
node 02_chat.js "Using the attached sales.csv, compute total revenue per region with code..." ./sales.csv
node 02_chat.js "Create a dashboard with a bar chart of total revenue by region" "" <report-url>
node 02_chat.js "Create a short 3-slide presentation about revenue by region" "" <report-url>
node 03_branding.js                       # org logo (Settings → General) + avatar (Account Settings)
sudo unshare -m --propagation private ./run_fresh_pod.sh <env-file>   # new pod
echo <file id> > fid.txt && node 04_newpod.js   # logo, avatar, thumbnails, file download
node 06_split.js "What two colors are in the attached image?" ./avatar.png &   # attach on pod A…
# …wait for ./ready, start a fresh pod B, then: touch ./go                    # …send on pod B
node 02_chat.js "Read sales.csv again with code and give me total units per product" "" <report-url>
```

The env file holds `BOW_DATABASE_URL`, `BOW_ENCRYPTION_KEY` and the
`BOW_STORAGE_*` settings (never committed).

## Results

Run against a real AWS S3 bucket (`auth: static`) and a real Azure Blob
container (`auth: sas`), each with its own key prefix and database, then the
default local backend.

| Check, on a pod that started with 0 local files | S3 | Azure |
|---|---|---|
| CSV upload → object in bucket; download returns same bytes | ✅ | ✅ |
| Follow-up chat: code sandbox reads the CSV (answer correct: Widget 455 / Gadget 150) | ✅ | ✅ |
| Agent tool outputs (`write_csv`) stored and re-read | ✅ | ✅ |
| Dashboard thumbnail rendered on pod A, served byte-identical on pod B | ✅ | ✅ |
| Org logo and avatar render in the UI | ✅ | ✅ |
| Publish dashboard → release thumbnail copied to bucket | ✅ | — |
| PDF export of the published dashboard (valid 133 KB PDF) | ✅ | — |
| Slide deck `.pptx` + 3 slide previews downloadable | ✅ | ✅ |
| Each object fetched once, then served from the pod's cache | ✅ (urllib3 trace) | ✅ |
| Image attached while pod A served, message sent after swapping to a fresh pod B: Haiku describes the image (gold background, purple circle) | ✅ | — |
| Backend errors / tracebacks | 0 | 0 |

Default `local` backend: same upload + chat + logo + avatar flow passed, files
on local disk, **0** calls to any object store.

The bucket listing after the S3 run (prefix stripped):
`files/<uuid>_sales.csv`, two `files/<uuid>_write_csv_output.csv`,
`thumbnails/<version>.png`, `thumbnails/release-<id>.png`,
`pptx/<id>.pptx`, `pptx_previews/<id>/slide-0{1,2,3}.png`,
`avatars/<user>-<digest>.png`, `branding/<org>-<digest>.png`.

## Automated tests

- `backend/tests/unit/test_object_storage.py` — storage contract (new pod gets
  the bytes, replaced/deleted-elsewhere copies never served, trees, prefixes,
  local-only areas, config validation, assume-role STS).
- `backend/tests/unit/test_object_storage_caches.py` — PBIX decode reused by a
  pod that never decoded, learned SSH host keys merged across pods, fast
  extracts, the uploads/ migration script.
- `backend/tests/e2e/test_object_storage_e2e.py` — upload/avatar/logo through
  the API with the local copy deleted; unreachable store → 503 and no orphan row.

Each runs per backend via the `object_store` fixture: `s3-moto` (always),
`azure-azurite` (when Azurite is listening), `s3-real` / `azure-real` (when
`BOW_TEST_S3_BUCKET` / `BOW_TEST_AZURE_*` are set). Observed: **113 unit runs
passed with 0 skipped** across all four backends; e2e 9 passed on moto +
Azurite. Each new test was seen failing with its wiring removed (download
route, PBIX hydration).

Regression on the default local backend: every e2e suite touching files,
dashboards, decks, exports, uploads, branding, email or connectors (140 files,
run as CI runs them): 1313 passed. The only failures are 25 dashboard
migration/visibility tests (`public_sharing_allowed None -> True`) that fail
identically on the commit before this change. A few file and connector tests
also failed only because this container sets `REQUESTS_CA_BUNDLE`; they pass
once it is unset. Unit tests importing any changed module: 2101 passed (one
file needs `libodbc`, missing here).

## Found along the way

- Images attached to a chat message are read from disk to send to the LLM
  (`app/ai/agent_v2.py`, `create_artifact.py`). A grep sweep missed them at
  first; on pod B the read failed, was logged as a warning, and the message went
  out without the image. The split-pod run is the test that catches it.

- Azure SDK: `ContainerClient.upload_blob` returns a client, not the ETag
  dict — caught by the Azurite contract run; the backend uses
  `BlobClient.upload_blob`.
- Storage settings from env vars skipped validation (pydantic does not validate
  defaults); fixed with `validate_default=True`.
- Pre-existing, not changed here: the dashboards list builds a slide deck's
  card image as `/thumbnails/slide-01.png`, which 404s on local disk too
  (`app/services/dashboard_list.py:_thumbnail_url`).
