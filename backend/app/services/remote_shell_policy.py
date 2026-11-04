"""Deterministic remote shell policy; command patterns are not an OS sandbox."""

import fnmatch
import json
import re
import shlex
import time

from sqlalchemy import select

from app.models.user_connection_tool_preference import UserConnectionToolPreference
from app.schemas.data_sources.remote_server import ShellRule


def config_dict(config):
    return json.loads(config) if isinstance(config, str) else dict(config or {})


def rule_matches(rule, command, cwd=None):
    if rule.get("working_directory") is not None and rule["working_directory"] != cwd:
        return False
    pattern = rule["pattern"]
    if rule.get("match", "exact") == "exact":
        return command == pattern
    # An allow-prefix never silently authorizes appended shell programs or
    # expansion. Explicit '*' means unrestricted; exact rules can allow scripts.
    if rule["action"] == "allow" and pattern != "*" and re.search(r"[\n\r;&|<>`$(){}]", command):
        return False
    candidates = [command]
    if rule["action"] == "deny":
        # Check simple command segments too, so a deny cannot be hidden after
        # a separator while a broader personal Allow is active.
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            lexer.commenters = ""
            segment = []
            for token in lexer:
                if token and all(c in ";&|()\n" for c in token):
                    candidates.append(" ".join(segment))
                    segment = []
                else:
                    segment.append(token)
            candidates.append(" ".join(segment))
        except ValueError:
            # Malformed commands are never authorized by a wildcard Allow.
            return True
    return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates)


def resolve_shell_policy(config, command, *, personal=None, user_rules=(), cwd=None):
    c = config_dict(config)
    mode = c.get("shell_policy", "policy")
    rules = c.get("shell_rules") or []
    # Administrator denials are absolute in every mode.
    if any(r["action"] == "deny" and rule_matches(r, command, cwd) for r in rules):
        return "deny"
    if mode in ("allow", "deny"):
        return mode
    if mode == "strict":
        return "deny" if personal == "deny" else "ask"
    default = "ask"
    for r in rules:
        if rule_matches(r, command, cwd):
            default = r["action"]
    if personal in ("allow", "ask", "deny"):
        default = personal
    for r in user_rules:
        if rule_matches(r, command, cwd):
            default = r["action"]
    return default


async def user_shell_rules(db, user_id, tool_id, report_id=None):
    row = (
        await db.execute(
            select(UserConnectionToolPreference)
            .where(
                UserConnectionToolPreference.user_id == str(user_id),
                UserConnectionToolPreference.connection_tool_id == str(tool_id),
                UserConnectionToolPreference.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    now = time.time()
    return (
        [
            r
            for r in (row.command_rules or [])
            if (
                not r.get("session_id")
                or (report_id and r["session_id"] == str(report_id) and r.get("expires_at", 0) > now)
            )
        ]
        if row
        else []
    )


# Stored when a user saves command rules without choosing a personal policy;
# normalize_tool_policy() maps it to "no preference", so admin policy still applies.
INHERIT_POLICY = "inherit"


async def shell_rule_row(db, user_id, tool_id):
    """The user's preference row for a tool, locked for update; created without a personal policy."""
    row = (
        await db.execute(
            select(UserConnectionToolPreference)
            .where(
                UserConnectionToolPreference.user_id == str(user_id),
                UserConnectionToolPreference.connection_tool_id == str(tool_id),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserConnectionToolPreference(
            user_id=str(user_id), connection_tool_id=str(tool_id), policy=INHERIT_POLICY
        )
    row.deleted_at = None
    return row


async def save_shell_rule(db, user_id, tool_id, rule, *, report_id=None):
    clean = ShellRule.model_validate(rule).model_dump()
    row = await shell_rule_row(db, user_id, tool_id)
    if report_id:
        clean.update(session_id=str(report_id), expires_at=time.time() + 8 * 3600)
    rules = [
        r
        for r in (row.command_rules or [])
        if (not r.get("expires_at") or r["expires_at"] > time.time())
        and not (
            r.get("pattern") == clean["pattern"]
            and r.get("match") == clean["match"]
            and r.get("working_directory") == clean["working_directory"]
            and r.get("session_id") == clean.get("session_id")
        )
    ]
    row.command_rules = (rules + [clean])[-200:]
    db.add(row)


async def effective_shell_policy(db, connection, tool, resolution, user, arguments, report_id=None):
    if not tool.is_enabled or not resolution.is_enabled or resolution.admin_policy == "deny":
        return "deny"
    if not config_dict(connection.config).get("enable_shell"):
        return "deny"
    rules = await user_shell_rules(db, user.id, tool.id, report_id) if user else []
    return resolve_shell_policy(
        connection.config,
        arguments.get("command", ""),
        personal=resolution.user_policy,
        user_rules=rules,
        cwd=arguments.get("working_directory"),
    )


def display_policy(connection, tool_name, admin, personal):
    from app.services.tool_policy_service import resolve_effective_policy

    kind = connection.get("type") if isinstance(connection, dict) else getattr(connection, "type", None)
    config = connection.get("config") if isinstance(connection, dict) else getattr(connection, "config", None)
    if kind == "remote_ssh" and tool_name == "execute_shell":
        if admin == "deny" or not config_dict(config).get("enable_shell"):
            return "deny"
        return resolve_shell_policy(config, "", personal=personal)
    return resolve_effective_policy(admin, personal)


async def resolve_shell_confirmation(db, row, *, approved, remember, user_id, rule, report_id=None):
    """Commit the first decision and its remembered rule in one transaction."""
    from datetime import datetime

    from sqlalchemy import or_, update

    from app.models.tool_confirmation import ToolConfirmation

    changed = await db.execute(
        update(ToolConfirmation)
        .where(
            ToolConfirmation.id == row.id,
            ToolConfirmation.status == ToolConfirmation.STATUS_PENDING,
            or_(ToolConfirmation.expires_at.is_(None), ToolConfirmation.expires_at > datetime.utcnow()),
        )
        .values(
            status=ToolConfirmation.STATUS_APPROVED if approved else ToolConfirmation.STATUS_DENIED,
            remember=remember,
            resolved_by_user_id=str(user_id),
            resolved_at=datetime.utcnow(),
        )
    )
    if changed.rowcount and rule is not None:
        await save_shell_rule(db, user_id, row.connection_tool_id, rule, report_id=report_id)
    await db.commit()
    await db.refresh(row)
    return row
