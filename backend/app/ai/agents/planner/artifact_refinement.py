"""Execution-scoped limit for optional screenshot-driven aesthetic edits."""


class ArtifactRefinementBudget:
    def __init__(self) -> None:
        self.used = False

    def allow(self, tool_name: str, arguments: dict) -> bool:
        if tool_name != "edit_artifact" or arguments.get("purpose") != "visual_refinement":
            return True
        if self.used:
            return False
        self.used = True
        return True
