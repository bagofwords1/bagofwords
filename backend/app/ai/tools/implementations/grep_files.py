"""grep_files agent tool — line-level content grep over a file-based data source.

The line-level sibling of search_files: search_files answers "which FILES
match" (fuzzy, keyword-index-accelerated); grep_files answers "which LINES
match" (deterministic regex over raw bytes) and returns the hits plus sweep
accounting, so a large log corpus is reduced at the source instead of being
paged into context. Capability-gated to sources that can scan raw bytes
(network_dir, s3) — provider-indexed sources (SharePoint/Drive) never see it.
"""
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.file_tools import GrepFilesInput, GrepFilesOutput
from app.data_sources.clients.base import Capability
from app.data_sources.clients._file_source_common import GlobScopeError
from app.data_sources.clients._grep_common import (
    SKIP_ACCESS_DENIED,
    compile_pattern,
    render_matches_details,
)

from ._file_tool_common import audit_file_access_denied, resolve_file_client

# Self-terminate the sweep with a clean cursor safely inside the tool's hard
# timeout (120s) — a partial result with a resume token beats a killed run.
_SWEEP_TIME_BUDGET_SECONDS = 90.0


class GrepFilesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="grep_files",
            description=(
                "Extract matching LINES (with line numbers and context) from "
                "text files on a Files & Directories or S3 connection — "
                "deterministic regex over raw file bytes, like grep -n. Scans "
                "many files per call with explicit budgets and a resumable "
                "cursor, and reports the total match count — use it to reduce "
                "logs/text at the source instead of reading whole files. Any "
                "file whose content is text is scanned (.log, .txt, .csv, "
                ".ndjson, extensionless); binaries are skipped and reported. "
                "For finding WHICH documents are relevant (fuzzy/ranked), use "
                "search_files; for transforms/aggregation on data, use "
                "inspect_data / create_data."
            ),
            category="research",
            input_schema=GrepFilesInput.model_json_schema(),
            output_schema=GrepFilesOutput.model_json_schema(),
            idempotent=True,
            timeout_seconds=120,
            tags=["files", "grep", "logs", "lines", "search", "network_dir", "s3"],
            requires_capability="grep_files",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GrepFilesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GrepFilesOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = GrepFilesInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={
            "title": f"Grepping files for {data.pattern!r}",
            "connection_id": data.connection_id,
        })

        def _fail(msg: str) -> ToolEndEvent:
            return ToolEndEvent(type="tool.end", payload={
                "output": {"success": False, "connection_id": data.connection_id,
                           "pattern": data.pattern, "error": msg},
                "observation": {"summary": msg, "success": False},
            })

        # Validate the pattern up front — a bad regex is a correctable input
        # error, not a sweep failure.
        try:
            compile_pattern(data.pattern, is_regex=data.is_regex, ignore_case=data.ignore_case)
        except (re.error, ValueError) as e:
            yield _fail(f"Invalid pattern: {e}")
            return

        client, err = await resolve_file_client(
            runtime_ctx, data.connection_id, Capability.GREP_FILES
        )
        if err:
            yield _fail(err)
            return

        try:
            sweep = await client.agrep_files(
                data.pattern,
                is_regex=data.is_regex,
                ignore_case=data.ignore_case,
                file_ids=data.file_ids,
                folder_id=data.folder_id,
                name_pattern=data.name_pattern,
                recursive=data.recursive,
                before=data.before,
                after=data.after,
                max_matches=data.max_matches,
                max_matches_per_file=data.max_matches_per_file,
                max_files=data.max_files,
                max_bytes_per_file=data.max_bytes_per_file,
                cursor=data.cursor,
                time_budget_seconds=_SWEEP_TIME_BUDGET_SECONDS,
            )
        except GlobScopeError as e:
            await audit_file_access_denied(
                runtime_ctx, data.connection_id, data.folder_id or "", str(e)
            )
            yield _fail(str(e))
            return
        except NotImplementedError:
            yield _fail("This connection does not support line-level grep.")
            return
        except (re.error, ValueError) as e:
            yield _fail(str(e))
            return
        except Exception as e:
            yield _fail(f"grep_files failed: {e}")
            return

        # Per-file scope denials inside the sweep (explicit off-glob file_ids)
        # are reported as skips — audit each so the trail matches read_file's.
        for s in sweep.get("skipped_files", []):
            if s.get("reason") == SKIP_ACCESS_DENIED:
                await audit_file_access_denied(
                    runtime_ctx, data.connection_id, s.get("file_id", ""),
                    "grep_files: file is outside this connection's allowed patterns",
                )

        output = {
            "success": True,
            "connection_id": data.connection_id,
            "pattern": data.pattern,
            "matches": sweep.get("matches", []),
            "total_matches": sweep.get("total_matches", 0),
            "files_scanned": sweep.get("files_scanned", 0),
            "files_with_matches": sweep.get("files_with_matches", 0),
            "files_skipped": sweep.get("skipped_files", []),
            "truncated": sweep.get("truncated", False),
            "stop_reason": sweep.get("stop_reason", "complete"),
            "next_cursor": sweep.get("next_cursor"),
        }

        # Observation carries the accounting even when the lines don't fit —
        # "1,240 matches, showing 100" is the reduction guarantee.
        bits = [
            f"{output['total_matches']} match(es) in "
            f"{output['files_with_matches']}/{output['files_scanned']} file(s) "
            f"for {data.pattern!r}"
        ]
        if len(output["matches"]) < output["total_matches"]:
            bits.append(f"showing {len(output['matches'])}")
        if output["files_skipped"]:
            bits.append(f"{len(output['files_skipped'])} file(s) skipped")
        if output["stop_reason"] != "complete":
            bits.append(f"stopped: {output['stop_reason']} — continue with cursor")
        observation: Dict[str, Any] = {"summary": " — ".join(bits), "success": True}
        # The planner reads the observation, not the raw output — ship the
        # matched lines themselves (grep-style, capped) or the whole sweep is
        # just a count to the model.
        if output["matches"]:
            observation["details"] = render_matches_details(
                output["matches"],
                has_more_pages=bool(output["next_cursor"]),
            )
        yield ToolEndEvent(type="tool.end", payload={
            "output": output,
            "observation": observation,
        })
