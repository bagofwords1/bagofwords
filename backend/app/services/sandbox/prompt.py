"""What the agent is told about the agent sandbox.

Kept in one place: the short prompt section added when the tools are in the
catalog, and the per-run note about network access that is appended to the
tool descriptions so the agent does not attempt downloads that cannot work.
"""
from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence

NETWORK_EXPLANATIONS = {
    "none": "no internet access: nothing can be downloaded and packages cannot be installed; use what is preinstalled",
    "registries": "only the domains the admin allows, by default the package registries (pip, npm and go get work); every other site is refused",
    "allowlist": "only the domains its environment lists; everything else is refused",
    "full": "the public internet over HTTP and HTTPS only (no SSH, database or other raw connections; private networks stay blocked)",
}

SANDBOX_TOOLS = ("create_sandbox", "run_in_sandbox", "sandbox_status", "delete_sandbox")

PROMPT_SECTION = (
    "<agent_sandbox_guidance>This report has a persistent sandbox: an isolated container where you can run "
    "Python, Node, Go or shell code with `run_in_sandbox`. Use it for work that needs real code execution on "
    "files rather than a query against a data source: processing or converting an uploaded file, a script that "
    "needs a package, producing a file (a chart image, a spreadsheet, a document) the user asked for. Do NOT use "
    "it to query the connected data sources: it holds none of their credentials (use create_data / inspect_data "
    "for that). Nothing from the report is in the sandbox unless you pass it with `input_file_ids`; those files "
    "appear under /workspace/inputs. Write results under /workspace/outputs: every new or changed file there is "
    "saved to the report after the run and returned with a file_id. Only /workspace/outputs is durable. The "
    "sandbox keeps its other files and installed packages between runs, but treat them as a cache: a sandbox "
    "can be deleted after a period of disuse or recreated after a failure, and then only what was saved to the "
    "report is put back. One run at a time. Check the network access stated in the tool description before "
    "trying to download anything.</agent_sandbox_guidance>"
)


def describe_network(policy: Optional[str]) -> str:
    return NETWORK_EXPLANATIONS.get(policy or "none", NETWORK_EXPLANATIONS["none"])


def prompt_section(tool_catalog: Optional[Iterable[Any]]) -> str:
    """The prompt section, or ``""`` when the sandbox tools are not in the catalog."""
    names = set()
    for tool in tool_catalog or []:
        names.add(getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None))
    return PROMPT_SECTION if "run_in_sandbox" in names else ""


def run_note(*, existing: Optional[dict], environments: Sequence[Any], default_name: Optional[str]) -> str:
    """One paragraph appended to the tool descriptions for this run: what the
    report's sandbox (or the environment a new one would use) can reach."""
    lines: List[str] = []
    if existing:
        lines.append(
            f"This report's sandbox exists ({existing.get('state')}), environment '{existing.get('environment')}'. "
            f"Network: {describe_network(existing.get('network_policy'))}."
        )
    else:
        described = "; ".join(
            f"'{e.name}'{' (default)' if e.name == default_name else ''}: {describe_network(e.network_policy)}"
            for e in environments
        )
        lines.append("This report has no sandbox yet; the first run creates one. Environments you may use: " + described + ".")
    return "\n\nFor this report:\n" + "\n".join(lines)
