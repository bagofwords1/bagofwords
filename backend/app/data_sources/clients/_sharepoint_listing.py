"""Bounded interactive listing, opted into by SharePoint clients only."""
import asyncio
import fnmatch
from contextlib import closing
from dataclasses import dataclass


@dataclass
class SharepointListingResult:
    files: list[dict]
    stop_reason: str | None = None


class SharepointBoundedListing:
    """Keep interactive limits separate from full indexing and other connectors."""

    async def alist_files_bounded(self, *, folder_id=None, recursive=False, limit=50, name_pattern=None):
        return await asyncio.to_thread(
            self.list_files_bounded, folder_id=folder_id, recursive=recursive,
            limit=limit, name_pattern=name_pattern,
        )

    def list_files_bounded(self, *, folder_id=None, recursive=False, limit=50, name_pattern=None):
        if not 1 <= limit <= 500:
            raise ValueError('SharePoint listing limit must be between 1 and 500.')
        files, seen = [], set()
        pattern = (name_pattern or '').lower()
        # Page size bounds each response; limit bounds matching results across pages.
        # Do not fetch an extra page merely to establish whether more files exist.
        with closing(self._iter_listing_files(folder_id, recursive, min(limit, 200))) as rows:
            for row in rows:
                if row['id'] in seen:
                    continue
                seen.add(row['id'])
                if not pattern or any(fnmatch.fnmatch(str(row.get(key) or '').lower(), pattern)
                                      for key in ('name', 'path')):
                    files.append(row)
                if len(files) >= limit:
                    return SharepointListingResult(files, 'result_limit')
                if len(seen) >= self.max_catalog_objects:
                    return SharepointListingResult(files, 'catalog_limit')
        return SharepointListingResult(files)
