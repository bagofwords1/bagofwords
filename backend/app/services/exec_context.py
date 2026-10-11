"""What running a saved step needs, without assuming a conversation.

StepService._execute_step_code used to take the Report and read its data
sources, files and organization off it. A published dashboard's copies have
no Report (docs/design/dashboards-first-class.md), so the core reads them
from an ExecContext instead. Today every caller builds one from its Report
with `from_report`, which hands the core exactly the values it used to read
itself, at the moment it used to read them: apart from the report's
organization id, nothing is read until the core asks.

`report` stays on the context for the two helpers that are still scoped to a
conversation: load_step resolution (LoadablesResolver) and the bow monitoring
client. Making those release-aware belongs with refreshing published
dashboards (phase 6); until then a context without a report resolves no
load_step reference and installs no bow client.
"""
from collections.abc import Callable, Iterable
from typing import Any

Sources = Iterable[Any] | Callable[[], Iterable[Any]]


class ExecContext:
    def __init__(
        self,
        organization=None,
        *,
        organization_id: str | None = None,
        organization_settings=None,
        credential_user=None,
        data_sources: Sources = (),
        files: Sources = (),
        report=None,
    ):
        self._organization = organization
        self._organization_id = organization_id
        self._organization_settings = organization_settings
        # Whose data-source credentials the run uses.
        self.credential_user = credential_user
        self.report = report
        # A list, or a callable read only when used: callers that pass
        # prebuilt clients never touch data_sources, so a report loaded
        # without them keeps working exactly as before.
        self._data_sources = data_sources
        self._files = files

    @property
    def data_sources(self) -> list:
        return _read(self._data_sources)

    @property
    def files(self) -> list:
        return _read(self._files)

    async def organization(self, db):
        """The given organization, else the one `organization_id` names."""
        if self._organization is None and self._organization_id:
            from app.models.organization import Organization
            self._organization = await db.get(Organization, str(self._organization_id))
        return self._organization

    async def settings(self, db):
        """The given settings, else the organization's own, fetched once."""
        if self._organization_settings is None:
            org = await self.organization(db)
            if org is not None:
                self._organization_settings = await org.get_settings(db)
        return self._organization_settings

    @classmethod
    def from_report(
        cls,
        report,
        *,
        credential_user=None,
        organization=None,
        organization_settings=None,
    ) -> "ExecContext":
        """The context a conversation's own runs use: its data sources, its
        files, its organization (the given one when a caller already loaded
        it), and its scope for load_step and the bow client."""
        return cls(
            organization,
            organization_id=getattr(report, "organization_id", None),
            organization_settings=organization_settings,
            credential_user=credential_user,
            data_sources=lambda: report.data_sources,
            files=lambda: report.files,
            report=report,
        )


def _read(source: Sources) -> list:
    return list((source() if callable(source) else source) or [])
