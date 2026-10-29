"""Explicit contract for version-specific SharePoint APIs. No REST fallback."""
from abc import ABC, abstractmethod


class SharepointAPI(ABC):
    def __init__(self, client):
        self.client = client

    @property
    @abstractmethod
    def description(self):
        pass

    @abstractmethod
    def library_scopes(self):
        """[(library title, server-relative scope folder)] for the configured libraries."""

    @abstractmethod
    def iter_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None, *, page_size=None):
        pass

    def list_files(self, folder_id=None, recursive=None, limit=None, progress_callback=None):
        return list(self.iter_files(folder_id, recursive, limit, progress_callback))

    @abstractmethod
    def read_raw_bytes(self, file_id, *, max_bytes=None):
        pass

    @abstractmethod
    def search_files(self, query, limit=100, **kwargs):
        pass

    @abstractmethod
    def test_connection(self):
        pass
