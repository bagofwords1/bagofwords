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
    name_pattern: Optional[str] = Field(
        None,
        description=(
            "Optional glob pattern (fnmatch syntax) to filter filenames — e.g. "
            "'*.xlsx', 'Book *.xlsx', 'Q?_*.csv'. Case-insensitive. Saves a "
            "roundtrip vs listing everything and filtering client-side."
        ),
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
    connection_id: str = Field(
        ...,
        description=(
            "ID of the file source attached to this agent. Either the "
            "Connection ID or the DataSource (agent) ID is accepted — when "
            "the agent has one file connection, passing the agent's own ID "
            "is the simplest path."
        ),
    )
    file_id: str = Field(
        ...,
        description=(
            "Opaque file ID returned in the `id` field by list_files or "
            "search_files (NOT the readable `name` field). A filename like "
            "'Book 7.xlsx' will be resolved as a fallback, but using the id "
            "is faster and unambiguous."
        ),
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
    # Set when the file was persisted as a session File attached to the
    # current report. Pass this ID to inspect_data / read_excel_as_csv /
    # create_data exactly like a user-uploaded file.
    session_file_id: Optional[str] = Field(
        default=None,
        description="Session file id you can pass to inspect_data / create_data / read_excel_as_csv. None for files that aren't attachable (oversize, unknown binary).",
    )
    error: Optional[str] = None


# --------------------------------------------------------- search_files


class SearchFilesInput(BaseModel):
    connection_id: str = Field(
        ...,
        description=(
            "ID of the file source attached to this agent. Either the "
            "Connection ID or the DataSource (agent) ID is accepted — when "
            "the agent has one file connection, passing the agent's own ID "
            "is the simplest path."
        ),
    )
    query: str = Field(..., description="Free-text search query — matches filename / content depending on the provider.")
    max_results: int = Field(default=50, ge=1, le=500)


class SearchFilesOutput(BaseModel):
    success: bool
    connection_id: str
    query: str
    file_count: int = 0
    files: List[FileEntry] = Field(default_factory=list)
    error: Optional[str] = None


# ----------------------------------------------------------- write_file


class WriteFileInput(BaseModel):
    connection_id: str = Field(
        ...,
        description=(
            "ID of a WRITABLE file source attached to this agent (Connection ID "
            "or the DataSource/agent ID). Only connections with writes enabled "
            "accept this — a read-only source rejects the call."
        ),
    )
    filename: str = Field(
        ...,
        description=(
            "Destination file name, optionally with a relative folder path "
            "(e.g. 'contracts/acme_2025.csv'). Resolved under the connection's "
            "configured root; paths escaping the root are rejected."
        ),
    )
    content: Optional[str] = Field(
        None,
        description=(
            "Text content to write. Provide EITHER content OR source_file_id, "
            "not both. Use content for text/CSV/markdown you generate."
        ),
    )
    source_file_id: Optional[str] = Field(
        None,
        description=(
            "Copy an existing session file into the destination instead of "
            "writing literal text. Pass a session_file_id returned by read_file "
            "(or an uploaded file's id). Use this to 'put' a file you found "
            "into the directory (cp / put). Mutually exclusive with content."
        ),
    )
    folder: Optional[str] = Field(
        None,
        description="Optional destination subfolder under the root. Combined with filename.",
    )
    overwrite: bool = Field(
        False,
        description="Overwrite the target if it already exists. Off by default to avoid clobbering.",
    )


class WriteFileOutput(BaseModel):
    success: bool
    connection_id: str
    file: Optional[FileEntry] = None
    error: Optional[str] = None
