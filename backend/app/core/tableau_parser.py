import logging
import zipfile
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Optional
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _strip_namespace(tag: str) -> str:
    if '}' in tag:
        return tag.split('}', 1)[1]
    return tag


class TableauTDSResourceExtractor:
    """
    Extract resources from Tableau datasource files (.tds, .tdsx).

    Output structure mirrors other extractors:
    - resources: dict of arrays with top-level resource objects
    - columns_by_resource: defaultdict(list)
    - docs_by_resource: defaultdict(str)
    """

    def __init__(self, project_dir: str | Path):
        self.project_dir = Path(project_dir)
        self.resources: Dict[str, List[Dict[str, Any]]] = {
            'tableau_datasources': [],
            # Connections and fields are not emitted as standalone resources; they are nested
            'tableau_joins': [],
            'tableau_custom_sql': [],
            'tableau_parameters': [],
        }
        self.columns_by_resource: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.docs_by_resource: Dict[str, str] = defaultdict(str)

    def extract_all_resources(self) -> Tuple[Dict[str, Any], Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
        """Walk project_dir, parse .tds and .tdsx files, and populate resources."""
        tds_files = list(self.project_dir.glob('**/*.tds'))
        tdsx_files = list(self.project_dir.glob('**/*.tdsx'))

        for tds_path in tds_files:
            try:
                tree = ET.parse(tds_path)
                root = tree.getroot()
                rel_path = str(tds_path.relative_to(self.project_dir))
                self._parse_tds_tree(root, rel_path, file_hint=tds_path.stem)
            except Exception as e:
                logger.error(f"Error parsing TDS {tds_path}: {e}")

        for tdsx_path in tdsx_files:
            try:
                with zipfile.ZipFile(tdsx_path, 'r') as zf:
                    # Find embedded .tds (there may be one; if multiple, parse all)
                    for member in zf.namelist():
                        if member.lower().endswith('.tds'):
                            try:
                                data = zf.read(member)
                                root = ET.fromstring(data)
                                # Represent internal path in a virtualized way
                                rel_tdsx = str(Path(tdsx_path).relative_to(self.project_dir))
                                rel_path = f"{rel_tdsx}#{member}"
                                self._parse_tds_tree(root, rel_path, file_hint=Path(member).stem)
                            except Exception as inner_e:
                                logger.error(f"Error parsing embedded TDS {member} in {tdsx_path}: {inner_e}")
            except Exception as e:
                logger.error(f"Error opening TDSX {tdsx_path}: {e}")

        return self.resources, self.columns_by_resource, self.docs_by_resource

    def _parse_tds_tree(self, root: ET.Element, rel_path: str, file_hint: Optional[str] = None) -> None:
        """Parse a TDS XML root element, populating resources."""
        tag = _strip_namespace(root.tag)
        if tag != 'datasource':
            # Some files may wrap datasource under a different root
            ds = root.find('.//*')
            if ds is None or _strip_namespace(ds.tag) != 'datasource':
                logger.warning(f"Skipping file {rel_path}: no <datasource> root found")
                return
            datasource_el = ds
        else:
            datasource_el = root

        ds_name = datasource_el.get('name') or datasource_el.get('caption') or file_hint or 'datasource'
        ds_description = datasource_el.get('caption') or ''

        datasource_obj = {
            'name': ds_name,
            'path': rel_path,
            'resource_type': 'tableau_datasource',
            'description': ds_description,
            'connection_type': datasource_el.get('class'),
            'sql_content': None,
            'raw_data': {'attrs': dict(datasource_el.attrib)},
            'depends_on': [],
            'columns': [],
        }
        self.resources['tableau_datasources'].append(datasource_obj)
        if ds_description:
            self.docs_by_resource[f"tableau_datasource.{ds_name}"] = ds_description

        # Connections are intentionally not emitted as standalone resources

        # Fields: <column> (nested under datasource via columns_by_resource only)
        for col in datasource_el.findall('.//'):  # Walk all and filter by tag name
            if _strip_namespace(col.tag) != 'column':
                continue
            field_name = col.get('name') or col.get('caption')
            if not field_name:
                continue
            # Normalize name without brackets if present like [field]
            normalized_name = field_name.strip('[]')
            data_type = col.get('datatype')
            role = col.get('role')
            default_agg = col.get('aggregation') or col.get('default-aggregation')
            calc_el = next((child for child in list(col) if _strip_namespace(child.tag) == 'calculation'), None)
            is_calculated = calc_el is not None
            formula = calc_el.get('formula') if is_calculated else None

            self.columns_by_resource[f"tableau_datasource.{ds_name}"].append({
                'name': normalized_name,
                'description': col.get('caption') or '',
                'data_type': data_type,
                'role': role,
                'default_aggregation': default_agg,
                'is_calculated': bool(is_calculated),
                'formula': formula,
                'folder': col.get('folder') or None,
                'tags': [],
                'meta': {},
            })

        # Relations: joins and custom SQL
        for rel in datasource_el.findall('.//'):
            if _strip_namespace(rel.tag) != 'relation':
                continue
            rel_type = rel.get('type')
            if rel_type == 'join':
                # Attempt to extract left/right children relations' names
                children = [child for child in list(rel) if _strip_namespace(child.tag) == 'relation']
                left = children[0].get('table') if len(children) > 0 else None
                right = children[1].get('table') if len(children) > 1 else None
                clause_el = rel.find('.//clause')
                clause_text = clause_el.text.strip() if clause_el is not None and clause_el.text else None
                join_obj = {
                    'name': f"{ds_name}_join_{len(self.resources['tableau_joins'])+1}",
                    'path': rel_path,
                    'resource_type': 'tableau_join',
                    'description': '',
                    'datasource': ds_name,
                    'left': left,
                    'right': right,
                    'clause': clause_text,
                    'join_type': rel.get('join') or rel.get('outer') or None,
                    'raw_data': {'attrs': dict(rel.attrib)},
                    'depends_on': [f"tableau_datasource.{ds_name}"],
                    'columns': [],
                }
                self.resources['tableau_joins'].append(join_obj)
            elif rel_type == 'text':
                # Custom SQL relation
                text_node = next((child for child in list(rel) if _strip_namespace(child.tag) == 'text'), None)
                if text_node is not None:
                    # Concatenate all text within the <text> node (handles CDATA)
                    sql_text = ''.join(text_node.itertext()).strip()
                else:
                    sql_text = (rel.text or '').strip()
                custom_sql_obj = {
                    'name': f"{ds_name}_custom_sql_{len(self.resources['tableau_custom_sql'])+1}",
                    'path': rel_path,
                    'resource_type': 'tableau_custom_sql',
                    'description': '',
                    'datasource': ds_name,
                    'sql_content': sql_text if sql_text else None,
                    'raw_data': {'attrs': dict(rel.attrib), 'sql_len': len(sql_text)},
                    'depends_on': [f"tableau_datasource.{ds_name}"],
                    'columns': [],
                }
                self.resources['tableau_custom_sql'].append(custom_sql_obj)
                # Also attach SQL to the datasource for UI convenience
                if sql_text:
                    if datasource_obj.get('sql_content'):
                        datasource_obj['sql_content'] += "\n\n-- Next relation --\n\n" + sql_text
                    else:
                        datasource_obj['sql_content'] = sql_text

        # Parameters (best-effort: some TDS files may not contain explicit parameter nodes)
        for param in datasource_el.findall('.//parameter'):
            if _strip_namespace(param.tag) != 'parameter':
                continue
            pname = param.get('name')
            if not pname:
                continue
            ptype = param.get('datatype') or param.get('type')
            pdesc = param.get('caption') or ''
            param_obj = {
                'name': pname,
                'path': rel_path,
                'resource_type': 'tableau_parameter',
                'description': pdesc,
                'datasource': ds_name,
                'data_type': ptype,
                'current_value': param.get('value'),
                'allowed_values': [],
                'raw_data': {'attrs': dict(param.attrib)},
                'depends_on': [f"tableau_datasource.{ds_name}"],
                'columns': [],
            }
            self.resources['tableau_parameters'].append(param_obj)

    def get_summary(self) -> Dict[str, int]:
        summary: Dict[str, int] = {}
        for key, items in self.resources.items():
            summary[key] = len(items)
        return summary


