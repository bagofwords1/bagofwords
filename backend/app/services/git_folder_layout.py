"""Map instruction build targets to repository-relative directories in one organization."""
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select

from app.models.data_source import DataSource
from app.models.folder import Folder


def slug(value):
    clean = re.sub(r'[^\w\s-]', '', unicodedata.normalize('NFC', value or ''))
    return clean.strip().replace(' ', '-').lower()


@dataclass
class GitFolderLayout:
    folders: dict[str, str]
    agents: dict[str, str]
    names: dict[str, str]

    @classmethod
    async def load(cls, db, org_id):
        rows = list(await db.execute(select(Folder.id, Folder.parent_id, Folder.name).where(
            Folder.organization_id == org_id, Folder.deleted_at.is_(None))))
        counts = Counter((parent, slug(name)) for _, parent, name in rows)
        segments = {}
        for fid, parent, name in rows:
            segment = slug(name) or fid
            if counts[parent, slug(name)] > 1 or segment == 'agents':
                segment += '~' + fid
            segments[fid] = (parent, segment)
        folders = {}
        for fid in segments:
            parts, seen, current = [], set(), fid
            while current is not None:
                if current not in segments or current in seen:
                    raise ValueError("Invalid hierarchy in Git folder layout")
                seen.add(current)
                current, segment = segments[current]
                parts.append(segment)
            folders[fid] = 'folders/' + '/'.join(reversed(parts))

        agents = list(await db.execute(select(DataSource.id, DataSource.folder_id, DataSource.name).where(
            DataSource.organization_id == org_id, DataSource.deleted_at.is_(None))))
        counts = Counter(slug(name) for _, _, name in agents)
        paths = {}
        reserved = {'global', 'shared', 'folders', 'general', 'code', 'data-modeling', 'visualizations', 'dashboard', 'system'}
        for aid, parent, name in agents:
            segment = slug(name) or aid
            if counts[slug(name)] > 1 or segment in reserved:
                segment += '-' + aid[:8]
            paths[aid] = f'{folders[parent]}/agents/{segment}' if parent in folders else segment
        return cls(folders, paths, {aid: name for aid, _, name in agents})
