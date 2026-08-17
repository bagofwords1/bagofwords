# Feedback Loop — deleting an agent or connection failed with eval suites

Deleting an agent with a per-agent Drafts suite failed in PostgreSQL because
`test_suites.data_source_id` still referenced the agent. Deleting a connection
failed through the same path when that connection owned the agent. The first
foreign-key error was then masked by the retry path reading an expired
`organization` ORM instance and raising `MissingGreenlet`.

## Reproduction and fix

Two lifecycle tests create an agent, attach a Drafts suite, and delete either
the agent directly or its sole connection. Before the fix, both left the suite
pointing at the deleted agent under SQLite; PostgreSQL rejected the parent
delete with `fk_test_suites_data_source_id`.

A suite's agent is its drafting home, not ownership or authorization scope, so
the safe behavior is to preserve the suite and its cases while clearing the
home. Both deletion services now set `data_source_id` to NULL before removing
the agent. The model and migration also define `ON DELETE SET NULL` as a
database-level safety net. The data-source retry and audit paths capture scalar
organization/user IDs before a possible rollback, preventing async lazy IO.

Both targeted tests failed before the implementation and pass afterward. The
complete connector and lifecycle verification finished with 88 passing tests.
