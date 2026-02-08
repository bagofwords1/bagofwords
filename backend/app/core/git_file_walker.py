"""
Git File Walker + Classifier

Walks a cloned git repository, filters by extension allowlist,
reads file content, and classifies each file for UI icons.
"""

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Extensions we index
ALLOWED_EXTENSIONS = {
    '.yml', '.yaml', '.sql', '.py', '.md', '.txt',
    '.json', '.toml', '.cfg', '.lkml', '.sqlx',
    '.tds', '.tdsx', '.csv',
}

# Directories to skip
SKIP_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv',
    '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
    'dist', 'build', '.eggs', '*.egg-info',
}

MAX_FILE_SIZE = 200 * 1024  # 200 KB


@dataclass
class GitFileInfo:
    relative_path: str   # e.g. "my-repo/models/orders.sql"
    content: str
    content_hash: str     # SHA-256
    size_bytes: int
    extension: str
    resource_type: str    # classified type for UI icons


def extract_repo_name(repo_url: str) -> str:
    """Extract repository name from a git URL.

    Handles:
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
    """
    # SSH style
    ssh_match = re.match(r'git@[^:]+:(.+?)(?:\.git)?$', repo_url)
    if ssh_match:
        path = ssh_match.group(1)
        return path.split('/')[-1]

    # HTTPS style
    parsed = urlparse(repo_url)
    path = parsed.path.strip('/')
    name = path.split('/')[-1] if path else 'repo'
    if name.endswith('.git'):
        name = name[:-4]
    return name or 'repo'


def classify_file(
    extension: str,
    content: str,
    is_dbt_project: bool,
) -> str:
    """Classify a file for UI icon display (~30 lines).

    Returns a resource_type string used by the frontend icon map.
    """
    ext = extension.lower()

    if ext == '.lkml':
        return 'lookml_view'
    if ext == '.sqlx':
        return 'dataform_table'
    if ext in ('.tds', '.tdsx'):
        return 'tableau_workbook'
    if ext == '.md':
        return 'markdown_document'

    if ext in ('.yml', '.yaml'):
        # Peek at content to sub-classify YAML
        if _looks_like_dbt_yaml(content):
            return 'dbt_source'
        if _looks_like_snowflake_semantic(content):
            return 'snowflake_semantic_view'
        return 'yaml_file'

    if ext == '.sql':
        return 'dbt_model' if is_dbt_project else 'sql_file'

    # Everything else
    return 'generic_file'


def _looks_like_dbt_yaml(content: str) -> bool:
    """Check if YAML content looks like a dbt schema/source file."""
    # Quick substring checks (avoids parsing YAML)
    for key in ('models:', 'sources:', 'seeds:', 'metrics:', 'exposures:', 'macros:'):
        if key in content:
            return True
    return False


def _looks_like_snowflake_semantic(content: str) -> bool:
    """Check if YAML content looks like a Snowflake semantic view definition."""
    return 'base_table:' in content or 'semantic_view:' in content


def walk_repo_files(
    repo_path: str,
    repo_name: str,
) -> List[GitFileInfo]:
    """Walk a cloned repo directory and return info for each allowed file.

    Args:
        repo_path: Absolute path to the cloned repo on disk.
        repo_name: Name to prefix all paths with (for disambiguation).

    Returns:
        List of GitFileInfo, one per allowed file.
    """
    repo_root = Path(repo_path)

    # Detect dbt project
    is_dbt_project = (repo_root / 'dbt_project.yml').is_file()

    files: List[GitFileInfo] = []

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skipped directories in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith('.egg-info')
        ]

        for fname in filenames:
            fpath = Path(dirpath) / fname
            ext = fpath.suffix.lower()

            if ext not in ALLOWED_EXTENSIONS:
                continue

            # Size guard
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_SIZE or size == 0:
                continue

            # Read content (UTF-8 with latin-1 fallback)
            content = _read_file(fpath)
            if content is None:
                continue

            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()

            rel = fpath.relative_to(repo_root)
            prefixed_path = f"{repo_name}/{rel.as_posix()}"

            resource_type = classify_file(ext, content, is_dbt_project)

            files.append(GitFileInfo(
                relative_path=prefixed_path,
                content=content,
                content_hash=content_hash,
                size_bytes=size,
                extension=ext,
                resource_type=resource_type,
            ))

    logger.info(
        f"Walked repo '{repo_name}': {len(files)} files "
        f"(dbt_project={is_dbt_project})"
    )
    return files


def _read_file(path: Path) -> Optional[str]:
    """Read a file with UTF-8, falling back to latin-1."""
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding='latin-1')
        except Exception as e:
            logger.warning(f"Cannot read {path}: {e}")
            return None
    except Exception as e:
        logger.warning(f"Cannot read {path}: {e}")
        return None
