"""Diagnosis explorer: a query language over agent runs and their tool calls.

- ``grammar``  — parser (pure; mirrored in ``frontend/utils/diagnosisQuery.ts``)
- ``fields``   — the field registry (the allowlist; generates the syntax help)
- ``compiler`` — AST → SQLAlchemy clause
- ``rollup``   — the denormalised columns on ``agent_executions``
- ``service``  — ``run_query`` and friends (used by the routes and, later, tools)
"""
