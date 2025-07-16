import os
import lkml
import glob
import re
from pathlib import Path
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class LookMLResourceExtractor:
    """
    Extracts resources (models, explores, views, dimensions, measures, etc.) 
    from LookML files within a project directory.
    """
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        # Standardized output structure - only top-level resources
        self.resources = {
            'lookml_models': [],
            'lookml_explores': [],
            'lookml_views': [],
            'lookml_joins': [],
        }
        # Using similar structure as DBT for potential future unified handling
        self.columns_by_resource = defaultdict(list) # Stores dimensions/measures per view/explore
        self.docs_by_resource = defaultdict(str)    # Might store descriptions here

    def extract_all_resources(self):
        """Extract all LookML resources from the project directory."""
        lookml_files = list(self.project_dir.glob('**/*.lkml'))
        
        for lkml_file in lookml_files:
            try:
                with open(lkml_file, 'r') as f:
                    parsed_lookml = lkml.load(f)
                self._parse_lookml_content(parsed_lookml, str(lkml_file))
                
            except Exception as e:
                logger.error(f"Error parsing LookML file {lkml_file}: {e}")
        
        # Post-process or aggregate if needed, e.g., linking explores to models
        self._link_explores_to_models()
        return self.resources, self.columns_by_resource, self.docs_by_resource

    def _parse_lookml_content(self, content, file_path):
        """Parses the content of a single LookML file."""
        if not isinstance(content, dict):
            return

        # Check if this is a model file (has connection field)
        if 'connection' in content:
            # This is a model file - create a model from the file itself
            model_name = Path(file_path).stem  # Use filename as model name
            model_data = {
                'name': model_name,
                'connection': content.get('connection'),
                'explores': content.get('explores', []),
                'includes': content.get('includes', []),
                # Add other model-level fields
                'label': content.get('label'),
                'description': content.get('description'),
            }
            self._extract_model(model_data, file_path)
        
        # Also check for explicit models array (less common)
        elif 'models' in content and isinstance(content.get('models'), list):
            for model_data in content['models']:
                if isinstance(model_data, dict):
                    self._extract_model(model_data, file_path)
        
        # Check for views (standalone view files)
        if 'views' in content and isinstance(content.get('views'), list):
            for view_data in content['views']:
                if isinstance(view_data, dict):
                    self._extract_view(view_data, file_path)
        
        # Check for standalone explores (only if this is NOT a model file)
        if 'explores' in content and isinstance(content.get('explores'), list) and 'connection' not in content:
            logger.debug(f"Found standalone explores in {file_path}, which are not currently processed independently of a model.")
            # The _extract_explore method requires a model_name, so we cannot process
            # standalone explores without more logic to determine their parent model.
            pass

    def _extract_model(self, model_data, file_path):
        """Extracts information from a LookML model definition."""
        if not isinstance(model_data, dict):
             logger.warning(f"Skipping invalid model definition in {file_path}: {model_data}")
             return

        # Use filename as model name if no name is provided
        model_name = model_data.get('name') or Path(file_path).stem
        model_obj = {
            'name': model_name,
            'path': file_path,
            'resource_type': 'lookml_model', # Use the unified naming scheme
            'label': model_data.get('label'),
            'connection': model_data.get('connection'),
            # Store the raw LookML for the model itself, excluding explores/joins for now
            'raw_data': {k: v for k, v in model_data.items() if k not in ['explores', 'access_grants']}, 
            'depends_on': [], # Placeholder for derived dependencies
            'columns': [], # Will be populated by explores below
            'description': model_data.get('description') # Check if description exists
        }
        
        # Extract explores defined within this model and store them as columns
        for explore_data in model_data.get('explores', []):
            explore_obj = self._extract_explore_as_column(explore_data, model_name, file_path)
            if explore_obj:
                model_obj['columns'].append(explore_obj)
                # Also store in columns_by_resource for compatibility
                resource_key = f"lookml_model.{model_name}"
                self.columns_by_resource[resource_key].append(explore_obj)

        self.resources['lookml_models'].append(model_obj)
        
        if model_obj['description']:
             self.docs_by_resource[f"lookml_model.{model_name}"] = model_obj['description']

    def _extract_explore_as_column(self, explore_data, model_name, file_path):
        """Extracts information from a LookML explore definition and returns it as a column object."""
        if not isinstance(explore_data, dict) or 'name' not in explore_data:
             logger.warning(f"Skipping invalid explore definition in {file_path} (model: {model_name}): {explore_data}")
             return None

        explore_name = explore_data['name']
        explore_obj = {
            'name': explore_name,  # Just the explore name, not the full path
            'field_name': explore_name, # Original field name
            'model_name': model_name, # Link back to the parent model
            'path': file_path,
            'resource_type': 'lookml_explore', # Keep the resource type for identification
            'label': explore_data.get('label'),
            'view_name': explore_data.get('view_name'),
            'description': explore_data.get('description'),
            'raw_data': {k: v for k, v in explore_data.items() if k != 'joins'}, # Exclude joins initially
            'depends_on': [f"lookml_model.{model_name}"], # Depends on its model
            'columns': [], # Explores can have their own columns (joins)
            'type': 'explore', # Add a type field to distinguish from dimensions/measures
        }
        
        # Add dependency on the base view if specified
        if explore_obj['view_name']:
             explore_obj['depends_on'].append(f"lookml_view.{explore_obj['view_name']}")

        # Extract joins defined within this explore and store them as sub-columns
        for join_data in explore_data.get('joins', []):
            join_obj = self._extract_join_as_column(join_data, explore_name, model_name, file_path)
            if join_obj:
                explore_obj['columns'].append(join_obj)
                # Also store in columns_by_resource for compatibility
                explore_key = f"lookml_explore.{explore_name}"
                self.columns_by_resource[explore_key].append(join_obj)
                
                # Add dependency based on join
                join_source_view = join_data.get('from') or join_data.get('name') # Looker uses 'from' or just the name
                if join_source_view:
                     explore_obj['depends_on'].append(f"lookml_view.{join_source_view}")

        if explore_obj['description']:
            self.docs_by_resource[f"lookml_explore.{explore_name}"] = explore_obj['description']
            
        return explore_obj

    def _extract_join_as_column(self, join_data, explore_name, model_name, file_path):
        """Extracts information from a LookML join definition and returns it as a column object."""
        if not isinstance(join_data, dict) or 'name' not in join_data:
            logger.warning(f"Skipping invalid join definition in {file_path} (explore: {explore_name}): {join_data}")
            return None
            
        join_name = join_data['name'] # Often the name of the view being joined
        join_obj = {
            'name': join_name,  # Just the join name
            'field_name': join_name, # Original field name
            'explore_name': explore_name,
            'model_name': model_name,
            'path': file_path,
            'resource_type': 'lookml_join',
            'join_view_name': join_name, # The view being joined
            'relationship': join_data.get('relationship'),
            'type': join_data.get('type'),
            'sql_on': join_data.get('sql_on'),
            'foreign_key': join_data.get('foreign_key'),
             # Raw data for the join itself
            'raw_data': join_data,
            'depends_on': [f"lookml_explore.{explore_name}", f"lookml_view.{join_name}"],
            'columns': [], # Joins don't have sub-columns
            'description': join_data.get('description'), # Joins can have descriptions
            'field_type': 'join', # Add a field_type to distinguish from dimensions/measures
        }
        
        if join_obj['description']:
             self.docs_by_resource[f"lookml_join.{join_obj['name']}"] = join_obj['description']
             
        return join_obj


    def _extract_view(self, view_data, file_path):
        """Extracts information from a LookML view definition."""
        if not isinstance(view_data, dict) or 'name' not in view_data:
            logger.warning(f"Skipping invalid view definition in {file_path}: {view_data}")
            return

        view_name = view_data['name']
        view_obj = {
            'name': view_name,
            'path': file_path,
            'resource_type': 'lookml_view',
            'label': view_data.get('label'),
            'sql_table_name': view_data.get('sql_table_name'),
            'derived_table': view_data.get('derived_table'),
            'description': view_data.get('description'),
            # Store raw_data but exclude *both* singular & plural field blocks
            'raw_data': {k: v for k, v in view_data.items()
                         if k not in [
                             'dimension', 'dimensions',
                             'measure', 'measures',
                             'dimension_group', 'dimension_groups',
                             'filter_fields', 'parameters']},
            'depends_on': [], # Dependencies added based on derived tables or extended views
            'columns': [], # Populated by dimensions and measures below
        }

        if view_data.get('extends'):
            extends_list = view_data.get('extends', [])
            view_obj['depends_on'].extend([f"lookml_view.{ext}" for ext in extends_list])
        
        # Basic derived table SQL dependency check (can be improved)
        if view_obj['derived_table'] and 'sql' in view_obj['derived_table']:
             # Simple regex, might need refinement
             refs = re.findall(r'\$\{([^}]+)\}\.SQL_TABLE_NAME', view_obj['derived_table']['sql'])
             view_obj['depends_on'].extend([f"lookml_view.{ref}" for ref in refs])

        # Extract dimensions, measures, etc. for this view
        resource_key = f"lookml_view.{view_name}"
        
        # Add debug logging to see what's in view_data
        logger.debug(f"Processing view {view_name} from {file_path}")
        logger.debug(f"View data keys: {list(view_data.keys())}")
        
        # ---- dimensions ----
        for key in ('dimension', 'dimensions'):
            block = view_data.get(key)
            if not block:
                continue
            logger.debug(f"Found {key} block: {block}")
            # lkml gives a dict when there is only one dimension
            dim_iter = block if isinstance(block, list) else [block]
            for dim_data in dim_iter:
                col_obj = self._extract_field(dim_data, 'dimension', view_name, file_path)
                if col_obj:
                    view_obj['columns'].append(col_obj)  # Add to view's columns
                    self.columns_by_resource[resource_key].append(col_obj)  # Also store in columns_by_resource for compatibility
        
        # ---- measures ----
        for key in ('measure', 'measures'):
            block = view_data.get(key)
            if not block:
                continue
            logger.debug(f"Found {key} block: {block}")
            mea_iter = block if isinstance(block, list) else [block]
            for mea_data in mea_iter:
                col_obj = self._extract_field(mea_data, 'measure', view_name, file_path)
                if col_obj:
                    view_obj['columns'].append(col_obj)  # Add to view's columns
                    self.columns_by_resource[resource_key].append(col_obj)  # Also store in columns_by_resource for compatibility
        
        # ---- dimension groups (timeframes) ----
        for key in ('dimension_group', 'dimension_groups'):
            block = view_data.get(key)
            if not block:
                continue
            logger.debug(f"Found {key} block: {block}")
            grp_iter = block if isinstance(block, list) else [block]
            for group_data in grp_iter:
                 # Treat each timeframe in a group as a separate dimension for simplicity
                 group_name = group_data.get('name')
                 group_type = group_data.get('type')
                 timeframes = group_data.get('timeframes', [])
                 sql = group_data.get('sql')
                 
                 if group_name and group_type == 'time' and timeframes and sql:
                     for timeframe in timeframes:
                         tf_col_data = {
                             'name': f"{group_name}_{timeframe}",
                             'type': 'time', # Original group type
                             'timeframe': timeframe, # Specific timeframe
                             'sql': sql, # Original SQL
                             'description': group_data.get('description'),
                             'label': group_data.get('label'),
                             # Copy other relevant properties
                             'tags': group_data.get('tags', []),
                             'hidden': group_data.get('hidden', 'no') == 'yes',
                         }
                         col_obj = self._extract_field(tf_col_data, 'dimension', view_name, file_path)
                         if col_obj:
                             view_obj['columns'].append(col_obj)  # Add to view's columns
                             self.columns_by_resource[resource_key].append(col_obj)  # Also store in columns_by_resource for compatibility

        self.resources['lookml_views'].append(view_obj)
        
        if view_obj['description']:
            self.docs_by_resource[f"lookml_view.{view_name}"] = view_obj['description']


    def _extract_field(self, field_data, field_type, view_name, file_path):
        """Extracts common information from a LookML field (dimension/measure)."""
        if not isinstance(field_data, dict) or 'name' not in field_data:
            logger.warning(f"Skipping invalid {field_type} definition in {file_path} (view: {view_name}): {field_data}")
            return None

        field_name = field_data['name']
        full_field_name = f"{view_name}.{field_name}" # e.g., users.id
        
        field_obj = {
            'name': full_field_name,
            'field_name': field_name, # Original field name
            'view_name': view_name,
            'path': file_path,
            'resource_type': f'lookml_{field_type}', # e.g., lookml_dimension
            'type': field_data.get('type'), # Looker type (string, number, time, duration, yesno, tier, distance, location, sum, average, count_distinct etc.)
            'sql': field_data.get('sql'),
            'description': field_data.get('description'),
            'label': field_data.get('label'),
            'hidden': field_data.get('hidden', 'no') == 'yes',
            'tags': field_data.get('tags', []),
            'value_format_name': field_data.get('value_format_name'),
            'primary_key': field_data.get('primary_key', 'no') == 'yes',
             # Store all raw data for the field
            'raw_data': field_data, 
            'depends_on': [f"lookml_view.{view_name}"], # Depends on its view
            'columns': [] # Fields don't have sub-columns
        }
        
        # Add simple SQL dependencies (can be improved with better parsing)
        if field_obj['sql']:
             # Look for ${view.field} patterns
             sql_refs = re.findall(r'\$\{([^}]+)\}', field_obj['sql'])
             for ref in sql_refs:
                 if '.' in ref and not ref.lower().endswith('._sql_table_name'): # Avoid self-refs or table name refs for now
                      # Assume format view_name.field_name or just field_name (implies same view)
                      parts = ref.split('.')
                      dep_view = parts[0] if len(parts) > 1 else view_name
                      dep_field = parts[1] if len(parts) > 1 else parts[0]
                      # We depend on the view, not necessarily the specific field within it for graph simplicity
                      dep_resource = f"lookml_view.{dep_view}"
                      if dep_resource not in field_obj['depends_on']:
                           field_obj['depends_on'].append(dep_resource)
                          
        if field_obj['description']:
            self.docs_by_resource[f"lookml_{field_type}.{full_field_name}"] = field_obj['description']
            
        return field_obj

    def _link_explores_to_models(self):
        """
        Ensure explores found potentially outside model blocks (less common)
        are associated if a model with the same file path exists.
        This is a basic heuristic.
        """
        model_paths = {m['path']: m['name'] for m in self.resources['lookml_models']}
        
        for explore in self.resources['lookml_explores']:
            if not explore.get('model_name') and explore['path'] in model_paths:
                model_name = model_paths[explore['path']]
                explore['model_name'] = model_name
                explore['depends_on'] = list(set(explore.get('depends_on', []) + [f"lookml_model.{model_name}"]))


    def get_summary(self):
        """Get a summary of all LookML resources found."""
        summary = {}
        for resource_type, items in self.resources.items():
            # Count only top-level items (models, explores, views, joins) for a concise summary
            if resource_type in ['lookml_models', 'lookml_explores', 'lookml_views', 'lookml_joins']:
                 summary[resource_type] = len(items)
        
        # Count dimensions and measures from the columns_by_resource dictionary
        total_dimensions = 0
        total_measures = 0
        
        for resource_key, columns in self.columns_by_resource.items():
            for column in columns:
                if column.get('resource_type') == 'lookml_dimension':
                    total_dimensions += 1
                elif column.get('resource_type') == 'lookml_measure':
                    total_measures += 1
        
        summary['lookml_dimensions'] = total_dimensions
        summary['lookml_measures'] = total_measures
        
        return summary
