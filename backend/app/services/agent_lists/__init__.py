"""Agent Lists: typed per-agent record collections filled by submit_<slug> tools.

Modules:
- ``access``   — who may view/manage a list (follows the owning agent)
- ``naming``   — slugs, tool names and bow table names
- ``compiler`` — list fields -> strict-subset JSON Schema for the tool input
- ``schema_change`` — additive vs breaking schema edits (version bumps)
- ``records``  — validate + upsert submitted records, revisions, locks
- ``verify``   — evidence quote verification against text the agent read
"""
