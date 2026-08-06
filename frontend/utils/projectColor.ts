/**
 * Project accent colors.
 *
 * The accent is how a report shows which project it belongs to: the sidebar
 * draws a thin rule in this color at the leading edge of the report's row.
 * A project with no color leaves that strip invisible, so dropping a report
 * onto a folder would look like nothing happened — hence every project gets a
 * color at creation time instead of only when someone opens project settings
 * and picks one.
 *
 * Same list backs the swatch row in project settings, so an auto-assigned
 * color and a hand-picked one are always drawn from the same palette.
 */
export const PROJECT_COLORS = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2', '#64748b']

/**
 * Pick the accent for a new project: the first swatch nobody is using yet, so
 * a workspace's first seven folders are all distinguishable. Past that the
 * palette repeats, keyed off the project count so consecutive creations don't
 * collide on the same color.
 */
export function nextProjectColor(existing: { color?: string | null }[] = []): string {
    const used = new Set(existing.map((p) => p?.color).filter(Boolean) as string[])
    const unused = PROJECT_COLORS.find((c) => !used.has(c))
    return unused || PROJECT_COLORS[existing.length % PROJECT_COLORS.length]
}
