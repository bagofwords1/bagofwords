from pathlib import Path
from collections import defaultdict
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MarkdownResourceExtractor:
    """
    Extracts Markdown files from a project directory and treats each as a resource.
    Useful for documentation, context files, and other text content that should be
    available to LLMs.
    """
    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        # Standardized output structure - only markdown documents
        self.resources = {
            'markdown_documents': [],
        }
        # Using similar structure as DBT/LookML for consistency
        self.columns_by_resource = defaultdict(list)  # Not used for markdown, but kept for interface consistency
        self.docs_by_resource = defaultdict(str)       # Store descriptions/summaries here

    def extract_all_resources(self):
        """Extract all Markdown files from the project directory."""
        markdown_files = list(self.project_dir.glob('**/*.md'))
        
        for md_file in markdown_files:
            try:
                self._parse_markdown_file(md_file)
            except Exception as e:
                logger.error(f"Error parsing Markdown file {md_file}: {e}")
        
        logger.info(f"Found {len(self.resources['markdown_documents'])} Markdown documents")
        return self.resources, self.columns_by_resource, self.docs_by_resource

    def _parse_markdown_file(self, file_path):
        """Parses a single Markdown file and creates one resource for the entire file."""
        encoding_used = 'utf-8'
        try:
            # Read the file content
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            # Try with latin-1 encoding as fallback
            try:
                encoding_used = 'latin-1'
                with open(file_path, 'r', encoding=encoding_used) as f:
                    content = f.read()
            except Exception as e:
                logger.error(f"Could not read file {file_path} with any encoding: {e}")
                return
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return

        # Get relative path from project root
        relative_path = file_path.relative_to(self.project_dir)
        relative_path_str = str(relative_path)
        
        # Extract title from the first H1 header if available, otherwise use filename
        file_title = self._extract_title_from_content(content) or file_path.stem
        
        # Extract description from first paragraph
        description = self._extract_description_from_content(content)
        
        # Create a single resource for the entire file (no chunking)
        self._add_markdown_resource(
            name=relative_path_str,
            title=file_title,
            path=relative_path_str,
            description=description,
            content=content,
            encoding=encoding_used,
        )

    def _extract_title_from_content(self, content):
        """Extract title from the first H1 header in the markdown content."""
        lines = content.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('# '):
                return line[2:].strip()
        return None

    def _extract_description_from_content(self, content):
        """Extract a brief description from the first paragraph or first few lines."""
        lines = content.split('\n')
        description_lines = []
        in_content = False
        
        for line in lines:
            line = line.strip()
            
            # Skip empty lines and headers at the beginning
            if not line or line.startswith('#'):
                if description_lines:  # If we already have content, stop at next header
                    break
                continue
            
            # Skip markdown metadata/frontmatter
            if line.startswith('---') and not in_content:
                continue
                
            in_content = True
            description_lines.append(line)
            
            # Stop after collecting a reasonable amount of text (2-3 sentences)
            if len(' '.join(description_lines)) > 200:
                break
                
            # Stop at the first empty line after we have some content
            if not line and description_lines:
                break
        
        description = ' '.join(description_lines).strip()
        
        # Limit description length
        if len(description) > 300:
            description = description[:297] + '...'
        
        return description if description else None

    def _add_markdown_resource(
        self,
        name,
        title,
        path,
        description,
        content,
        encoding='utf-8',
    ):
        """Helper to add a markdown file resource."""
        file_size = len(content)
        line_count = len(content.splitlines())

        md_resource = {
            'name': name,
            'title': title,
            'path': path,
            'resource_type': 'markdown_document',
            'description': description,
            'content': content,
            'file_size': file_size,
            'line_count': line_count,
            'raw_data': {
                'content': content,
                'title': title,
                'file_path': path,
                'file_size': file_size,
                'line_count': line_count,
                'encoding': encoding,
            },
            'depends_on': [],
            'columns': [],
        }

        self.resources['markdown_documents'].append(md_resource)

        # Store description in docs_by_resource for consistency
        if description:
            self.docs_by_resource[f"markdown_document.{path}"] = description

    def get_summary(self):
        """Get a summary of all Markdown resources found."""
        total_files = len(self.resources['markdown_documents'])
        total_size = sum(doc.get('file_size', 0) for doc in self.resources['markdown_documents'])
        
        return {
            'markdown_documents': total_files,
            'total_file_size_bytes': total_size,
            'average_file_size_bytes': total_size // total_files if total_files > 0 else 0
        }