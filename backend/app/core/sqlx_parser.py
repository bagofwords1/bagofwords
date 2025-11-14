import re
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple


class SQLXResourceExtractor:
    """
    Lightweight, static extractor for Dataform SQLX projects.

    This does *not* execute or compile the project. It performs a best-effort
    parse of `.sqlx` files to expose:
      - table-like actions (tables/views/incrementals)
      - config metadata (tags, uniqueKey, warehouse-specific bits)
      - columns and docs when statically declared
      - dependencies via `ref("...")` calls
      - pre_operations blocks for incremental logic
    """

    def __init__(self, project_dir: str):
        self.project_dir = Path(project_dir)
        self.resources: Dict[str, List[dict]] = {
            "tables": [],
            "assertions": [],
            "operations": [],
            "declarations": [],
        }
        self.columns_by_resource = defaultdict(list)
        self.docs_by_resource = defaultdict(str)

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def extract_all_resources(self) -> Tuple[Dict[str, List[dict]], dict, dict]:
        """Extract all SQLX resources from the project directory."""
        self._parse_sqlx_files()

        # Clean paths relative to project root
        for resource_type in self.resources:
            for resource in self.resources[resource_type]:
                path_value = resource.get("path")
                if not path_value or not isinstance(path_value, str):
                    continue
                try:
                    absolute_path = Path(path_value).resolve()
                    relative_path = absolute_path.relative_to(self.project_dir.resolve())
                    resource["path"] = str(relative_path)
                except ValueError:
                    # If path is not within project_dir, keep the original
                    continue

        return self.resources, self.columns_by_resource, self.docs_by_resource

    def get_summary(self) -> Dict[str, int]:
        """Return a simple count of resources per type."""
        return {resource_type: len(items) for resource_type, items in self.resources.items()}

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _parse_sqlx_files(self) -> None:
        """Discover and parse all `.sqlx` files under the project directory."""
        sqlx_files = list(self.project_dir.glob("**/*.sqlx"))

        for sqlx_file in sqlx_files:
            try:
                with sqlx_file.open("r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue

            name = sqlx_file.stem

            config_block, remainder_after_config = self._extract_block(content, "config")
            pre_ops_block, sql_body = self._extract_block(remainder_after_config, "pre_operations")

            config_info = self._parse_config_block(config_block)
            depends_on = self._scan_dependencies(content)

            # Build table-like resource (we only handle tables/views/incrementals for now)
            table_item: dict = {
                "name": name,
                "path": str(sqlx_file),
                # Use "dataform_*" as the canonical resource_type prefix
                "type": "dataform_table",
                "materialization": config_info.get("type", ""),
                "description": config_info.get("description", ""),
                "tags": config_info.get("tags", []),
                "schema_expr": config_info.get("schema_expr", ""),
                "unique_key": config_info.get("unique_key", []),
                "partition_by": config_info.get("partition_by"),
                "cluster_by": config_info.get("cluster_by", []),
                "assertions": config_info.get("assertions", {}),
                "sql_body": sql_body.strip() if isinstance(sql_body, str) else "",
                "pre_operations_raw": pre_ops_block.strip() if isinstance(pre_ops_block, str) else "",
                "sqlx_source_snippet": content,
                "depends_on": depends_on,
                "raw_config": config_info.get("raw_config", {}),
            }

            columns = config_info.get("columns", [])
            if columns:
                table_item["columns"] = columns

            self.resources["tables"].append(table_item)

            # Maintain auxiliary maps for columns/docs, similar to DBT parser
            if columns:
                key = f"dataform_table.{name}"
                for col in columns:
                    self.columns_by_resource[key].append(col)

            description = table_item.get("description")
            if description:
                self.docs_by_resource[f"dataform_table.{name}"] = description

    def _extract_block(self, content: str, keyword: str) -> Tuple[str, str]:
        """
        Extract a top-level `{ ... }` block that follows a keyword, returning:
          (block_contents_without_outer_braces, remaining_text)

        If the keyword is not found, returns ("", content).
        """
        pattern = re.compile(rf"{keyword}\s*\{{", re.IGNORECASE)
        match = pattern.search(content)
        if not match:
            return "", content

        start_index = match.start()
        brace_start = content.find("{", match.start())
        if brace_start == -1:
            return "", content

        depth = 0
        end_index = None
        for idx in range(brace_start, len(content)):
            ch = content[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = idx
                    break

        if end_index is None:
            # Unbalanced braces; return the whole content as remainder
            return "", content

        block_inner = content[brace_start + 1 : end_index]
        remainder = content[:start_index] + content[end_index + 1 :]
        return block_inner.strip(), remainder

    def _parse_config_block(self, block: str) -> Dict:
        """
        Best-effort parser for the SQLX `config { ... }` block.

        This is intentionally conservative: it pulls out a few common fields
        for downstream consumers and also returns a `raw_config` structure
        for later inspection.
        """
        if not block:
            return {
                "raw_config": {},
                "columns": [],
                "tags": [],
                "assertions": {},
                "cluster_by": [],
                "unique_key": [],
            }

        lines = [line.strip() for line in block.splitlines() if line.strip() and not line.strip().startswith("//")]

        config: Dict = {
            "raw_config": {"__raw_text__": block},
            "columns": [],
            "tags": [],
            "assertions": {},
            "cluster_by": [],
            "unique_key": [],
        }

        # Simple regex helpers
        def _extract_string_value(pattern: str) -> str:
            for line in lines:
                m = re.search(pattern, line)
                if m:
                    return m.group(1)
            return ""

        def _extract_string_list(pattern: str) -> List[str]:
            for line in lines:
                m = re.search(pattern, line)
                if not m:
                    continue
                inner = m.group(1)
                parts = [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
                return parts
            return []

        # type: "table" | "incremental" | "view"
        config["type"] = _extract_string_value(r'type\s*:\s*["\']([^"\']+)["\']')

        # description: "..."
        config["description"] = _extract_string_value(r'description\s*:\s*["\']([^"\']+)["\']')

        # schema: utils.getWriteSchema() or literal string
        schema_expr = ""
        for line in lines:
            if line.startswith("schema"):
                # everything after ':' up to trailing comma
                parts = line.split(":", 1)
                if len(parts) == 2:
                    schema_expr = parts[1].rstrip(",").strip()
                break
        config["schema_expr"] = schema_expr

        # tags: ["a", "b"]
        config["tags"] = _extract_string_list(r"tags\s*:\s*\[([^\]]*)\]")

        # uniqueKey: ["col1", "col2"]
        unique_key = _extract_string_list(r"uniqueKey\s*:\s*\[([^\]]*)\]")
        if not unique_key:
            # Sometimes under assertions.uniqueKey
            unique_key = _extract_string_list(r"uniqueKey\s*:\s*\[([^\]]*)\]")
        config["unique_key"] = unique_key

        # BigQuery / warehouse-specific
        config["partition_by"] = _extract_string_value(r'partitionBy\s*:\s*["\']([^"\']+)["\']')
        config["cluster_by"] = _extract_string_list(r"clusterBy\s*:\s*\[([^\]]*)\]")

        # Columns block: naive parse for `columns: { name: "desc", ... }`
        columns: List[dict] = []
        columns_block = self._extract_inline_block(block, "columns")
        if columns_block:
            col_lines = [
                ln.strip() for ln in columns_block.splitlines() if ln.strip() and not ln.strip().startswith("//")
            ]
            for ln in col_lines:
                # pattern: key: "description",
                m = re.match(r"([A-Za-z0-9_]+)\s*:\s*['\"]([^'\"]*)['\"]", ln)
                if not m:
                    continue
                col_name = m.group(1)
                description = m.group(2)
                columns.append(
                    {
                        "name": col_name,
                        "description": description,
                        "data_type": "",
                        "meta": {},
                    }
                )
        config["columns"] = columns

        # Assertions block: keep as raw text for now
        assertions_block = self._extract_inline_block(block, "assertions")
        if assertions_block:
            config["assertions"]["__raw_text__"] = assertions_block

        return config

    def _extract_inline_block(self, content: str, key: str) -> str:
        """
        Extract a `{ ... }` block that is associated with a key inside another
        block, e.g. `columns: { ... }`.
        """
        pattern = re.compile(rf"{key}\s*:\s*\{{", re.IGNORECASE)
        match = pattern.search(content)
        if not match:
            return ""

        brace_start = content.find("{", match.start())
        if brace_start == -1:
            return ""

        depth = 0
        end_index = None
        for idx in range(brace_start, len(content)):
            ch = content[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = idx
                    break

        if end_index is None:
            return ""

        return content[brace_start + 1 : end_index].strip()

    def _scan_dependencies(self, content: str) -> List[str]:
        """Scan for `ref("...")` calls and return a de-duplicated list of names."""
        refs = re.findall(r'ref\(\s*["\']([^"\']+)["\']\s*\)', content)
        # Preserve order while removing duplicates
        seen = set()
        ordered_refs: List[str] = []
        for r in refs:
            if r not in seen:
                seen.add(r)
                ordered_refs.append(r)
        return ordered_refs


