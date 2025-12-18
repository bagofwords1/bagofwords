"""
DEPRECATED: This module is deprecated and will be removed in a future version.

Use InstructionContextBuilder instead, which now handles both user-created
instructions and git-sourced resources through the unified Instruction model.

Example migration:
    # Old way (deprecated)
    builder = ResourceContextBuilder(db, data_sources, org, prompt)
    context = await builder.build()
    
    # New way (recommended)
    builder = InstructionContextBuilder(db, org)
    context = await builder.build(query, data_source_ids=ds_ids)
"""

import re
import json
import warnings
from sqlalchemy import select

# Import the unified MetadataResource model
from app.models.metadata_resource import MetadataResource
from app.models.git_repository import GitRepository
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.organization import Organization
from app.ai.context.sections.resources_section import ResourcesSection


class ResourceContextBuilder:
    """
    DEPRECATED: Use InstructionContextBuilder instead.
    
    This class is maintained for backwards compatibility but will be removed
    in a future version. Git-sourced resources are now synced to Instructions
    and should be loaded via InstructionContextBuilder.
    """
    
    def __init__(self, db, data_sources, organization, prompt_content):
        warnings.warn(
            "ResourceContextBuilder is deprecated. Use InstructionContextBuilder.build() instead.",
            DeprecationWarning,
            stacklevel=2
        )
        self.db = db
        self.organization = organization
        self.prompt_content = prompt_content
        self.data_sources = data_sources

    async def build_context(self):
        """Build context from resources based on the prompt content (string)."""
        context = []
        repositories: list[ResourcesSection.Repository] = []
        # Extract keywords from the prompt
        keywords = self._extract_keywords_from_prompt(self.prompt_content)
        # For each data source, check if there's a git repository
        for data_source in self.data_sources:
            # Find the most recently created git repository connected to this data source
            git_repository_result = await self.db.execute(
                select(GitRepository)
                .where(GitRepository.data_source_id == data_source.id)
                .order_by(GitRepository.created_at.desc())
                .limit(1)
            )
            git_repository = git_repository_result.scalars().first()
            
            if not git_repository:
                continue
                
            # Find the latest *successful* metadata index job for this repository
            latest_index_job = await self.db.execute(
                    select(MetadataIndexingJob)
                .where(
                    MetadataIndexingJob.git_repository_id == git_repository.id,
                    MetadataIndexingJob.status == 'completed' # Only consider completed jobs
                    )
                .order_by(MetadataIndexingJob.completed_at.desc()) # Order by completion time
                .limit(1)
            )

            latest_index_job = latest_index_job.scalars().first()

            if not latest_index_job:
                continue
                
            # Find all active MetadataResources associated with this index job
            metadata_resources_result = await self.db.execute(
                select(MetadataResource)
                .where(MetadataResource.metadata_indexing_job_id == latest_index_job.id)
                .where(MetadataResource.is_active == True)
            )
            all_resources = metadata_resources_result.scalars().all()
            
            # Filter resources based on keywords
            relevant_resources = self._filter_resources_by_keywords(all_resources, keywords)
            # Add relevant resources to context
            if relevant_resources:
                context.append("<relevant_metadata_resources>")
                
                for resource in relevant_resources:
                    # Format the resource based on its type
                    formatted_resource = self._format_resource_by_type(resource)
                    context.append(formatted_resource)
                    
                context.append("</relevant_metadata_resources>")

            # Build structured repository entry for combined renderers
            try:
                ds_name = getattr(data_source, 'name', None) or "Data Source"
                repo_name = f"{ds_name} Metadata Resources"
                repo_id = str(getattr(git_repository, 'id', None)) if getattr(git_repository, 'id', None) else None
                ds_id = str(getattr(data_source, 'id', None)) if getattr(data_source, 'id', None) else None

                resources_payload: list[dict] = []
                for res in all_resources:
                    try:
                        resources_payload.append({
                            "name": getattr(res, 'name', None),
                            "resource_type": (getattr(res, 'resource_type', None) or ""),
                            "path": getattr(res, 'path', None),
                            "description": getattr(res, 'description', None),
                            "sql_content": getattr(res, 'sql_content', None),
                            "source_name": getattr(res, 'source_name', None),
                            "database": getattr(res, 'database', None),
                            "schema": getattr(res, 'schema', None),
                            "columns": getattr(res, 'columns', None),
                            "depends_on": getattr(res, 'depends_on', None),
                            "raw_data": getattr(res, 'raw_data', None),
                        })
                    except Exception:
                        continue

                repositories.append(ResourcesSection.Repository(
                    name=repo_name,
                    id=repo_id,
                    data_source_id=ds_id,
                    resources=resources_payload,
                ))
            except Exception:
                # Structured section is best-effort; keep legacy content regardless
                pass
        
        # Return both legacy pre-rendered content and structured repositories
        return "\n".join(context), repositories

    async def build(self) -> ResourcesSection:
        result = await self.build_context()
        if isinstance(result, tuple):
            content, repositories = result
            try:
                return ResourcesSection(content=content, repositories=repositories)
            except Exception:
                return ResourcesSection(content=content)
        # Back-compat if older signature is returned
        content = result
        return ResourcesSection(content=content)

    def _extract_keywords_from_prompt(self, prompt_data):
        """Extract important keywords from the prompt."""
        # Simple implementation - split by spaces and remove common words
        common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'about', 'like', 'through', 'over', 'before', 'between', 'after', 'since', 'without', 'under', 'within', 'along', 'following', 'across', 'behind', 'beyond', 'plus', 'except', 'but', 'up', 'out', 'around', 'down', 'off', 'above', 'near', 'show', 'me', 'get', 'find', 'what', 'where', 'when', 'who', 'how', 'why', 'which', 'create', 'make', 'list', 'all', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must'}
        # Ensure prompt_data is a dictionary and has 'content'
        prompt_content = prompt_data.get('content', '') if isinstance(prompt_data, dict) else ''
        prompt = prompt_content.lower()

        words = re.findall(r'\b\w+\b', prompt)
        keywords = [word for word in words if word not in common_words and len(word) > 2]
        
        return keywords

    def _filter_resources_by_keywords(self, resources, keywords):
        """Filter resources based on keywords."""
        relevant_resources = []
        
        for resource in resources:
            # Create a searchable text from the resource
            # Include resource_type in searchable text
            searchable_text = f"{resource.name} {resource.resource_type} {resource.description or ''} {resource.sql_content or ''} {resource.path or ''}"
            
            # Check if any keyword is in the searchable text
            if any(keyword.lower() in searchable_text.lower() for keyword in keywords):
                relevant_resources.append(resource)
        
        # Limit to top 5 most relevant resources to avoid context overload
        return relevant_resources[:5]

    def _format_resource_by_type(self, resource):
        """Format a resource based on its type according to the schema."""
        # Normalize resource_type (e.g., 'dbt model', 'lookml view')
        # Check for DBT types first, then LookML, then generic
        resource_type = resource.resource_type.lower()
        
        if resource_type == "model": # Handles 'dbt model' if stored as 'model'
            return self._format_model(resource)
        elif resource_type == "metric": # Handles 'dbt metric'
            return self._format_metric(resource)
        elif resource_type == "source": # Handles 'dbt source'
            return self._format_source(resource)
        elif resource_type == "seed": # Handles 'dbt seed'
            return self._format_seed(resource)
        elif resource_type == "macro": # Handles 'dbt macro'
            return self._format_macro(resource)
        elif resource_type in ["test", "singular_test"]: # Handles 'dbt test'
             return self._format_test(resource)
        elif resource_type == "exposure": # Handles 'dbt exposure'
            return self._format_exposure(resource)
        # Add LookML specific formatters if needed, otherwise they use generic
        elif resource_type == "lookml_model":
             return self._format_lookml_model(resource) # Example placeholder
        elif resource_type == "lookml_view":
             return self._format_lookml_view(resource)  # Example placeholder
        elif resource_type == "lookml_explore":
             return self._format_lookml_explore(resource) # Example placeholder
        # ... add other LookML types ...
        else:
            # Default formatting for unknown or unhandled types
            return self._format_generic_resource(resource)
    
    def _parse_json_field(self, field_value):
        """Safely parse a JSON field, returning empty list/dict on error or if None."""
        if field_value is None:
            return [] # Default to list, adjust if dict is more common
        if isinstance(field_value, (list, dict)):
            return field_value
        if isinstance(field_value, str):
            try:
                return json.loads(field_value)
            except json.JSONDecodeError:
                return [] # Or {} if appropriate
        # Fallback for other unexpected types
        return []

    def _format_model(self, resource):
        """Format a model resource (likely DBT)."""
        columns_json = self._parse_json_field(resource.columns)
        columns_formatted = []
        
        for column in columns_json:
            column_str = f"    <column>\n"
            column_str += f"      <name>{column.get('name', '')}</name>\n"
            column_str += f"      <description>{column.get('description', '')}</description>\n"
            column_str += f"      <data_type>{column.get('data_type', '')}</data_type>\n"
            # Add other column details if available, e.g., tests
            if column.get('tests'):
                 column_str += f"      <tests>{column.get('tests')}</tests>\n"
            column_str += f"    </column>"
            columns_formatted.append(column_str)
        
        model_str = f"<model>\n" # Consider adding type prefix, e.g., <dbt_model>
        model_str += f"  <name>{resource.name}</name>\n"
        model_str += f"  <resource_type>{resource.resource_type}</resource_type>\n" # Explicitly add type
        model_str += f"  <description>{resource.description or ''}</description>\n"
        model_str += f"  <path>{resource.path or ''}</path>\n" # Add path
        model_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        
        if columns_formatted:
            model_str += f"  <columns>\n"
            model_str += "\n".join(columns_formatted) + "\n"
            model_str += f"  </columns>\n"
        
        # Add depends_on if available
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            model_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        # Add raw_data fields if useful and not redundant
        # raw_data = self._parse_json_field(resource.raw_data)
        # Example: model_str += f"  <config>{raw_data.get('config', {})}</config>\n"

        model_str += f"</model>"
        return model_str
    
    def _format_metric(self, resource):
        """Format a metric resource (DBT)."""
        # Use raw_data which stores the original extracted dictionary
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}

        metric_str = f"<metric>\n" # Consider <dbt_metric>
        metric_str += f"  <name>{resource.name}</name>\n"
        metric_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        metric_str += f"  <description>{resource.description or ''}</description>\n"
        metric_str += f"  <path>{resource.path or ''}</path>\n"
        # Extract specific fields from raw_data
        metric_str += f"  <label>{raw_data.get('label', '')}</label>\n"
        metric_str += f"  <calculation_method>{raw_data.get('calculation_method', '')}</calculation_method>\n"
        metric_str += f"  <expression>{raw_data.get('expression', '')}</expression>\n"

        dimensions = raw_data.get('dimensions', [])
        if dimensions:
            metric_str += f"  <dimensions>{', '.join(dimensions)}</dimensions>\n"

        time_grains = raw_data.get('time_grains', [])
        if time_grains:
            metric_str += f"  <time_grains>{', '.join(time_grains)}</time_grains>\n"

        # Filters if available in raw_data
        filters = raw_data.get('filters', [])
        if filters:
             filter_strs = [f"{f.get('field','?')}:{f.get('operator','?')}:{f.get('value','?')}" for f in filters]
             metric_str += f"  <filters>{', '.join(filter_strs)}</filters>\n"

        # Add columns if they exist in the metric definition (less common)
        columns_json = self._parse_json_field(resource.columns)
        if columns_json:
            columns_formatted = []
            for column in columns_json:
                col_str = f"    <column name='{column.get('name', '')}' description='{column.get('description', '')}' data_type='{column.get('data_type', '')}' />"
                columns_formatted.append(col_str)
            metric_str += f"  <columns>\n" + "\n".join(columns_formatted) + "\n  </columns>\n"

        # Add depends_on
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            metric_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        metric_str += f"</metric>"
        return metric_str
    
    def _format_source(self, resource):
        """Format a source resource (DBT)."""
        columns_json = self._parse_json_field(resource.columns)
        columns_formatted = []
        
        for column in columns_json:
            column_str = f"    <column>\n"
            column_str += f"      <name>{column.get('name', '')}</name>\n"
            column_str += f"      <description>{column.get('description', '')}</description>\n"
            column_str += f"      <data_type>{column.get('data_type', '')}</data_type>\n"
            if column.get('tests'):
                 column_str += f"      <tests>{column.get('tests')}</tests>\n"
            column_str += f"    </column>"
            columns_formatted.append(column_str)
        
        source_str = f"<source>\n" # Consider <dbt_source>
        source_str += f"  <name>{resource.name}</name>\n" # Full name like source_name.table_name
        source_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        source_str += f"  <description>{resource.description or ''}</description>\n"
        source_str += f"  <path>{resource.path or ''}</path>\n"
        # Use direct fields from MetadataResource
        source_str += f"  <source_name>{resource.source_name or ''}</source_name>\n" # Extracted source group name
        source_str += f"  <database>{resource.database or ''}</database>\n"
        source_str += f"  <schema>{resource.schema or ''}</schema>\n"

        # Add loader from raw_data if exists
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        source_str += f"  <loader>{raw_data.get('loader', '')}</loader>\n"
        # Add freshness info if exists
        freshness = raw_data.get('freshness', {})
        if freshness:
             warn_after = freshness.get('warn_after', {})
             error_after = freshness.get('error_after', {})
             filter_cond = freshness.get('filter')
             freshness_str = f"warn_after: {warn_after.get('count')} {warn_after.get('period')}" if warn_after.get('count') else ""
             freshness_str += f", error_after: {error_after.get('count')} {error_after.get('period')}" if error_after.get('count') else ""
             if filter_cond: freshness_str += f", filter: {filter_cond}"
             source_str += f"  <freshness>{freshness_str}</freshness>\n"


        if columns_formatted:
            source_str += f"  <columns>\n"
            source_str += "\n".join(columns_formatted) + "\n"
            source_str += f"  </columns>\n"

        # Add depends_on (less common for sources)
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            source_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        source_str += f"</source>"
        return source_str
    
    def _format_seed(self, resource):
        """Format a seed resource (DBT)."""
        seed_str = f"<seed>\n" # Consider <dbt_seed>
        seed_str += f"  <name>{resource.name}</name>\n"
        seed_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        seed_str += f"  <description>{resource.description or ''}</description>\n"
        seed_str += f"  <path>{resource.path or ''}</path>\n"
        seed_str += f"  <database>{resource.database or ''}</database>\n"
        seed_str += f"  <schema>{resource.schema or ''}</schema>\n"
        # Include columns if parsed and stored
        columns_json = self._parse_json_field(resource.columns)
        if columns_json:
            columns_formatted = [f"    <column name='{c.get('name', '')}' data_type='{c.get('data_type', '')}' />" for c in columns_json]
            seed_str += f"  <columns>\n" + "\n".join(columns_formatted) + "\n  </columns>\n"
        seed_str += f"</seed>"
        return seed_str
    
    def _format_macro(self, resource):
        """Format a macro resource (DBT)."""
        macro_str = f"<macro>\n" # Consider <dbt_macro>
        macro_str += f"  <name>{resource.name}</name>\n"
        macro_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        macro_str += f"  <description>{resource.description or ''}</description>\n"
        macro_str += f"  <path>{resource.path or ''}</path>\n"
        macro_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        # Include arguments if parsed into raw_data
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        arguments = raw_data.get('arguments', [])
        if arguments:
            arg_strs = [f"{arg.get('name')}({arg.get('type', '?')})" for arg in arguments]
            macro_str += f"  <arguments>{', '.join(arg_strs)}</arguments>\n"
        macro_str += f"</macro>"
        return macro_str
    
    def _format_test(self, resource):
        """Format a test resource (DBT - generic, schema, singular)."""
        test_str = f"<test>\n" # Consider <dbt_test>
        test_str += f"  <name>{resource.name}</name>\n"
        test_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        test_str += f"  <description>{resource.description or ''}</description>\n"
        test_str += f"  <path>{resource.path or ''}</path>\n"
        test_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n" # Often just the test name for schema tests
        # Include test metadata from raw_data if available
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        test_metadata = raw_data.get('test_metadata', {})
        if test_metadata:
             test_str += f"  <test_type>{test_metadata.get('name', 'unknown')}</test_type>\n" # e.g., unique, not_null
             if test_metadata.get('kwargs'):
                  test_str += f"  <test_config>{test_metadata.get('kwargs')}</test_config>\n"
        # Add depends_on
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            test_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        test_str += f"</test>"
        return test_str
    
    def _format_exposure(self, resource):
        """Format an exposure resource (DBT)."""
        # Use raw_data which stores the original extracted dictionary
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}

        exposure_str = f"<exposure>\n" # Consider <dbt_exposure>
        exposure_str += f"  <name>{resource.name}</name>\n"
        exposure_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        exposure_str += f"  <description>{resource.description or ''}</description>\n"
        exposure_str += f"  <path>{resource.path or ''}</path>\n"
        # Extract specific fields from raw_data
        exposure_str += f"  <type>{raw_data.get('type', '')}</type>\n" # e.g., dashboard, notebook
        exposure_str += f"  <maturity>{raw_data.get('maturity', '')}</maturity>\n" # e.g., high, medium
        exposure_str += f"  <url>{raw_data.get('url', '')}</url>\n"
        owner = raw_data.get('owner', {})
        exposure_str += f"  <owner_name>{owner.get('name', '')}</owner_name>\n"
        exposure_str += f"  <owner_email>{owner.get('email', '')}</owner_email>\n"

        # Add depends_on (should be in resource.depends_on directly now)
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            exposure_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        exposure_str += f"</exposure>"
        return exposure_str

    # --- Placeholder Formatters for LookML ---
    # Implement these based on LookML structure stored in MetadataResource.raw_data
    # and MetadataResource.columns if applicable

    def _format_lookml_model(self, resource):
        """Format a LookML model resource."""
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        model_str = f"<lookml_model>\n"
        model_str += f"  <name>{resource.name}</name>\n"
        model_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        model_str += f"  <description>{resource.description or ''}</description>\n"
        model_str += f"  <path>{resource.path or ''}</path>\n"
        model_str += f"  <label>{raw_data.get('label', '')}</label>\n"
        model_str += f"  <connection>{raw_data.get('connection', '')}</connection>\n"
        # List explores defined within? depends_on should capture this?
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            model_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"
        model_str += f"</lookml_model>"
        return model_str

    def _format_lookml_view(self, resource):
        """Format a LookML view resource."""
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        columns_json = self._parse_json_field(resource.columns) # Dimensions/Measures
        columns_formatted = []

        for field in columns_json: # These are fields (dimensions/measures)
            field_type = field.get('resource_type', 'lookml_field').replace('lookml_', '') # dimension/measure
            field_str = f"    <{field_type}>\n" # <dimension> or <measure>
            field_str += f"      <name>{field.get('field_name', field.get('name', ''))}</name>\n" # Use original field name
            field_str += f"      <type>{field.get('type', '')}</type>\n" # Looker type (string, number, time...)
            field_str += f"      <description>{field.get('description', '')}</description>\n"
            field_str += f"      <sql>{field.get('sql', '')}</sql>\n"
            field_str += f"      <hidden>{field.get('hidden', False)}</hidden>\n"
            if field.get('tags'): field_str += f"      <tags>{', '.join(field.get('tags', []))}</tags>\n"
            if field.get('primary_key'): field_str += f"      <primary_key>{field.get('primary_key', False)}</primary_key>\n"
            field_str += f"    </{field_type}>"
            columns_formatted.append(field_str)

        view_str = f"<lookml_view>\n"
        view_str += f"  <name>{resource.name}</name>\n"
        view_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        view_str += f"  <description>{resource.description or ''}</description>\n"
        view_str += f"  <path>{resource.path or ''}</path>\n"
        view_str += f"  <label>{raw_data.get('label', '')}</label>\n"
        view_str += f"  <sql_table_name>{raw_data.get('sql_table_name', '')}</sql_table_name>\n"
        if raw_data.get('derived_table'):
            view_str += f"  <derived_table_sql>{raw_data['derived_table'].get('sql', '...')}</derived_table_sql>\n"

        if columns_formatted:
            view_str += f"  <fields>\n" # Changed from <columns> to <fields>
            view_str += "\n".join(columns_formatted) + "\n"
            view_str += f"  </fields>\n"

        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            view_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        view_str += f"</lookml_view>"
        return view_str

    def _format_lookml_explore(self, resource):
        """Format a LookML explore resource."""
        raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        explore_str = f"<lookml_explore>\n"
        explore_str += f"  <name>{resource.name}</name>\n"
        explore_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        explore_str += f"  <description>{resource.description or ''}</description>\n"
        explore_str += f"  <path>{resource.path or ''}</path>\n"
        explore_str += f"  <model_name>{raw_data.get('model_name', '')}</model_name>\n"
        explore_str += f"  <label>{raw_data.get('label', '')}</label>\n"
        explore_str += f"  <view_name>{raw_data.get('view_name', '')}</view_name>\n" # Base view

        # List joins? depends_on should capture this
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            explore_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        explore_str += f"</lookml_explore>"
        return explore_str


    # --- Generic Formatter ---

    def _format_generic_resource(self, resource):
        """Format a generic resource when type specific formatter is not available."""
        resource_str = f"<resource>\n"
        resource_str += f"  <name>{resource.name}</name>\n"
        resource_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        resource_str += f"  <description>{resource.description or ''}</description>\n"
        resource_str += f"  <path>{resource.path or ''}</path>\n"
        resource_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n" # If applicable

        # Add columns if they exist
        columns_json = self._parse_json_field(resource.columns)
        if columns_json:
            columns_formatted = []
            for column in columns_json:
                 col_str = f"    <column name='{column.get('name', '')}' description='{column.get('description', '')}' data_type='{column.get('data_type', '')}' />"
                 columns_formatted.append(col_str)
            resource_str += f"  <columns>\n" + "\n".join(columns_formatted) + "\n  </columns>\n"

        # Add depends_on if it exists
        depends_on_list = self._parse_json_field(resource.depends_on)
        if depends_on_list:
            resource_str += f"  <depends_on>{', '.join(depends_on_list)}</depends_on>\n"

        # Include snippet of raw_data? Be careful about size.
        # raw_data = self._parse_json_field(resource.raw_data) if resource.raw_data else {}
        # raw_data_snippet = {k: v for k, v in raw_data.items() if k not in ['name', 'description', 'path', 'sql_content', 'columns', 'depends_on', 'resource_type']}
        # if raw_data_snippet:
        #     resource_str += f"  <raw_data_snippet>{json.dumps(raw_data_snippet, indent=2)}</raw_data_snippet>\n"


        resource_str += f"</resource>"
        return resource_str