import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional, Set

from app.ai.context.sections.files_schema_section import FilesSchemaContext
from app.models.organization import Organization
from app.models.report import Report

logger = logging.getLogger(__name__)

# Aggregate budget (tokens, ~4 chars/token) for rich previews in the <files>
# section. Forced-rich files (mentioned / attached this turn) always render
# rich; the budget bounds how many *additional* user files keep sample rows
# before degrading to the index tier.
RICH_PREVIEW_TOKEN_BUDGET = 4000
# Reports with at most this many user-attached files keep full previews for
# all of them — the single-CSV chat flow stays exactly as before.
SMALL_REPORT_RICH_MAX = 3


def _estimate_tokens(text: Optional[str]) -> int:
    return len(text) // 4 if text else 0


def decide_file_tiers(
    files: list,
    agent_file_ids: Set[str],
    forced_rich_ids: Set[str],
    rich_budget_tokens: int = RICH_PREVIEW_TOKEN_BUDGET,
    small_report_max: int = SMALL_REPORT_RICH_MAX,
) -> dict:
    """Pure tier decision: file id -> "full" | "index".

    Rules, in order:
      1. Mentioned / attached-this-turn files are always full.
      2. Agent-library files (snapshotted from a data source) are index-only —
         they are a catalog, discoverable and readable on demand.
      3. If the report has few user files, keep them all full.
      4. Remaining user files render full newest-first until the shared token
         budget runs out; the rest degrade to index.
    """
    tiers: dict = {}
    budget = rich_budget_tokens

    user_files = []
    for f in files:
        fid = str(getattr(f, "id", ""))
        if fid in forced_rich_ids:
            tiers[fid] = "full"
            budget -= _estimate_tokens(getattr(f, "description", None) or "")
        elif fid in agent_file_ids:
            tiers[fid] = "index"
        else:
            user_files.append(f)

    if len(user_files) <= small_report_max:
        for f in user_files:
            tiers[str(getattr(f, "id", ""))] = "full"
        return tiers

    def _created(f):
        v = getattr(f, "created_at", None)
        return v.isoformat() if v is not None else ""

    for f in sorted(user_files, key=_created, reverse=True):
        fid = str(getattr(f, "id", ""))
        cost = _estimate_tokens(getattr(f, "description", None) or "")
        if budget - cost >= 0:
            tiers[fid] = "full"
            budget -= cost
        else:
            tiers[fid] = "index"
    return tiers


class FilesContextBuilder:
    def __init__(self, db: AsyncSession, organization: Organization, report: Report, head_completion=None):
        self.db = db
        self.organization = organization
        self.report = report
        self.head_completion = head_completion

    async def _agent_file_ids(self) -> Set[str]:
        """Ids of report files that came from a data source's file library.

        Joins the association tables directly instead of touching possibly
        lazy relationships on the report object (async-safe).
        """
        if self.db is None or not getattr(self.report, "id", None):
            return set()
        try:
            from app.models.data_source_file_association import data_source_file_association
            from app.models.report_data_source_association import report_data_source_association
            result = await self.db.execute(
                select(data_source_file_association.c.file_id)
                .join(
                    report_data_source_association,
                    report_data_source_association.c.data_source_id
                    == data_source_file_association.c.data_source_id,
                )
                .where(report_data_source_association.c.report_id == str(self.report.id))
            )
            return {str(row[0]) for row in result.fetchall()}
        except Exception as e:
            logger.warning(f"[files_context] agent file lookup failed: {e}")
            return set()

    async def _forced_rich_ids(self) -> Set[str]:
        """Ids that must stay rich: @-mentioned this turn, or attached with the
        current user message."""
        forced: Set[str] = set()
        cid = str(getattr(self.head_completion, "id", "")) if self.head_completion else None
        if not cid or self.db is None:
            return forced
        try:
            from app.models.mention import Mention, MentionType
            result = await self.db.execute(
                select(Mention.object_id).where(
                    Mention.completion_id == cid,
                    Mention.type == MentionType.FILE,
                )
            )
            forced |= {str(row[0]) for row in result.fetchall()}
        except Exception as e:
            logger.warning(f"[files_context] mention lookup failed: {e}")
        try:
            from app.models.report_file_association import report_file_association
            result = await self.db.execute(
                select(report_file_association.c.file_id).where(
                    report_file_association.c.report_id == str(self.report.id),
                    report_file_association.c.completion_id == cid,
                )
            )
            forced |= {str(row[0]) for row in result.fetchall()}
        except Exception as e:
            logger.warning(f"[files_context] turn-attachment lookup failed: {e}")
        return forced

    async def build(self) -> FilesSchemaContext:
        files = getattr(self.report, 'files', []) or []
        if not files:
            return FilesSchemaContext(files=[])

        agent_ids = await self._agent_file_ids()
        forced_ids = await self._forced_rich_ids()
        tiers = decide_file_tiers(files, agent_ids, forced_ids)

        items: List[FilesSchemaContext.FileItem] = []
        for f in files:
            fid = str(getattr(f, 'id', '')) or None
            try:
                prompt_schema = f.prompt_schema()
            except Exception:
                prompt_schema = None
            detail = tiers.get(fid or "", "full")
            index_summary = None
            if detail == "index":
                try:
                    from app.services.file_preview import render_file_index_line
                    index_summary = render_file_index_line(
                        getattr(f, "preview", None),
                        getattr(f, "path", "") or "",
                        filename=getattr(f, "filename", "") or "",
                    )
                except Exception:
                    index_summary = (prompt_schema or "")[:200] or None
                prompt_schema = None
            items.append(
                FilesSchemaContext.FileItem(
                    id=fid,
                    filename=getattr(f, 'filename', getattr(f, 'name', 'unknown')),
                    path=getattr(f, 'path', None),
                    content_type=getattr(f, 'content_type', None),
                    prompt_schema=prompt_schema,
                    detail=detail,
                    index_summary=index_summary,
                    origin=("agent" if (fid or "") in agent_ids else "upload"),
                )
            )
        return FilesSchemaContext(files=items)
