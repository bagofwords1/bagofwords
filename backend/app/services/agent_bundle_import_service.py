"""Import an agent from the zip bundle that "Export agent" produces.

Bundle layout (see ``InstructionService.export_agent_bundle_zip``)::

    agent.yaml            the agent manifest (connections by NAME, active tables
                          as ``{connection}.{schema}.{table}``, tools, members)
    instructions/**.md    one markdown file per instruction, YAML frontmatter;
                          sub-folders mirror the agent's instruction folders
    evals/*.yaml          one test suite per file (TestSuiteService YAML)
    queries/*.yaml        one saved query (Entity) per file

The import always creates a NEW agent. Everything that names something in the
source environment is re-pointed here:

- connections: the caller maps each bundle connection name to a connection in
  this organization (or skips it); table patterns and tool overlays are
  rewritten onto the mapped connection's name before the manifest is applied
  through ``AgentYamlService.apply`` — the same path as ``POST /agents/apply``.
- members / tools that do not exist here are dropped with a warning instead of
  failing the import (a bundle is moved between environments by design).
- instruction table references are re-resolved by table name against the new
  agent's tables; instruction folders are recreated from the zip paths.
- eval suites point at the new agent (and get a unique suite name, since suite
  import upserts by name and must never overwrite an existing suite).
"""

from __future__ import annotations

import copy
import io
import logging
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_bundle_import_schema import (
    AgentBundleCounts,
    AgentBundleImportOptions,
    AgentBundleImportResult,
    AgentBundlePreview,
    BundleConnectionPreview,
)
from app.schemas.agent_manifest_schema import (
    AgentManifest,
    ApplyErrorCode,
    ApplyStatus,
)
from app.services.agent_yaml_service import (
    AgentYamlService,
    ManifestResolver,
    _glob_match,
    _table_fqn,
)

logger = logging.getLogger(__name__)

# Bounds for an uploaded bundle. An export of a very large agent is a few MB;
# these leave ample headroom while refusing zip bombs.
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_BUNDLE_FILES = 10_000
_MISSING_SAMPLE = 20

_INSTRUCTION_STATUSES = {"published", "draft", "archived"}
_LOAD_MODES = {"always", "intelligent", "disabled"}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class BundleInstruction:
    path: str
    folder: Tuple[str, ...]
    text: str
    title: Optional[str] = None
    category: str = "general"
    status: str = "published"
    load_mode: str = "always"
    kind: str = "instruction"
    references: List[str] = field(default_factory=list)


@dataclass
class ParsedBundle:
    manifest: AgentManifest
    instructions: List[BundleInstruction] = field(default_factory=list)
    evals: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    queries: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _invalid(message: str) -> AppError:
    return AppError.bad_request(ErrorCode.AGENT_IMPORT_INVALID_BUNDLE, message)


def _split_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """``---\\n<yaml>\\n---\\n\\n<body>`` -> (frontmatter, body). A file
    without a frontmatter fence is all body."""
    text = content.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_raw = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                fm = yaml.safe_load(fm_raw) or {}
            except yaml.YAMLError:
                fm = {}
            return (fm if isinstance(fm, dict) else {}), body.strip()
    return {}, text.strip()


def parse_bundle(data: bytes) -> ParsedBundle:
    if not data:
        raise _invalid("The uploaded file is empty.")
    if len(data) > MAX_BUNDLE_BYTES:
        raise _invalid("The bundle is too large.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise _invalid("The uploaded file is not a zip archive.")

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_BUNDLE_FILES:
        raise _invalid("The bundle has too many files.")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED_BYTES:
        raise _invalid("The bundle is too large once extracted.")

    # Normalize names; drop OS junk and anything trying to escape the archive.
    files: Dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = info.filename.replace("\\", "/").lstrip("/")
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts):
            continue
        if parts[0] == "__MACOSX" or parts[-1].startswith("."):
            continue
        files[name] = info

    # A bundle re-zipped from its extracted folder nests everything under one
    # top-level directory: accept the shallowest agent.yaml as the root.
    manifest_paths = sorted(
        (n for n in files if n == "agent.yaml" or n.endswith("/agent.yaml")),
        key=lambda n: n.count("/"),
    )
    if not manifest_paths:
        raise _invalid("agent.yaml is missing — is this an agent export bundle?")
    manifest_path = manifest_paths[0]
    root = manifest_path[: -len("agent.yaml")]

    def read_text(name: str) -> str:
        return zf.read(files[name]).decode("utf-8")

    try:
        raw_manifest = yaml.safe_load(read_text(manifest_path))
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise _invalid(f"agent.yaml could not be read: {e}")
    if not isinstance(raw_manifest, dict):
        raise _invalid("agent.yaml must be a mapping.")
    try:
        manifest = AgentManifest.model_validate(raw_manifest)
    except ValidationError as e:
        raise _invalid(f"agent.yaml is not a valid agent manifest: {e.errors()[0].get('msg')}")

    bundle = ParsedBundle(manifest=manifest)

    for name in sorted(files):
        if not name.startswith(root) or name == manifest_path:
            continue
        rel = name[len(root):]
        section, _, rest = rel.partition("/")
        if not rest:
            continue
        try:
            if section == "instructions" and rest.endswith(".md"):
                fm, body = _split_frontmatter(read_text(name))
                if not body:
                    bundle.warnings.append(f"{rel}: empty instruction skipped")
                    continue
                folder = tuple(rest.split("/")[:-1])
                refs = fm.get("references") or []
                status = str(fm.get("status") or "published")
                load_mode = str(fm.get("load_mode") or "always")
                bundle.instructions.append(
                    BundleInstruction(
                        path=rel,
                        folder=folder,
                        text=body,
                        # Title only from frontmatter: the export names an
                        # untitled instruction's file by its id, which is not
                        # a title.
                        title=str(fm["title"]) if fm.get("title") else None,
                        category=str(fm.get("category") or "general"),
                        status=status if status in _INSTRUCTION_STATUSES else "published",
                        load_mode=load_mode if load_mode in _LOAD_MODES else "always",
                        kind=str(fm.get("kind") or "instruction"),
                        references=[str(r) for r in refs if isinstance(r, (str, int))]
                        if isinstance(refs, list) else [],
                    )
                )
            elif section == "evals" and rest.endswith((".yaml", ".yml")):
                doc = yaml.safe_load(read_text(name))
                if isinstance(doc, dict) and doc.get("name"):
                    bundle.evals.append((rel, doc))
                else:
                    bundle.warnings.append(f"{rel}: not a test suite, skipped")
            elif section == "queries" and rest.endswith((".yaml", ".yml")):
                doc = yaml.safe_load(read_text(name))
                if isinstance(doc, dict) and doc.get("code"):
                    bundle.queries.append((rel, doc))
                else:
                    bundle.warnings.append(f"{rel}: not a saved query, skipped")
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            bundle.warnings.append(f"{rel}: could not be read ({type(e).__name__}), skipped")

    return bundle


# ---------------------------------------------------------------------------
# Connection-name rewriting
# ---------------------------------------------------------------------------


def _patterns_for(patterns: Sequence[str], conn_name: str) -> List[str]:
    """The table patterns in ``patterns`` that address ``conn_name``."""
    prefix = conn_name.lower() + "."
    return [p for p in patterns if p.lower().startswith(prefix)]


def _rewrite(pattern: str, old: str, new: str) -> str:
    return new + pattern[len(old):]


def _table_patterns(manifest: AgentManifest) -> Tuple[List[str], List[str]]:
    rules = manifest.tables
    if rules is None:
        return [], []
    return list(rules.include or []), list(rules.exclude or [])


async def _connection_fqns(db: AsyncSession, conn: Connection) -> List[str]:
    rows = (
        await db.execute(
            select(ConnectionTable).where(ConnectionTable.connection_id == str(conn.id))
        )
    ).scalars().all()
    return [_table_fqn(conn.name, ct) for ct in rows]


def _match_stats(patterns: Sequence[str], fqns: Sequence[str]) -> Tuple[int, List[str]]:
    """How many of ``patterns`` match at least one table, and which don't."""
    found = 0
    missing: List[str] = []
    for p in patterns:
        if any(_glob_match(p, f) for f in fqns):
            found += 1
        else:
            missing.append(p)
    return found, missing


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AgentBundleImportService:
    def __init__(self) -> None:
        self._yaml = AgentYamlService()

    # ----- shared lookups -------------------------------------------------

    async def _org_connections(self, db: AsyncSession, organization: Organization) -> List[Connection]:
        return list(
            (
                await db.execute(
                    select(Connection).where(
                        Connection.organization_id == str(organization.id),
                        Connection.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        )

    async def _name_taken(self, db: AsyncSession, organization: Organization, name: str) -> bool:
        return (
            await db.execute(
                select(DataSource.id).where(
                    DataSource.organization_id == organization.id,
                    DataSource.name == name,
                ).limit(1)
            )
        ).scalar_one_or_none() is not None

    async def _free_name(self, db: AsyncSession, organization: Organization, name: str) -> str:
        if not await self._name_taken(db, organization, name):
            return name
        n = 1
        while True:
            candidate = f"{name} (imported)" if n == 1 else f"{name} (imported {n})"
            if not await self._name_taken(db, organization, candidate):
                return candidate
            n += 1

    @staticmethod
    def _suggest(name: str, bundle_count: int, conns: List[Connection]) -> Optional[Connection]:
        """Same name first (exact, then case-insensitive); with a single
        connection on both sides, that one."""
        for c in conns:
            if c.name == name:
                return c
        for c in conns:
            if (c.name or "").casefold() == name.casefold():
                return c
        if bundle_count == 1 and len(conns) == 1:
            return conns[0]
        return None

    async def _resolve_mapping(
        self,
        db: AsyncSession,
        organization: Organization,
        manifest: AgentManifest,
        options: AgentBundleImportOptions,
    ) -> Tuple[Dict[str, Optional[Connection]], Dict[str, bool]]:
        """bundle connection name -> Connection (None = skipped/unmapped),
        plus which of those were server suggestions."""
        conns = await self._org_connections(db, organization)
        by_id = {str(c.id): c for c in conns}
        mapping: Dict[str, Optional[Connection]] = {}
        suggested: Dict[str, bool] = {}
        for name in manifest.connections:
            if name in options.connection_map:
                cid = options.connection_map[name]
                if cid is None:
                    mapping[name] = None
                elif str(cid) in by_id:
                    mapping[name] = by_id[str(cid)]
                else:
                    raise AppError.bad_request(
                        ErrorCode.AGENT_IMPORT_NO_CONNECTION,
                        f"Connection '{cid}' was not found in this organization.",
                    )
                suggested[name] = False
            else:
                mapping[name] = self._suggest(name, len(manifest.connections), conns)
                suggested[name] = mapping[name] is not None
        return mapping, suggested

    async def _can_build_on(
        self, db: AsyncSession, organization: Organization, user: User, conn: Connection
    ) -> bool:
        from app.core.permissions_decorator import check_resource_permissions

        try:
            await check_resource_permissions(
                db, str(user.id), str(organization.id),
                "connection", [str(conn.id)], "create_data_sources",
            )
            return True
        except HTTPException:
            return False

    # ----- preview --------------------------------------------------------

    async def preview(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        data: bytes,
        options: AgentBundleImportOptions,
    ) -> AgentBundlePreview:
        bundle = parse_bundle(data)
        manifest = bundle.manifest
        mapping, suggested = await self._resolve_mapping(db, organization, manifest, options)
        include, exclude = _table_patterns(manifest)

        conn_previews: List[BundleConnectionPreview] = []
        for name in manifest.connections:
            patterns = _patterns_for(include, name)
            p = BundleConnectionPreview(
                name=name,
                tables_total=len(patterns),
                tools_total=sum(
                    len(o.allow) + len(o.confirm) + len(o.deny)
                    for k, o in manifest.tools.items() if k == name
                ),
                suggested=suggested.get(name, False),
            )
            conn = mapping.get(name)
            if conn is not None:
                p.mapped_connection_id = str(conn.id)
                if not await self._can_build_on(db, organization, user, conn):
                    p.error = "no_access"
                fqns = await _connection_fqns(db, conn)
                if not fqns:
                    p.indexing_pending = True
                else:
                    rewritten = [_rewrite(x, name, conn.name) for x in patterns]
                    found, missing = _match_stats(rewritten, fqns)
                    p.tables_found = found
                    # Show the table part only — the connection prefix is noise.
                    p.missing_tables = [
                        m[len(conn.name) + 1:] for m in missing[:_MISSING_SAMPLE]
                    ]
            conn_previews.append(p)

        members_unresolved = await self._unresolved_members(db, organization, manifest)
        folders = {i.folder[:k] for i in bundle.instructions for k in range(1, len(i.folder) + 1)}
        name = (options.name or manifest.name).strip() or manifest.name
        taken = await self._name_taken(db, organization, name)
        return AgentBundlePreview(
            agent_name=manifest.name,
            suggested_name=await self._free_name(db, organization, name) if taken else name,
            name_taken=taken,
            description=manifest.description,
            connections=conn_previews,
            counts=AgentBundleCounts(
                instructions=len(bundle.instructions),
                folders=len(folders),
                evals=len(bundle.evals),
                eval_cases=sum(len(d.get("cases") or []) for _, d in bundle.evals),
                queries=len(bundle.queries),
                conversation_starters=len(manifest.conversation_starters),
                members=len(manifest.members),
            ),
            members_unresolved=members_unresolved,
            warnings=bundle.warnings,
        )

    async def _unresolved_members(
        self, db: AsyncSession, organization: Organization, manifest: AgentManifest
    ) -> List[str]:
        if not manifest.members:
            return []
        probe = AgentManifest(name=manifest.name, members=manifest.members)
        _, errors = await ManifestResolver(db, organization).resolve(probe)
        out: List[str] = []
        for e in errors:
            if e.code in (ApplyErrorCode.USER_NOT_FOUND, ApplyErrorCode.GROUP_NOT_FOUND):
                out.append(str(e.value))
        return out

    # ----- import ---------------------------------------------------------

    async def import_bundle(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        data: bytes,
        options: AgentBundleImportOptions,
    ) -> AgentBundleImportResult:
        bundle = parse_bundle(data)
        src = bundle.manifest

        name = (options.name or src.name).strip()
        if not name:
            raise _invalid("The agent name must not be empty.")
        if await self._name_taken(db, organization, name):
            raise AppError.conflict(
                ErrorCode.AGENT_IMPORT_NAME_TAKEN,
                f"An agent named '{name}' already exists.",
                name=name,
            )

        mapping, _ = await self._resolve_mapping(db, organization, src, options)
        mapped = {old: c for old, c in mapping.items() if c is not None}
        if not mapped:
            raise AppError.bad_request(
                ErrorCode.AGENT_IMPORT_NO_CONNECTION,
                "Map at least one of the bundle's connections to a connection in this organization.",
            )
        # Same bar as POST /data_sources on existing connections: every linked
        # connection must allow building agents on it.
        from app.core.permissions_decorator import check_resource_permissions

        await check_resource_permissions(
            db, str(user.id), str(organization.id),
            "connection", sorted({str(c.id) for c in mapped.values()}), "create_data_sources",
        )

        warnings: List[str] = list(bundle.warnings)
        manifest_dict, rewritten_patterns = self._rewrite_manifest(src, name, mapping, options)
        skipped = [old for old, c in mapping.items() if c is None]
        if skipped:
            warnings.append(f"Skipped connections: {', '.join(skipped)}")

        # Dry-run first: members and tools that do not exist in this
        # environment are dropped (with a warning) rather than failing.
        for _ in range(3):
            dry = await self._yaml.apply(
                db, organization, user, yaml.safe_dump(manifest_dict, allow_unicode=True),
                dry_run=True,
            )
            if dry.status != ApplyStatus.ERROR:
                break
            pruned = self._prune_unresolvable(manifest_dict, dry.errors, warnings)
            if not pruned:
                raise AppError(
                    ErrorCode.AGENT_IMPORT_FAILED,
                    "; ".join(e.message for e in dry.errors) or "The agent could not be created.",
                    status_code=400,
                )

        overrides: Dict[str, Any] = {"kind": "agent"}
        if options.folder_id:
            overrides["folder_id"] = options.folder_id
        result = await self._yaml.apply(
            db, organization, user, yaml.safe_dump(manifest_dict, allow_unicode=True),
            create_overrides=overrides,
        )
        if result.status != ApplyStatus.CREATED or not result.id:
            raise AppError(
                ErrorCode.AGENT_IMPORT_FAILED,
                "; ".join(e.message for e in result.errors) or "The agent could not be created.",
                status_code=400,
            )
        for w in result.warnings:
            warnings.append(w.message)
        ds_id = str(result.id)

        out = AgentBundleImportResult(data_source_id=ds_id, name=result.name or name)

        # Table outcome against what actually landed.
        active_names = (
            await db.execute(
                select(func.count(DataSourceTable.id)).where(
                    DataSourceTable.datasource_id == ds_id,
                    DataSourceTable.is_active == True,  # noqa: E712
                )
            )
        ).scalar() or 0
        out.tables_active = int(active_names)
        for old, conn in mapped.items():
            fqns = await _connection_fqns(db, conn)
            if fqns:
                _, missing = _match_stats(rewritten_patterns.get(old, []), fqns)
                out.tables_missing += len(missing)

        ds = await db.get(DataSource, ds_id)

        if options.include_instructions and bundle.instructions:
            await self._import_instructions(db, organization, user, ds, bundle, options, out)
        if options.include_evals and bundle.evals:
            await self._import_evals(db, organization, user, src.name, out.name, bundle, out)
        if options.include_queries and bundle.queries:
            await self._import_queries(db, organization, user, ds_id, bundle, options, out)

        out.warnings = warnings + out.warnings
        return out

    # ----- manifest -------------------------------------------------------

    @staticmethod
    def _rewrite_manifest(
        src: AgentManifest,
        name: str,
        mapping: Dict[str, Optional[Connection]],
        options: AgentBundleImportOptions,
    ) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
        include, exclude = _table_patterns(src)
        new_include: List[str] = []
        new_exclude: List[str] = []
        per_conn: Dict[str, List[str]] = {}
        conn_names: List[str] = []
        tools: Dict[str, Any] = {}
        for old, conn in mapping.items():
            if conn is None:
                continue
            if conn.name not in conn_names:
                conn_names.append(conn.name)
            inc = [_rewrite(p, old, conn.name) for p in _patterns_for(include, old)]
            per_conn[old] = inc
            new_include.extend(inc)
            new_exclude.extend(_rewrite(p, old, conn.name) for p in _patterns_for(exclude, old))
            if old in src.tools:
                tools[conn.name] = src.tools[old].model_dump()

        manifest: Dict[str, Any] = {
            "name": name,
            "description": src.description,
            "context": src.context,
            "is_public": src.is_public,
            "use_llm_sync": src.use_llm_sync,
            "connections": conn_names,
            # Exact active set. An empty include deactivates everything — the
            # source agent had no active tables on these connections (an
            # absent `tables` would mean "all tables" to apply).
            "tables": {"include": sorted(set(new_include)), "exclude": sorted(set(new_exclude))},
            "tools": tools,
            "conversation_starters": list(src.conversation_starters)
            if options.include_conversation_starters else [],
            "members": [m.model_dump(exclude_none=True) for m in src.members],
        }
        return manifest, per_conn

    @staticmethod
    def _prune_unresolvable(manifest: Dict[str, Any], errors, warnings: List[str]) -> bool:
        """Drop members/tools the dry-run could not resolve. Returns False when
        an error is not one we can prune (the import must fail)."""
        drop_members: set = set()
        drop_tools: List[Tuple[str, str, Any]] = []
        drop_tool_conns: set = set()
        for e in errors:
            loc = list(e.loc or [])
            if e.code in (ApplyErrorCode.USER_NOT_FOUND, ApplyErrorCode.GROUP_NOT_FOUND) and len(loc) >= 2:
                drop_members.add(loc[1])
                warnings.append(f"Member not found here, left out: {e.value}")
            elif e.code == ApplyErrorCode.TOOL_NOT_FOUND and len(loc) >= 4:
                drop_tools.append((loc[1], loc[2], e.value))
                warnings.append(f"Tool not found on '{loc[1]}', left out: {e.value}")
            elif e.code == ApplyErrorCode.CONNECTION_TYPE_MISMATCH and len(loc) >= 2:
                drop_tool_conns.add(loc[1])
                warnings.append(f"Tool settings dropped: '{loc[1]}' does not provide tools")
            else:
                return False
        if drop_members:
            manifest["members"] = [
                m for i, m in enumerate(manifest.get("members") or []) if i not in drop_members
            ]
        for conn_name, bucket, value in drop_tools:
            overlay = (manifest.get("tools") or {}).get(conn_name)
            if overlay and value in overlay.get(bucket, []):
                overlay[bucket] = [v for v in overlay[bucket] if v != value]
        for conn_name in drop_tool_conns:
            (manifest.get("tools") or {}).pop(conn_name, None)
        return True

    # ----- instructions ---------------------------------------------------

    async def _import_instructions(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        ds: DataSource,
        bundle: ParsedBundle,
        options: AgentBundleImportOptions,
        out: AgentBundleImportResult,
    ) -> None:
        from app.models.instruction_directory import InstructionDirectoryPlacement
        from app.schemas.instruction_directory_schema import InstructionDirectoryCreate
        from app.schemas.instruction_reference_schema import InstructionReferenceCreate
        from app.schemas.instruction_schema import InstructionCreate
        from app.services.build_service import BuildService
        from app.services.instruction_directory_service import InstructionDirectoryService
        from app.services.instruction_service import InstructionService

        ds_id = str(ds.id)
        service = InstructionService()
        build_service = BuildService()
        dir_service = InstructionDirectoryService()

        # Table references resolve by table name within the new agent — the
        # export writes each reference's display text (the table name).
        tables = (
            await db.execute(
                select(DataSourceTable.id, DataSourceTable.name, DataSourceTable.is_active)
                .where(DataSourceTable.datasource_id == ds_id)
            )
        ).all()
        by_name: Dict[str, str] = {}
        by_fold: Dict[str, str] = {}
        # Active tables win over inactive ones of the same name.
        for tid, tname, active in sorted(tables, key=lambda r: bool(r[2])):
            by_name[tname] = str(tid)
            by_fold[(tname or "").casefold()] = str(tid)

        # Folders: create each path once, parents first.
        dir_ids: Dict[Tuple[str, ...], str] = {}
        paths = sorted(
            {i.folder[:k] for i in bundle.instructions for k in range(1, len(i.folder) + 1)},
            key=len,
        )
        for path in paths:
            try:
                d = await dir_service.create(
                    db, organization, user,
                    InstructionDirectoryCreate(
                        name=path[-1][:100], data_source_id=ds_id,
                        parent_id=dir_ids.get(path[:-1]),
                    ),
                )
                dir_ids[path] = str(d.id)
                out.folders_created += 1
            except Exception as e:  # a folder is cosmetic — never fail the import
                logger.warning("agent import: folder %s not created: %s", "/".join(path), e)
                await db.rollback()

        # One fresh build for the whole import, finalized once at the end — a
        # reused draft build could carry the user's unrelated pending edits.
        build = await build_service.create_build(
            db, str(organization.id), source="user", user_id=str(user.id)
        )
        placements: List[Tuple[str, str]] = []
        for item in bundle.instructions:
            refs: List[InstructionReferenceCreate] = []
            for label in item.references:
                tid = by_name.get(label) or by_fold.get(label.casefold())
                if tid:
                    refs.append(InstructionReferenceCreate(
                        object_type="datasource_table", object_id=tid, display_text=label,
                    ))
                    out.references_resolved += 1
                else:
                    out.references_unresolved += 1
            if item.status == "archived":
                status = "archived"
            else:
                status = item.status if options.publish_instructions else "draft"
            try:
                created = await service.create_instruction(
                    db=db,
                    instruction_data=InstructionCreate(
                        text=item.text,
                        title=item.title,
                        category=item.category,
                        status=status,
                        load_mode=item.load_mode,
                        kind=item.kind if item.kind in ("instruction", "skill") else "instruction",
                        data_source_ids=[ds_id],
                        references=refs,
                    ),
                    current_user=user,
                    organization=organization,
                    build=build,
                    auto_finalize=False,
                )
                out.instructions_created += 1
                did = dir_ids.get(item.folder)
                if did:
                    placements.append((str(created.id), did))
            except Exception as e:
                out.instructions_failed += 1
                logger.warning("agent import: instruction %s failed: %s", item.path, e)
                try:
                    await db.rollback()
                except Exception:
                    pass

        # New instructions have no placement anywhere yet, so writing the
        # (instruction, directory) edges directly keeps the service's
        # one-placement-per-scope invariant without N tree reloads.
        if placements:
            for iid, did in placements:
                db.add(InstructionDirectoryPlacement(instruction_id=iid, directory_id=did))
            await db.commit()

        if out.instructions_created:
            perms = await service._get_user_permissions(db, user, organization)
            build = await build_service.get_build(db, str(build.id))
            ok = await service._auto_finalize_build(db, build, user, perms)
            if not ok:
                out.warnings.append(
                    "Instructions were imported but are waiting for approval in a pending build."
                )
        if out.instructions_failed:
            out.warnings.append(f"{out.instructions_failed} instruction(s) could not be imported.")
        if out.references_unresolved:
            out.warnings.append(
                f"{out.references_unresolved} table reference(s) in instructions did not match a table here."
            )

    # ----- evals ----------------------------------------------------------

    async def _import_evals(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        old_agent: str,
        new_agent: str,
        bundle: ParsedBundle,
        out: AgentBundleImportResult,
    ) -> None:
        from app.models.eval import TestSuite
        from app.services.test_suite_service import TestSuiteService

        suite_service = TestSuiteService()
        existing_agents = set(
            (
                await db.execute(
                    select(DataSource.name).where(DataSource.organization_id == organization.id)
                )
            ).scalars().all()
        )

        def remap(slugs: Optional[List[str]]) -> Optional[List[str]]:
            if slugs is None:
                return None
            outl: List[str] = []
            for s in slugs:
                t = new_agent if s == old_agent else s
                if t in existing_agents and t not in outl:
                    outl.append(t)
            return outl

        async def free_suite_name(base: str) -> str:
            # Every suite name, deleted ones included: import_yaml upserts by
            # (org, name) without looking at deleted_at.
            names = set(
                (
                    await db.execute(
                        select(TestSuite.name).where(
                            TestSuite.organization_id == str(organization.id),
                        )
                    )
                ).scalars().all()
            )
            if base not in names:
                return base
            cand = f"{base} ({new_agent})"
            n = 2
            while cand in names:
                cand = f"{base} ({new_agent} {n})"
                n += 1
            return cand

        async def home(imported: Dict[str, Any]) -> None:
            # Home the suite on the new agent (the per-agent evals list and a
            # later re-export find suites by this column, not by their cases).
            suite = await db.get(TestSuite, imported["suite_id"])
            if suite is not None:
                suite.data_source_id = new_agent_id
                await db.commit()

        new_agent_id = (
            await db.execute(
                select(DataSource.id).where(
                    DataSource.organization_id == organization.id,
                    DataSource.name == new_agent,
                )
            )
        ).scalar_one()

        for rel, doc in bundle.evals:
            doc = copy.deepcopy(doc)
            doc["name"] = await free_suite_name(str(doc.get("name")))
            doc["data_source_slugs"] = remap(doc.get("data_source_slugs") or []) or [new_agent]
            for case in doc.get("cases") or []:
                if isinstance(case, dict) and case.get("data_source_slugs") is not None:
                    case["data_source_slugs"] = remap(case["data_source_slugs"]) or [new_agent]
            try:
                await home(await suite_service.import_yaml(
                    db, str(organization.id), user, yaml.safe_dump(doc, allow_unicode=True)
                ))
                out.evals_created += 1
            except HTTPException as e:
                # Model slugs (``provider/model``) are environment-specific:
                # retry once on the org's default model before giving up.
                stripped = _strip_models(doc)
                try:
                    if not stripped:
                        raise e
                    await home(await suite_service.import_yaml(
                        db, str(organization.id), user, yaml.safe_dump(doc, allow_unicode=True)
                    ))
                    out.evals_created += 1
                    out.warnings.append(f"{rel}: model settings dropped (not available here)")
                except Exception as e2:
                    await db.rollback()
                    out.warnings.append(f"{rel}: eval suite not imported ({getattr(e2, 'detail', e2)})")
            except Exception as e:
                await db.rollback()
                out.warnings.append(f"{rel}: eval suite not imported ({e})")

    # ----- queries --------------------------------------------------------

    async def _import_queries(
        self,
        db: AsyncSession,
        organization: Organization,
        user: User,
        ds_id: str,
        bundle: ParsedBundle,
        options: AgentBundleImportOptions,
        out: AgentBundleImportResult,
    ) -> None:
        from app.core.permission_resolver import resolve_permissions
        from app.schemas.entity_schema import EntityCreate
        from app.services.entity_service import EntityService

        resolved = await resolve_permissions(db, str(user.id), str(organization.id))
        can_publish = resolved.has_resource_permission("data_source", ds_id, "create_entities")
        service = EntityService()
        for rel, doc in bundle.queries:
            status = str(doc.get("status") or "draft")
            if not options.publish_instructions or status not in ("draft", "published"):
                status = "draft"
            try:
                await service.create_entity(
                    db,
                    EntityCreate(
                        type=str(doc.get("type") or "model"),
                        title=str(doc.get("title") or ""),
                        slug=str(doc.get("slug") or "") or None,
                        description=doc.get("description"),
                        tags=[str(t) for t in (doc.get("tags") or [])],
                        code=str(doc.get("code")),
                        parameters=doc.get("parameters") if isinstance(doc.get("parameters"), list) else None,
                        status=status,
                        data_source_ids=[ds_id],
                    ),
                    user,
                    organization,
                    creator_can_publish=can_publish,
                )
                out.queries_created += 1
            except Exception as e:
                await db.rollback()
                out.warnings.append(f"{rel}: saved query not imported ({e})")


def _strip_models(doc: Dict[str, Any]) -> bool:
    """Remove every ``prompt.model`` in a suite doc. Returns True if any."""
    changed = False
    for case in doc.get("cases") or []:
        if not isinstance(case, dict):
            continue
        prompts = []
        if isinstance(case.get("prompt"), dict):
            prompts.append(case["prompt"])
        for turn in case.get("turns") or []:
            if isinstance(turn, dict) and isinstance(turn.get("prompt"), dict):
                prompts.append(turn["prompt"])
        for p in prompts:
            if p.pop("model", None) is not None:
                changed = True
    return changed
