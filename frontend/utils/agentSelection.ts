/**
 * What the prompt box's agent chip is standing for.
 *
 * Two different things both read as "the user didn't pick agents by hand":
 *
 *  - **Workspace auto** — every visible agent is in play. No chip can usefully
 *    name that set, so it renders as the "Auto" bolt, and an external picker
 *    (the blank-report agent chips) must show nothing selected rather than
 *    lighting up the whole roster.
 *  - **Project defaults** — the report was created inside a project and carries
 *    exactly that project's default agents. That is a short, concrete set the
 *    report really holds, so it gets named and highlighted like any other
 *    selection. Every new report in a project opens in this state, which is why
 *    collapsing it into "Auto" hid the project's agent from its own report.
 *
 * Project defaults are intersected with the visible agents first: a default the
 * user can't see was never attached to their report, so it can't count toward
 * the match.
 */
export interface AgentAutoInput {
    selectedIds: string[]
    visibleIds: string[]
    projectDefaultIds?: string[]
}

export interface AgentAutoState {
    /** Selection carries no hand-picked choice — project defaults or everything. */
    isAuto: boolean
    /** Auto standing for every visible agent, i.e. the nameless kind. */
    isWorkspaceAuto: boolean
    /** Project defaults that are actually visible to this user. */
    effectiveDefaultIds: string[]
}

export function resolveAgentAuto(input: AgentAutoInput): AgentAutoState {
    const visible = new Set((input.visibleIds || []).map(String))
    const selected = new Set((input.selectedIds || []).map(String))
    const effectiveDefaultIds = (input.projectDefaultIds || [])
        .map(String)
        .filter((id) => visible.has(id))

    if (effectiveDefaultIds.length > 0) {
        const isAuto = selected.size === effectiveDefaultIds.length
            && effectiveDefaultIds.every((id) => selected.has(id))
        return { isAuto, isWorkspaceAuto: false, effectiveDefaultIds }
    }

    const isAuto = visible.size > 0 && [...visible].every((id) => selected.has(id))
    return { isAuto, isWorkspaceAuto: isAuto, effectiveDefaultIds }
}
