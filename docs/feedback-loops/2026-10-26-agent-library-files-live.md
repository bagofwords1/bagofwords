# Feedback Loop — "I can't reach the agent's Files panel"

A customer moved a long event catalogue (`core.yaml`, `devices.yaml`,
`names.tsv`) from an instruction into the agent's Files tab. In a new chat the
agent said it had no file ids, `read_file` failed, and it fell back to
`ILIKE`-scanning the warehouse for event names. With the agent picked
explicitly the files loaded, but `read_file` returned them cut off, and the
agent guessed event names instead of searching for them.

## Root cause (validated)

1. **Agent files were a snapshot, not live.** `DataSource.files` reached a
   conversation only when it was copied onto the report at creation or agent
   switch (`backend/app/services/report_service.py`, `create_report` and
   `set_data_sources_for_report`). Two cases got nothing:
   - **Auto mode** (no agent picked): the report has no agents attached and
     resolves them per run (`agent_focus_common.resolve_run_agents`), so nothing
     was ever copied.
   - **Files uploaded after the chat started.**
   Without files in the conversation, `read_file`/`grep_files` were not offered
   and the `<files>` block was empty.
2. **Nothing pointed the agent at grep.** `read_file` shows the model 4,000
   chars per read (`read_file.py`, `_OBS_DETAILS_MAX_CHARS`). Its "partial"
   trailer said to page and mentioned a `connection_id` these files don't have.
   `grep_files` described itself as a connector tool, and the `<files>` note
   only mentioned `read_file`/`inspect_data`.

## Loop A — deterministic reproduction

`backend/tests/e2e/test_agent_library_files_live.py`. It drives run-agent
resolution, the `<files>` block, `read_file` and `grep_files` against the real
API and DB.

```bash
cd backend
export TESTING=true ENVIRONMENT=production
uv run pytest tests/e2e/test_agent_library_files_live.py -m e2e --db=sqlite -q
```

Before the fix: `6 failed, 1 passed`. The only passing case is "agent picked,
file uploaded before the chat", which already worked. The `<files>` block
rendered `<files>\n\n</files>`, and the read trailer had no `grep_files`.

## The fix

- `backend/app/ai/context/agent_files.py`: `agent_library_files()` reads the
  run agents' already-loaded `files` (no extra query, no lazy load).
- `AgentV2.__init__` stages them as `self.agent_files`. They join
  `analysis_files` (so `inspect_data`/`create_data` see them), enable
  `read_file`/`grep_files`, and are passed in `runtime_ctx["agent_files"]`.
- `resolve_session_file` (read_file) and `grep_files`' conversation sweep
  include them. `grep_files` now also includes project files, which `read_file`
  already did.
- `FilesContextBuilder` lists them (`source="agent"`, index tier) from the
  run's agents.
- Wording: `grep_files` says it searches the conversation's files, agent files
  included. A partial text read says PARTIAL and gives the exact `grep_files`
  call for that file (no `connection_id` for conversation files). The
  `<files>` note says to grep large text files first and never infer an entry
  that wasn't found.

The run agents are already access-filtered, so a user only sees the library of
agents the run uses. This is covered for a non-admin member under Auto.

After: `7 passed`. Agent startup was also checked directly: for a file uploaded
after the chat started, with the agent picked and in Auto, the file is in
`agent_files` and `analysis_files`, and `read_file`/`grep_files` are offered.

## Regression notes

- The snapshot on report creation stays. It is now redundant for live agents
  but harmless (files are de-duplicated).
- A file removed from the agent disappears from Auto runs at once (tested).
  Reports that snapshotted it earlier keep their copy, as before.
- Not changed: file-connector tool gating still reads `report.data_sources`, so
  an Auto chat doesn't get list/search tools for file connectors. That's a
  separate issue.
- Pre-existing failures in this sandbox (SharePoint 2010, email attachments)
  reproduce identically on `main`.
