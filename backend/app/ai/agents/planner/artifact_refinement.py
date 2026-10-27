"""Execution-scoped limit for optional screenshot-driven aesthetic edits."""


class ArtifactRefinementBudget:
    """One optional visual refinement per request. It is spent only by a
    refinement that saved: a rejected attempt (a contract error, a failed
    render) leaves it available, so the retry is not refused."""

    def __init__(self) -> None:
        self.used = False

    @staticmethod
    def _is_refinement(tool_name: str, arguments: dict) -> bool:
        return tool_name == "edit_artifact" and (arguments or {}).get("purpose") == "visual_refinement"

    def allow(self, tool_name: str, arguments: dict) -> bool:
        return not (self._is_refinement(tool_name, arguments) and self.used)

    def record(self, tool_name: str, arguments: dict, saved: bool) -> None:
        """After the call: a refinement that saved spends the budget."""
        if saved and self._is_refinement(tool_name, arguments):
            self.used = True
