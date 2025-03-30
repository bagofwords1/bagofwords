import re
import json
from sqlalchemy import select

class ResourceContextBuilder:
    def __init__(self, db, prompt_content):
        self.db = db
        self.prompt_content = prompt_content

    async def build_context(self, data_sources):
        """Build context from resources based on the prompt content."""
        context = []
        # Extract keywords from the prompt
        keywords = self._extract_keywords_from_prompt(self.prompt_content)

        # For each data source, check if there's a git repository
        for data_source in data_sources:
            # Find the git repository connected to this data source
            git_repository = await self.db.execute(
                select(GitRepository).where(GitRepository.data_source_id == data_source.id)
            )
            git_repository = git_repository.scalars().first()
            
            if not git_repository:
                continue
                
            # Find the latest metadata index job for this repository
            latest_index_job = await self.db.execute(
                    select(MetadataIndexingJob)
                .where(MetadataIndexingJob.git_repository_id == git_repository.id)
                .order_by(MetadataIndexingJob.created_at.desc())
                .limit(1)
            )

            latest_index_job = latest_index_job.scalars().first()
            
            if not latest_index_job:
                continue
                
            # Find all DBT resources associated with this index job
            dbt_resources = await self.db.execute(
                select(DBTResource)
                .where(DBTResource.metadata_indexing_job_id == latest_index_job.id)
                .where(DBTResource.is_active == True)
            )
            dbt_resources = dbt_resources.scalars().all()
            
            # Filter resources based on keywords
            relevant_resources = self._filter_resources_by_keywords(dbt_resources, keywords)
            
            # Add relevant resources to context
            if relevant_resources:
                context.append("<relevant_dbt_resources>")
                
                for resource in relevant_resources:
                    # Format the resource based on its type
                    formatted_resource = self._format_resource_by_type(resource)
                    context.append(formatted_resource)
                    
                context.append("</relevant_dbt_resources>")
        
        return "\n".join(context)

    def _extract_keywords_from_prompt(self, prompt):
        """Extract important keywords from the prompt."""
        # Simple implementation - split by spaces and remove common words
        common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'about', 'like', 'through', 'over', 'before', 'between', 'after', 'since', 'without', 'under', 'within', 'along', 'following', 'across', 'behind', 'beyond', 'plus', 'except', 'but', 'up', 'out', 'around', 'down', 'off', 'above', 'near', 'show', 'me', 'get', 'find', 'what', 'where', 'when', 'who', 'how', 'why', 'which', 'create', 'make', 'list', 'all', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'can', 'could', 'will', 'would', 'shall', 'should', 'may', 'might', 'must'}
        prompt = self.prompt_content['content'].lower()

        words = re.findall(r'\b\w+\b', prompt)
        keywords = [word for word in words if word not in common_words and len(word) > 2]
        
        return keywords

    def _filter_resources_by_keywords(self, resources, keywords):
        """Filter resources based on keywords."""
        relevant_resources = []
        
        for resource in resources:
            # Create a searchable text from the resource
            searchable_text = f"{resource.name} {resource.description or ''} {resource.sql_content or ''}"
            
            # Check if any keyword is in the searchable text
            if any(keyword.lower() in searchable_text.lower() for keyword in keywords):
                relevant_resources.append(resource)
               
        # Limit to top 5 most relevant resources to avoid context overload
        return relevant_resources[:5]

    def _format_resource_by_type(self, resource):
        """Format a resource based on its type according to the schema."""
        resource_type = resource.resource_type.lower()
        
        if resource_type == "model":
            return self._format_model(resource)
        elif resource_type == "metric":
            return self._format_metric(resource)
        elif resource_type == "source":
            return self._format_source(resource)
        elif resource_type == "seed":
            return self._format_seed(resource)
        elif resource_type == "macro":
            return self._format_macro(resource)
        elif resource_type == "test" or resource_type == "singular_test":
            return self._format_test(resource)
        elif resource_type == "exposure":
            return self._format_exposure(resource)
        else:
            # Default formatting for unknown types
            return self._format_generic_resource(resource)
    
    def _format_model(self, resource):
        """Format a model resource."""
        # Handle columns data based on its type
        if isinstance(resource.columns, list):
            columns_json = resource.columns
        elif isinstance(resource.columns, str):
            columns_json = json.loads(resource.columns) if resource.columns else []
        else:
            columns_json = getattr(resource.columns, '__dict__', []) if resource.columns else []
        
        columns_formatted = []
        
        for column in columns_json:
            column_str = f"    <column>\n"
            column_str += f"      <name>{column.get('name', '')}</name>\n"
            column_str += f"      <description>{column.get('description', '')}</description>\n"
            column_str += f"      <data_type>{column.get('data_type', '')}</data_type>\n"
            column_str += f"    </column>"
            columns_formatted.append(column_str)
        
        model_str = f"<model>\n"
        model_str += f"  <name>{resource.name}</name>\n"
        model_str += f"  <description>{resource.description or ''}</description>\n"
        model_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        
        if columns_formatted:
            model_str += f"  <columns>\n"
            model_str += "\n".join(columns_formatted) + "\n"
            model_str += f"  </columns>\n"
        
        model_str += f"</model>"
        return model_str
    
    def _format_metric(self, resource):
        """Format a metric resource."""
        # Use the metadata directly if it's already an object, otherwise try to parse it
        if isinstance(resource.metadata, dict):
            metric_data = resource.metadata
        elif isinstance(resource.metadata, str):
            metric_data = json.loads(resource.metadata) if resource.metadata else {}
        else:
            # If it's some other type, try to convert it to a dict or use empty dict
            metric_data = getattr(resource.metadata, '__dict__', {}) if resource.metadata else {}
        
        metric_str = f"<metric>\n"
        metric_str += f"  <name>{resource.name}</name>\n"
        metric_str += f"  <description>{resource.description or ''}</description>\n"
        metric_str += f"  <calculation_method>{metric_data.get('calculation_method', '')}</calculation_method>\n"
        metric_str += f"  <expression>{metric_data.get('expression', '')}</expression>\n"
        
        if metric_data.get('dimensions'):
            dimensions = metric_data.get('dimensions', [])
            metric_str += f"  <dimensions>{', '.join(dimensions)}</dimensions>\n"
        
        if metric_data.get('time_grains'):
            time_grains = metric_data.get('time_grains', [])
            metric_str += f"  <time_grains>{', '.join(time_grains)}</time_grains>\n"
        
        metric_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        metric_str += f"</metric>"
        return metric_str
    
    def _format_source(self, resource):
        """Format a source resource."""
        # Handle metadata based on its type
        if isinstance(resource.metadata, dict):
            source_data = resource.metadata
        elif isinstance(resource.metadata, str):
            source_data = json.loads(resource.metadata) if resource.metadata else {}
        else:
            source_data = getattr(resource.metadata, '__dict__', {}) if resource.metadata else {}
        
        # Handle columns data based on its type
        if isinstance(resource.columns, list):
            columns_json = resource.columns
        elif isinstance(resource.columns, str):
            columns_json = json.loads(resource.columns) if resource.columns else []
        else:
            columns_json = getattr(resource.columns, '__dict__', []) if resource.columns else []
        
        source_str = f"<source>\n"
        source_str += f"  <name>{resource.name}</name>\n"
        source_str += f"  <description>{resource.description or ''}</description>\n"
        source_str += f"  <database>{source_data.get('database', '')}</database>\n"
        source_str += f"  <schema>{source_data.get('schema', '')}</schema>\n"
        
        if columns_json:
            source_str += f"  <columns>\n"
            for column in columns_json:
                source_str += f"    <column>\n"
                source_str += f"      <name>{column.get('name', '')}</name>\n"
                source_str += f"      <description>{column.get('description', '')}</description>\n"
                source_str += f"      <data_type>{column.get('data_type', '')}</data_type>\n"
                source_str += f"    </column>\n"
            source_str += f"  </columns>\n"
        
        source_str += f"</source>"
        return source_str
    
    def _format_seed(self, resource):
        """Format a seed resource."""
        seed_str = f"<seed>\n"
        seed_str += f"  <name>{resource.name}</name>\n"
        seed_str += f"  <description>{resource.description or ''}</description>\n"
        seed_str += f"</seed>"
        return seed_str
    
    def _format_macro(self, resource):
        """Format a macro resource."""
        macro_str = f"<macro>\n"
        macro_str += f"  <name>{resource.name}</name>\n"
        macro_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        macro_str += f"</macro>"
        return macro_str
    
    def _format_test(self, resource):
        """Format a test resource."""
        test_str = f"<test>\n"
        test_str += f"  <name>{resource.name}</name>\n"
        test_str += f"  <description>{resource.description or ''}</description>\n"
        test_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        test_str += f"</test>"
        return test_str
    
    def _format_exposure(self, resource):
        """Format an exposure resource."""
        # Handle metadata based on its type
        if isinstance(resource.metadata, dict):
            exposure_data = resource.metadata
        elif isinstance(resource.metadata, str):
            exposure_data = json.loads(resource.metadata) if resource.metadata else {}
        else:
            exposure_data = getattr(resource.metadata, '__dict__', {}) if resource.metadata else {}
        
        exposure_str = f"<exposure>\n"
        exposure_str += f"  <name>{resource.name}</name>\n"
        exposure_str += f"  <description>{resource.description or ''}</description>\n"
        exposure_str += f"  <maturity>{exposure_data.get('maturity', '')}</maturity>\n"
        exposure_str += f"  <url>{exposure_data.get('url', '')}</url>\n"
        
        if exposure_data.get('depends_on'):
            depends_on = exposure_data.get('depends_on', [])
            exposure_str += f"  <depends_on>{', '.join(depends_on)}</depends_on>\n"
        
        exposure_str += f"</exposure>"
        return exposure_str
    
    def _format_generic_resource(self, resource):
        """Format a generic resource when type is unknown."""
        resource_str = f"<resource>\n"
        resource_str += f"  <name>{resource.name}</name>\n"
        resource_str += f"  <resource_type>{resource.resource_type}</resource_type>\n"
        resource_str += f"  <description>{resource.description or ''}</description>\n"
        resource_str += f"  <sql_content>{resource.sql_content or ''}</sql_content>\n"
        
        if resource.columns:
            # Handle columns based on type
            if isinstance(resource.columns, str):
                try:
                    columns_str = resource.columns
                except:
                    columns_str = str(resource.columns)
            else:
                columns_str = str(resource.columns)
            
            resource_str += f"  <columns>{columns_str}</columns>\n"
        
        resource_str += f"</resource>"
        return resource_str

# Import models at the module level
from app.models.git_repository import GitRepository
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.dbt_resource import DBTResource