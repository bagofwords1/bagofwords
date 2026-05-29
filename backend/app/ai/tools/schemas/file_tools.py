"""Input/output schemas for file-source agent tools (SharePoint, OneDrive,
Google Drive, and any future client declaring the LIST_FILES / READ_FILE /
SEARCH_FILES capabilities)."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    id: str = Field(..., description="Opaque file identifier — pass to read_file.")
    name: str
    path: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    modified_at: Optional[str] = None
    web_url: Optional[str] = None


# ----------------------------------------------------------- list_files


class ListFilesInput(BaseModel):
    connection_id: str = Field(
        ...,
        description=(
            "ID of the file-source connection to list (SharePoint, OneDrive, "
            "Google Drive). The connection must be attached to the current agent."
        ),
    )
    folder_id: Optional[str] = Field(
        None,
        description=(
            "Optional folder ID returned by a previous list_files call. "
            "Leave blank to use the connection's configured root."
        ),
    )
    recursive: bool = Field(
        False,
        description="Include files in subfolders. Off by default to keep results focused.",
    )


class ListFilesOutput(BaseModel):
    success: bool
    connection_id: str
    file_count: int = 0
    files: List[FileEntry] = Field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None


# ----------------------------------------------------------- read_file


class ReadFileInput(BaseModel):
    connection_id: str = Field(..., description="ID of the file-source connection.")
    file_id: str = Field(
        ...,
        description="File ID returned by list_files or search_files.",
    )
    sheet: Optional[str] = Field(
        None,
        description="For Excel / Google Sheets only: sheet name. Defaults to the first sheet.",
    )
    max_rows: int = Field(
        default=1000,
        ge=1,
        le=100000,
        description="For tabular files: max rows to return. Extra rows are dropped; truncated=true is set.",
    )
    max_chars: int = Field(
        default=20000,
        ge=100,
        le=500000,
        description="For text files: max characters to return.",
    )


class ReadFileOutput(BaseModel):
    success: bool
    connection_id: str
    file_id: str
    file_name: Optional[str] = None
    content_type: str = Field(
        default="unknown",
        description="One of: tabular, text, json, binary, unknown.",
    )
    csv: Optional[str] = None  # for tabular
    text: Optional[str] = None  # for text/json/document
    row_count: Optional[int] = None
    col_count: Optional[int] = None
    truncated: bool = False
    byte_count: Optional[int] = None  # for binary
    error: Optional[str] = None


# --------------------------------------------------------- search_files


class SearchFilesInput(BaseModel):
    connection_id: str = Field(..., description="ID of the file-source connection.")
    query: str = Field(..., description="Free-text search query — matches filename / content depending on the provider.")
    max_results: int = Field(default=50, ge=1, le=500)


class SearchFilesOutput(BaseModel):
    success: bool
    connection_id: str
    query: str
    file_count: int = 0
    files: List[FileEntry] = Field(default_factory=list)
    error: Optional[str] = None
