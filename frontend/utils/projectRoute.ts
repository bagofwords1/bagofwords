/**
 * Which route counts as "inside a project".
 *
 * This decides whether a new report inherits the project's default agents:
 * POST /reports copies them onto the report only when the body carries a
 * `project_id` (and no explicit agents), so a route read wrong here silently
 * produces a report with the workspace-wide agent fallback instead of the
 * agents the project rail promises.
 *
 * Only the project detail page qualifies. `/projects` (the index) and every
 * other screen are workspace-level.
 */
const PROJECT_ROUTE = /^\/projects\/([^/?#]+)/

export function projectIdFromPath(path?: string | null): string | null {
    if (!path) return null
    const match = PROJECT_ROUTE.exec(path)
    if (!match) return null
    try {
        return decodeURIComponent(match[1]) || null
    } catch {
        // A malformed escape is still a real route segment — keep it verbatim
        // rather than dropping the project context.
        return match[1] || null
    }
}
