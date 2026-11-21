"""request_agent_signin — ask the user to sign in to an agent the answer needs.

ACTION tool. Some agents and connectors are backed by a connection each person signs in to
(``auth_policy == "user_required"``, e.g. Power BI on-behalf-of). Until this
user signs in, the agent has no client, so its data is not in the run; the
planner sees it in ``<agents_needing_signin>`` (or as SIGN-IN REQUIRED in a
search_agents result). When the question needs one, this tool shows the user a
Connect card for it and ends the turn with the planner's message, instead of
the planner answering from some other agent or saying the data does not exist.

The ids are re-checked here: only agents in this report's scope that still need
this user's sign-in are shown. Anything else is rejected back to the planner.
"""
import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.request_agent_signin import (
    RequestAgentSigninInput,
    RequestAgentSigninOutput,
    SigninAgentItem,
)

logger = logging.getLogger(__name__)


class RequestAgentSigninTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="request_agent_signin",
            description=(
                "Show the user a Connect (sign-in) button for agents whose data the request "
                "needs but that the user has not signed in to yet (listed in "
                "<agents_needing_signin>, or marked SIGN-IN REQUIRED by search_agents). "
                "Ends the turn with your `message`; the user connects and asks again. "
                "Use it instead of answering from another agent or saying the data is unavailable."
            ),
            category="action",
            version="1.0.0",
            input_schema=RequestAgentSigninInput.model_json_schema(),
            output_schema=RequestAgentSigninOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["agent", "data_source", "signin", "user-interaction"],
            allowed_modes=["chat", "training"],
            examples=[
                {
                    "input": {
                        "agent_ids": ["<id from agents_needing_signin>"],
                        "message": "Sales data comes from the Sales agent, which you haven't connected yet. Connect it below and ask again.",
                    },
                    "description": "The user asked about sales; the Sales agent needs their sign-in.",
                }
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return RequestAgentSigninInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return RequestAgentSigninOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = RequestAgentSigninInput(**(tool_input or {}))
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={"agent_ids": data.agent_ids, "title": data.title})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        mode = runtime_ctx.get("mode") or "chat"
        if not all([db, organization, user]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "Missing required runtime context", "code": "MISSING_CONTEXT"})
            return

        try:
            from app.ai.tools.implementations.agent_focus_common import (
                resolve_candidate_agents,
                signin_required_ids,
                signin_scope_agents,
            )
            from app.ai.context.agent_roster import load_agent_one_liners
            from app.ai.tools.implementations.search_agents import SearchAgentsTool

            wanted = list(dict.fromkeys(str(i) for i in data.agent_ids))
            candidates, _scope, _attached = await resolve_candidate_agents(db, organization, user, report, mode)
            # Plus what a run leaves out before it starts: a folder's agents
            # that need sign-in, and the user's own unconnected connectors.
            known = {str(ds.id) for ds in candidates}
            candidates = list(candidates) + [
                ds for ds in await signin_scope_agents(db, organization, user, report)
                if str(ds.id) not in known
            ]
            in_scope = [ds for ds in candidates if str(ds.id) in set(wanted)]
            needs = await signin_required_ids(db, in_scope, user)
            agents = [ds for ds in in_scope if str(ds.id) in needs]
            rejected = [i for i in wanted if i not in {str(ds.id) for ds in agents}]
        except Exception as e:
            logger.exception("request_agent_signin failed")
            yield ToolErrorEvent(type="tool.error", payload={"error": str(e), "code": "SIGNIN_LOOKUP_FAILED"})
            return

        if not agents:
            msg = (
                "None of these agents needs a sign-in from this user: each is either usable "
                "now or not available in this conversation. Continue with the agents you have."
            )
            out = RequestAgentSigninOutput(success=False, agents=[], rejected_ids=rejected, message=msg)
            yield ToolEndEvent(type="tool.end", payload={
                "output": out.model_dump(),
                "observation": {"summary": msg, "artifacts": []},
            })
            return

        one_liners = await load_agent_one_liners(db, agents, organization, user)
        items = [
            SigninAgentItem(
                id=str(ds.id),
                name=getattr(ds, "name", "") or "",
                description=one_liners.get(str(ds.id)) or None,
                **SearchAgentsTool._icon_hints(ds),
            )
            for ds in agents
        ]
        names = ", ".join(i.name for i in items)
        out = RequestAgentSigninOutput(success=True, agents=items, rejected_ids=rejected, message=data.message)
        yield ToolEndEvent(type="tool.end", payload={
            "output": out.model_dump(),
            "observation": {
                "summary": f"Asked the user to connect: {names}. Waiting for them to sign in and ask again.",
                "artifacts": [],
                # Nothing more can be answered until the user signs in: end the
                # turn with the planner's own words (in the user's language).
                "analysis_complete": True,
                "final_answer": data.message,
            },
        })
