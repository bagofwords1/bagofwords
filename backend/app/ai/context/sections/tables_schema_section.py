from typing import ClassVar, List, Optional
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape
from app.schemas.data_source_schema import DataSourceSummarySchema
from app.ai.prompt_formatters import Table as PromptTable


class TablesSchemaContext(ContextSection):
    tag_name: ClassVar[str] = "schemas"

    class DataSource(ContextSection):
        tag_name: ClassVar[str] = "data_source"
        info: DataSourceSummarySchema
        tables: List[PromptTable] = []

        def render(self) -> str:
            tables_xml = []
            for t in self.tables or []:
                cols = "\n".join(
                    f'<column name="{xml_escape(c.name)}" dtype="{xml_escape(c.dtype or "")}"/>'
                    for c in (t.columns or [])
                )
                pks = "\n".join(
                    f'<pk name="{xml_escape(pk.name)}" dtype="{xml_escape(pk.dtype or "")}"/>'
                    for pk in (t.pks or [])
                )
                fks = "\n".join(
                    f'<fk column="{xml_escape(fk.column.name)}" '
                    f'ref_table="{xml_escape(fk.references_name)}" '
                    f'ref_column="{xml_escape(fk.references_column.name)}"/>'
                    for fk in (t.fks or [])
                )
                metrics_lines: List[str] = []
                if any(v is not None for v in [t.score, t.usage_count, t.success_count, t.failure_count, t.success_rate, t.pos_feedback_count, t.neg_feedback_count, t.last_used_at, t.last_feedback_at]):
                    if t.score is not None:
                        metrics_lines.append(f'<score value="{xml_escape(str(round(t.score, 6)))}"/>')
                    if any(v is not None for v in [t.usage_count, t.success_count, t.failure_count]):
                        metrics_lines.append(
                            f'<usage count="{t.usage_count or 0}" success="{t.success_count or 0}" failure="{t.failure_count or 0}"/>'
                        )
                    if t.success_rate is not None:
                        metrics_lines.append(f'<success_rate value="{xml_escape(str(round(t.success_rate, 6)))}"/>')
                    if any(v is not None for v in [t.pos_feedback_count, t.neg_feedback_count]):
                        metrics_lines.append(
                            f'<feedback pos="{t.pos_feedback_count or 0}" neg="{t.neg_feedback_count or 0}"/>'
                        )
                    if t.last_used_at:
                        metrics_lines.append(f'<last_used_at value="{xml_escape(t.last_used_at)}"/>')
                    if t.last_feedback_at:
                        metrics_lines.append(f'<last_feedback_at value="{xml_escape(t.last_feedback_at)}"/>')
                metrics_xml = xml_tag("metrics", "\n".join(metrics_lines)) if metrics_lines else ""
                inner = "\n".join(filter(None, [xml_tag("columns", cols), xml_tag("pks", pks), xml_tag("fks", fks), metrics_xml]))
                tables_xml.append(xml_tag("table", inner, {"name": t.name}))
            content_parts = []
            if self.info.context:
                content_parts.append(xml_tag("context", xml_escape(self.info.context)))
            content_parts.append("\n\n".join(tables_xml))
            return xml_tag(self.tag_name, "\n".join(content_parts), {"name": self.info.name, "type": self.info.type, "id": self.info.id})

    data_sources: List[DataSource] = []

    def render(self) -> str:
        return xml_tag(self.tag_name, "\n\n".join(ds.render() for ds in self.data_sources or []))


