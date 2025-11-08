from typing import List, Optional, Union, Any, Dict
from pydantic import BaseModel


class ResourcePreview(BaseModel):
    name: Optional[str] = None
    resource_type: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    source_name: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None
    raw_data: Optional[Dict[str, Any]] = None


class ReadResourcesInput(BaseModel):
    """Simplified input schema for the read_resources tool.

    - query: names or regex patterns applied globally
    - data_source_id: optional scope to a single data source
    - git_repository_id: optional scope to a single git repository
    """
    query: Union[str, List[str]]
    data_source_id: Optional[str] = None
    git_repository_id: Optional[str] = None


class ReadResourcesOutput(BaseModel):
    """Output schema for the read_resources tool."""
    resources_excerpt: str               # XML-like combined excerpt (<resources> ... </resources>)
    truncated: bool                      # true if any repository exceeded the sample/index caps
    searched_repos: int                  # number of repositories/data sources searched
    searched_resources_est: int          # estimated count of matched resources across repos
    errors: List[str] = []
    # Echoed input for UI hydration (can be a list of per-source dicts)
    search_query: Optional[Any] = None
    # Lightweight preview for UI cards
    top_results: List[ResourcePreview] = []


