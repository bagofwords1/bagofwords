from typing import Dict, Callable

from app.ai.tools.answer_question import AnswerQuestionTool
from app.ai.tools.create_widget import CreateWidgetTool

class ToolRegistry:
    """Minimal registry scaffold. Holds a name->factory map.

    AgentV2 can use this to resolve tools. Extend with schemas/retries later.
    """

    def __init__(self) -> None:
        self._factories: Dict[str, Callable] = {}
        # Pre-register common tools for MVP
        self.register("answer_question", lambda: AnswerQuestionTool())
        self.register("create_widget", lambda: CreateWidgetTool())

    def register(self, name: str, factory: Callable) -> None:
        self._factories[name] = factory

    def get(self, name: str):
        factory = self._factories.get(name)
        return factory() if factory else None

    def get_catalog(self, organization=None):
        """Return a list of tool descriptors: [{name, description, schema}].

        Accepts organization for future org-specific availability filtering.
        """
        catalog = []
        for name, factory in self._factories.items():
            # TODO: gate by organization when policies are defined
            try:
                tool = factory()
                desc = getattr(tool, "description", "")
                input_schema = getattr(tool, "input_model", None)
                # We expose JSON schema to the planner as a hint only; planner should not enforce it
                schema_dict = input_schema.model_json_schema() if input_schema else None
            except Exception:
                desc = ""
                schema_dict = None
            catalog.append({"name": name, "description": desc, "schema": schema_dict})
        return catalog

