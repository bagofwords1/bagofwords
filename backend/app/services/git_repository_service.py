"""
GitRepositoryService - Legacy compatibility module

This module redirects to the consolidated GitService.
All Git operations are now handled by git_service.py.
"""

# Re-export the consolidated service for backwards compatibility
from app.services.git_service import GitService, GitService as GitRepositoryService

__all__ = ["GitRepositoryService", "GitService"]
