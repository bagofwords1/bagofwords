from typing import ClassVar, List, Optional, Literal
from app.ai.context.sections.base import ContextSection, xml_tag, xml_escape
from app.ai.context.formatters.resources_xml import format_resource_dict_xml


class ResourcesSection(ContextSection):
    tag_name: ClassVar[str] = "resources"

    # Back-compat payload: pre-rendered string from older builder
    content: str = ""

    class Repository(ContextSection):
        tag_name: ClassVar[str] = "repository"
        # Minimal identity (align to data source or git repo as producer chooses)
        name: str
        id: Optional[str] = None
        data_source_id: Optional[str] = None
        # Resources held as simple dicts with keys: name, type, path?, desc?
        resources: List[dict] = []

        def _render_topk_resources_full(self, top_k: int) -> str:
            items: List[str] = []
            for r in (self.resources or [])[: max(0, top_k)]:
                # If pre-rendered XML exists, use it; otherwise format from dict
                pre = r.get("formatted_xml") if isinstance(r, dict) else None
                try:
                    xml_repr = str(pre) if pre else format_resource_dict_xml(r)
                except Exception:
                    # Fallback to a minimal tag
                    attrs = {"name": r.get("name", ""), "type": r.get("type", r.get("resource_type", ""))}
                    if r.get("path"):
                        attrs["path"] = r.get("path")
                    xml_repr = xml_tag("resource", "", attrs)
                items.append(xml_repr)
            if not items:
                return ""
            return xml_tag("resources", "\n".join(items))

        def _render_names_index(self, index_limit: int = 200) -> str:
            all_items = list(self.resources or [])
            if not all_items:
                return ""
            cap = max(0, index_limit)
            lines: List[str] = []
            for r in all_items[: cap if cap > 0 else len(all_items)]:
                attrs = {"name": r.get("name", ""), "type": r.get("type", "")}
                if r.get("path"):
                    attrs["path"] = r.get("path")
                # emit self-closing <item .../>
                attrs_str = "".join(f' {k}="{xml_escape(str(v))}"' for k, v in attrs.items())
                lines.append(f"<item{attrs_str}/>")
            idx_attrs = {"count": str(len(all_items))}
            if cap > 0 and len(all_items) > cap:
                idx_attrs["truncated"] = "true"
            return xml_tag("index", "\n".join(lines), idx_attrs)

        def render_combined(self, top_k_per_repo: int = 10, index_limit: int = 200, include_index: bool = True) -> str:
            parts: List[str] = []
            sample_xml = self._render_topk_resources_full(top_k_per_repo)
            if sample_xml:
                parts.append(xml_tag("sample", sample_xml, {"k": str(top_k_per_repo)}))
            if include_index:
                index_xml = self._render_names_index(index_limit)
                if index_xml:
                    parts.append(index_xml)
            attrs = {"name": self.name}
            if self.id:
                attrs["id"] = self.id
            if self.data_source_id:
                attrs["data_source_id"] = self.data_source_id
            attrs["total_resources"] = str(len(self.resources or []))
            return xml_tag(self.tag_name, "\n".join(parts), attrs)

    repositories: List[Repository] = []

    def render(self, format: Literal["full","gist","names","digest"] = "full") -> str:
        # Structured path
        if self.repositories:
            if format == "names":
                # names: just indices per repo
                chunks = []
                for repo in (self.repositories or []):
                    chunks.append(repo._render_names_index())
                return xml_tag(self.tag_name, "".join(chunks))
            # full/gist/digest → use combined by default (kept simple for now)
            return self.render_combined()

        # Backwards compatibility: fall back to pre-rendered content
        return xml_tag(self.tag_name, self.content or "")

    def render_combined(self, top_k_per_repo: int = 10, index_limit: int = 200, include_index: bool = True) -> str:
        if not (self.repositories or []):
            return xml_tag(self.tag_name, self.content or "")
        repo_chunks: List[str] = []
        for repo in (self.repositories or []):
            repo_chunks.append(repo.render_combined(top_k_per_repo=top_k_per_repo, index_limit=index_limit, include_index=include_index))
        return xml_tag(self.tag_name, "".join(repo_chunks))


