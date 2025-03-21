import os
import yaml
import re
import glob
from pathlib import Path
from collections import defaultdict

class DBTResourceExtractor:
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.resources = {
            'metrics': [],
            'models': [],
            'sources': [],
            'seeds': [],
            'macros': [],
            'tests': [],
            'exposures': []
        }
        # Add a dictionary to store column information
        self.columns_by_resource = defaultdict(list)
        self.docs_by_resource = defaultdict(str)
        
    def extract_all_resources(self):
        """Extract all resources from the dbt project without running dbt"""
        self._parse_yaml_files()
        self._parse_sql_models()
        self._find_macros()
        self._find_seeds()
        return self.resources
    
    def _parse_yaml_files(self):
        """Parse all YAML files to extract metrics, sources, and other YAML-defined resources"""
        yaml_files = list(self.project_dir.glob('**/*.yml')) + list(self.project_dir.glob('**/*.yaml'))
        
        for yaml_file in yaml_files:
            try:
                with open(yaml_file, 'r') as f:
                    yaml_contents = yaml.safe_load(f)
                    
                if not yaml_contents:
                    continue
                    
                # Handle case where yaml_contents is a string instead of a dictionary
                if isinstance(yaml_contents, str):
                    print(f"Warning: YAML file {yaml_file} contains a string instead of a dictionary")
                    continue
                    
                # Skip processing dbt_project.yml file differently
                if yaml_file.name == 'dbt_project.yml':
                    print(f"Processing dbt_project.yml with special handling")
                    # You could extract project-level information here if needed
                    continue
                    
                # Extract metrics
                if 'metrics' in yaml_contents:
                    for metric in yaml_contents['metrics']:
                        # Check if metric is a dictionary before accessing its attributes
                        if not isinstance(metric, dict):
                            print(f"Warning: Metric in {yaml_file} is not a dictionary: {metric}")
                            continue
                            
                        metric_name = metric.get('name', '')
                        
                        # Capture all valuable metric information
                        metric_obj = {
                            'name': metric_name,
                            'path': str(yaml_file),
                            'type': 'metric',
                            'description': metric.get('description', ''),
                            'label': metric.get('label', ''),
                            'calculation_method': metric.get('calculation_method', ''),
                            'expression': metric.get('expression', ''),
                            'timestamp': metric.get('timestamp', ''),
                            'time_grains': metric.get('time_grains', []),
                            'dimensions': metric.get('dimensions', []),
                            'filters': metric.get('filters', []),
                            'meta': metric.get('meta', {}),
                            'model': metric.get('model', ''),
                            'sql': metric.get('sql', ''),
                            'window': metric.get('window', {}),
                            'tags': metric.get('tags', []),
                            'refs': metric.get('refs', []),
                            'depends_on': metric.get('depends_on', []),
                            'columns': []  # Add empty columns list for consistency
                        }
                        
                        # Extract any metric-specific columns if they exist
                        if 'columns' in metric:
                            for column in metric.get('columns', []):
                                column_obj = {
                                    'name': column.get('name', ''),
                                    'description': column.get('description', ''),
                                    'data_type': column.get('data_type', ''),
                                    'tests': column.get('tests', []),
                                    'meta': column.get('meta', {})
                                }
                                metric_obj['columns'].append(column_obj)
                                # Also keep in the old structure for backward compatibility
                                self.columns_by_resource[f"metric.{metric_name}"].append(column_obj)
                        
                        self.resources['metrics'].append(metric_obj)
                        
                        # Store documentation for this metric
                        if 'description' in metric and metric['description']:
                            self.docs_by_resource[f"metric.{metric_name}"] = metric['description']
                
                # Extract sources
                if 'sources' in yaml_contents:
                    if not isinstance(yaml_contents['sources'], list):
                        print(f"Warning: Sources in {yaml_file} is not a list")
                        continue
                    for source in yaml_contents['sources']:
                        source_name = source.get('name', '')
                        source_description = source.get('description', '')
                        
                        for table in source.get('tables', []):
                            table_name = table.get('name', '')
                            full_source_name = f"{source_name}.{table_name}"
                            
                            self.resources['sources'].append({
                                'name': full_source_name,
                                'path': str(yaml_file),
                                'type': 'source',
                                'description': table.get('description', ''),
                                'database': source.get('database', ''),
                                'schema': source.get('schema', ''),
                                'loader': source.get('loader', ''),
                                'freshness': table.get('freshness', {}),
                                'meta': table.get('meta', {})
                            })
                            
                            # Extract columns for this source
                            for column in table.get('columns', []):
                                self.columns_by_resource[f"source.{full_source_name}"].append({
                                    'name': column.get('name', ''),
                                    'description': column.get('description', ''),
                                    'data_type': column.get('data_type', ''),
                                    'tests': column.get('tests', []),
                                    'meta': column.get('meta', {})
                                })
                            
                            # Store documentation for this source
                            if 'description' in table and table['description']:
                                self.docs_by_resource[f"source.{full_source_name}"] = table['description']
                
                # Extract exposures
                if 'exposures' in yaml_contents:
                    for exposure in yaml_contents['exposures']:
                        exposure_name = exposure.get('name', '')
                        self.resources['exposures'].append({
                            'name': exposure_name,
                            'path': str(yaml_file),
                            'type': 'exposure',
                            'description': exposure.get('description', ''),
                            'maturity': exposure.get('maturity', ''),
                            'url': exposure.get('url', ''),
                            'depends_on': exposure.get('depends_on', []),
                            'owner': exposure.get('owner', {})
                        })
                
                # Extract model configurations from schema files
                if 'models' in yaml_contents:
                    for model in yaml_contents['models']:
                        model_name = model.get('name', '')
                        self.resources['models'].append({
                            'name': model_name,
                            'path': str(yaml_file),
                            'type': 'model_config',
                            'description': model.get('description', ''),
                            'config': model.get('config', {}),
                            'meta': model.get('meta', {})
                        })
                        
                        # Extract columns for this model
                        for column in model.get('columns', []):
                            self.columns_by_resource[f"model.{model_name}"].append({
                                'name': column.get('name', ''),
                                'description': column.get('description', ''),
                                'data_type': column.get('data_type', ''),
                                'tests': column.get('tests', []),
                                'meta': column.get('meta', {})
                            })
                        
                        # Store documentation for this model
                        if 'description' in model and model['description']:
                            self.docs_by_resource[f"model.{model_name}"] = model['description']
                
                # Extract docs
                if 'docs' in yaml_contents:
                    for doc in yaml_contents.get('docs', {}).get('docs', []):
                        doc_name = doc.get('name', '')
                        doc_content = doc.get('content', '')
                        if doc_name and doc_content:
                            self.docs_by_resource[f"doc.{doc_name}"] = doc_content
                        
            except Exception as e:
                print(f"Error parsing {yaml_file}: {e}")
    
    def _parse_sql_models(self):
        """Parse SQL files to find models"""
        sql_files = list(self.project_dir.glob('models/**/*.sql'))
        
        for sql_file in sql_files:
            model_name = sql_file.stem
            
            # Check if this model was already found in YAML
            if not any(m['name'] == model_name and m['type'] == 'model_config' for m in self.resources['models']):
                # If not found in YAML, add from SQL file
                description = self._extract_sql_description(sql_file)
                self.resources['models'].append({
                    'name': model_name,
                    'path': str(sql_file),
                    'type': 'model_sql',
                    'description': description,
                    'sql_content': self._extract_sql_content(sql_file)
                })
                
                # Store documentation for this model
                if description:
                    self.docs_by_resource[f"model.{model_name}"] = description
                
            # Check for tests in SQL files (singular tests)
            if 'tests' in str(sql_file) and not any(t['name'] == model_name for t in self.resources['tests']):
                description = self._extract_sql_description(sql_file)
                self.resources['tests'].append({
                    'name': model_name,
                    'path': str(sql_file),
                    'type': 'singular_test',
                    'description': description,
                    'sql_content': self._extract_sql_content(sql_file)
                })
    
    def _extract_sql_content(self, sql_file):
        """Extract the SQL content from a file"""
        try:
            with open(sql_file, 'r') as f:
                return f.read()
        except:
            return ''
    
    def _extract_sql_description(self, sql_file):
        """Extract description from SQL file comments if available"""
        try:
            with open(sql_file, 'r') as f:
                content = f.read()
            
            # Look for description in SQL comments
            comment_match = re.search(r'/\*\*(.*?)\*/', content, re.DOTALL)
            if comment_match:
                return comment_match.group(1).strip()
            return ''
        except:
            return ''
    
    def _find_macros(self):
        """Find all macros in the project"""
        macro_files = list(self.project_dir.glob('macros/**/*.sql'))
        
        for macro_file in macro_files:
            try:
                with open(macro_file, 'r') as f:
                    content = f.read()
                
                # Extract macro names using regex
                macro_matches = re.findall(r'{%-?\s*macro\s+([a-zA-Z0-9_]+)', content)
                
                for macro_name in macro_matches:
                    self.resources['macros'].append({
                        'name': macro_name,
                        'path': str(macro_file),
                        'type': 'macro'
                    })
            except Exception as e:
                print(f"Error parsing macro {macro_file}: {e}")
    
    def _find_seeds(self):
        """Find all seed files"""
        seed_files = list(self.project_dir.glob('seeds/**/*.csv'))
        
        for seed_file in seed_files:
            self.resources['seeds'].append({
                'name': seed_file.stem,
                'path': str(seed_file),
                'type': 'seed'
            })
    
    def get_summary(self):
        """Get a summary of all resources found"""
        summary = {}
        for resource_type, items in self.resources.items():
            summary[resource_type] = len(items)
        return summary
    
    def export_to_csv(self, output_file):
        """Export all resources to a CSV file"""
        import csv
        
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Resource Type', 'Name', 'Path', 'Description', 'Additional Info'])
            
            for resource_type, items in self.resources.items():
                for item in items:
                    # Prepare additional info as a string
                    additional_info = {}
                    for key, value in item.items():
                        if key not in ['name', 'path', 'type', 'description']:
                            additional_info[key] = value
                    
                    writer.writerow([
                        resource_type,
                        item.get('name', ''),
                        item.get('path', ''),
                        item.get('description', ''),
                        str(additional_info) if additional_info else ''
                    ])
    
    def export_columns_to_csv(self, output_file):
        """Export all columns to a CSV file"""
        import csv
        
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Resource', 'Column Name', 'Description', 'Data Type', 'Tests', 'Meta'])
            
            for resource, columns in self.columns_by_resource.items():
                for column in columns:
                    writer.writerow([
                        resource,
                        column.get('name', ''),
                        column.get('description', ''),
                        column.get('data_type', ''),
                        str(column.get('tests', [])),
                        str(column.get('meta', {}))
                    ])
    
    def export_docs_to_csv(self, output_file):
        """Export all documentation to a CSV file"""
        import csv
        
        with open(output_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Resource', 'Documentation'])
            
            for resource, doc in self.docs_by_resource.items():
                writer.writerow([resource, doc])