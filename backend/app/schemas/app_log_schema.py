from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class AppLogEntry(BaseModel):
    # ``<file name>:<byte offset>``: stable for as long as the line exists.
    id: str
    timestamp: datetime
    instance_id: str
    level: Optional[str] = None
    logger: Optional[str] = None
    stream: str
    message: str


class AppLogList(BaseModel):
    """Newest first. ``has_more`` is true when the limit cut matching lines off."""

    items: List[AppLogEntry]
    limit: int
    has_more: bool


class AppLogStatus(BaseModel):
    # Hostname of the instance that answered this request.
    instance_id: str
    # Every instance that has log files, for the instance filter.
    instances: List[str]
    # Where the log files are kept (bow_config.system_logs.directory).
    directory: str
    retention_days: int
    max_size_bytes: int
    size_bytes: int
    # This instance's last write to the directory failed; lines are kept in
    # memory and written once it works again.
    write_error: bool = False
    # Lines this instance could not keep since it started (its buffer
    # overflowed while the directory could not be written).
    dropped_lines: int = 0


class AppLogDeleteResult(BaseModel):
    deleted_files: int
