/**
 * What the prompt box's agent chip is standing for.
 *
 * Agent scope is a MODE, not a set:
 *
 *  - **Auto** — no agent pinned. The backend resolves it per run against the
 *    running user ("every agent I can access"), so it follows access changes
 *    and picks up agents created later. Encoded as an empty selection, which is
 *    exactly what the backend already reads as Auto (see
 *    `agent_focus_common.report_selection_is_auto`).
 *  - **Manual** — one or more agents pinned. A hard scope, named on the chip.
 *
 * The two must never be inferred from each other. Selecting every agent by hand
 * is manual, not Auto: it freezes today's roster. And in a one-agent org,
 * picking that agent is still manual — "the only agent right now" and "whatever
 * I can access" are different promises, even when they currently coincide.
 *
 * Project defaults are an ordinary manual selection: the backend copies the
 * project's agents onto the report at creation, so the report genuinely holds
 * them and they render like any other pin.
 */
export interface AgentAutoInput {
    selectedIds: string[]
}

export interface AgentAutoState {
    /** No agent pinned — the backend resolves scope at run time. */
    isAuto: boolean
}

export function resolveAgentAuto(input: AgentAutoInput): AgentAutoState {
    return { isAuto: (input.selectedIds || []).length === 0 }
}

/**
 * The starting selection for a new report opened inside a project: the
 * project's default agents, in the project's order, limited to the agents this
 * user can see. A default never widens access — an id the user can't see is
 * dropped rather than pinned. No visible default means an empty selection,
 * i.e. Auto.
 */
export function projectDefaultSelection<T extends { id: string | number }>(
    defaultIds: Array<string | number>,
    available: T[],
): T[] {
    const visible = new Map((available || []).map(a => [String(a.id), a]))
    const out: T[] = []
    const seen = new Set<string>()
    for (const raw of defaultIds || []) {
        const id = String(raw)
        const agent = visible.get(id)
        if (agent && !seen.has(id)) { seen.add(id); out.push(agent) }
    }
    return out
}
