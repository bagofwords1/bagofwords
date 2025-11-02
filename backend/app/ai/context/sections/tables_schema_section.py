from typing import ClassVar, List, Optional, Literal
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

                # ignored for now
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
                # Optional metadata (compact attributes)
                metadata_xml = ""
                try:
                    tj = (t.metadata_json or {}).get("tableau", {}) if isinstance(t.metadata_json, dict) else {}
                    attrs = {}
                    for k in ("datasourceLuid", "projectName", "name"):
                        v = tj.get(k)
                        if v is not None:
                            attrs[k] = v
                    if attrs:
                        metadata_xml = xml_tag("metadata", "", attrs)
                except Exception:
                    metadata_xml = ""
                inner = "\n".join(filter(None, [xml_tag("columns", cols), metadata_xml, metrics_xml]))
                tables_xml.append(xml_tag("table", inner, {"name": t.name}))
            content_parts = []
            if self.info.context:
                content_parts.append(xml_tag("context", xml_escape(self.info.context)))
            content_parts.append("\n\n".join(tables_xml))
            return xml_tag(self.tag_name, "\n".join(content_parts), {"name": self.info.name, "type": self.info.type, "id": self.info.id})

        # Compact renderers for gist/index/digest
        def _render_gist(self, columns_per_table: int = 2) -> str:
            table_tags: List[str] = []
            for t in (self.tables or []):
                # Per-table metrics: score, usage, columns count
                try:
                    score_val = getattr(t, 'score', None)
                    if score_val is not None:
                        try:
                            score_str = str(round(float(score_val), 2))
                        except Exception:
                            score_str = str(score_val)
                    else:
                        score_str = None
                except Exception:
                    score_str = None
                try:
                    usage_val = getattr(t, 'usage_count', None)
                    usage_str = str(int(usage_val)) if usage_val is not None else None
                except Exception:
                    usage_str = None
                try:
                    cols_count = len(t.columns or [])
                except Exception:
                    cols_count = 0

                meta_parts: List[str] = []
                if score_str is not None:
                    meta_parts.append(f"score: {score_str}")
                if usage_str is not None:
                    meta_parts.append(f"usage: {usage_str}")
                meta_parts.append(f"{cols_count} columns")
                meta_text = f"({', '.join(meta_parts)})" if meta_parts else None

                attrs = {"n": t.name}
                if meta_text:
                    attrs["meta"] = meta_text
                table_tags.append(xml_tag("t", "", attrs))
            # Skip empty data sources in gist
            if not table_tags:
                return ""
            label = xml_tag("label", "Sample top 10 tables for reference")
            inner = label + xml_tag("tables", "".join(table_tags))
            attrs = {"name": self.info.name, "type": self.info.type, "id": self.info.id, "sample": str(len(table_tags))}
            if self.info.context:
                attrs["desc"] = xml_escape(self.info.context)
            return xml_tag("data_source", inner, attrs)

        def _render_names(self) -> str:
            names = [getattr(t, 'name', '') for t in (self.tables or [])]
            # Skip empty data sources
            if not names:
                return ""
            # Ultra-compact: count + comma-separated list on one line
            label = xml_tag("label", "Index of all tables in database")
            payload = label + xml_tag("count", str(len(names))) + xml_tag("list", ", ".join(names))
            return xml_tag("data_source", payload, {"name": self.info.name, "type": self.info.type, "id": self.info.id})

        def _render_digest(self) -> str:
            first_five = [t.name for t in (self.tables or [])][:5]
            payload = xml_tag("count", str(len(self.tables or []))) + xml_tag("top", ", ".join(first_five))
            return xml_tag(self.tag_name, payload, {"name": self.info.name, "type": self.info.type, "id": self.info.id})

        def _render_topk_tables_full(self, top_k: int) -> str:
            tables_xml: List[str] = []
            for t in (self.tables or [])[: max(0, top_k)]:
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
                attrs = {"name": t.name, "cols": str(len(t.columns or []))}
                try:
                    if getattr(t, 'score', None) is not None:
                        attrs["score"] = str(round(float(getattr(t, 'score')), 2))
                except Exception:
                    pass
                try:
                    if getattr(t, 'usage_count', None) is not None:
                        attrs["usage"] = str(int(getattr(t, 'usage_count') or 0))
                except Exception:
                    pass
                inner = "\n".join(filter(None, [xml_tag("columns", cols), xml_tag("pks", pks) if pks else "", xml_tag("fks", fks) if fks else ""]))
                tables_xml.append(xml_tag("table", inner, attrs))
            if not tables_xml:
                return ""
            return xml_tag("tables", "\n".join(tables_xml))

        def _render_names_index(self, index_limit: int = 200) -> str:
            tables = list(self.tables or [])
            if not tables:
                return ""
            # Build nested <item> elements with minimal metrics
            items_xml: List[str] = []
            cap = max(0, index_limit)
            for t in tables[:cap if cap > 0 else len(tables)]:
                attrs = {
                    "name": t.name,
                    "cols": str(len(getattr(t, 'columns', []) or [])),
                }
                try:
                    if getattr(t, 'score', None) is not None:
                        attrs["score"] = str(round(float(getattr(t, 'score')), 2))
                except Exception:
                    pass
                items_xml.append(xml_tag("item", "", attrs))
            idx_attrs = {"count": str(len(tables))}
            if cap > 0 and len(tables) > cap:
                idx_attrs["truncated"] = "true"
            return xml_tag("index", "".join(items_xml), idx_attrs)

    data_sources: List[DataSource] = []

    def render(self, format: Literal["full","gist","names","digest"] = "full", columns_per_table: int = 2) -> str:
        if format == "full":
            return xml_tag(self.tag_name, "\n\n".join(ds.render() for ds in self.data_sources or []))
        if format == "gist":
            # Compact gist with per-table metrics (score, usage, columns)
            return xml_tag(self.tag_name, "".join(ds._render_gist(columns_per_table) for ds in self.data_sources or []))
        if format == "names":
            return xml_tag(self.tag_name, "".join(ds._render_names() for ds in self.data_sources or []))
        if format == "digest":
            return xml_tag(self.tag_name, "".join(ds._render_digest() for ds in self.data_sources or []))
        return xml_tag(self.tag_name, "\n\n".join(ds.render() for ds in self.data_sources or []))

    def render_combined(self, top_k_per_ds: int = 10, index_limit: int = 200) -> str:
        ds_chunks: List[str] = []
        for ds in (self.data_sources or []):
            sample_xml = ds._render_topk_tables_full(top_k_per_ds)
            index_xml = ds._render_names_index(index_limit)
            if not (sample_xml or index_xml):
                continue
            inner_parts: List[str] = []
            if getattr(ds.info, 'context', None):
                inner_parts.append(xml_tag("description", xml_escape(ds.info.context)))
            if sample_xml:
                inner_parts.append(xml_tag("sample", sample_xml, {"k": str(top_k_per_ds)}))
            if index_xml:
                inner_parts.append(index_xml)
            attrs = {
                "name": ds.info.name,
                "type": ds.info.type,
                "id": ds.info.id,
                "total_tables": str(len(getattr(ds, 'tables', []) or [])),
            }
            ds_chunks.append(xml_tag("data_source", "".join(inner_parts), attrs))
        return xml_tag(self.tag_name, "".join(ds_chunks))


