/**
 * Split a picker list into a "Recent" head and the rest.
 *
 * The prompt box's model and agent dropdowns lead with what this user actually
 * uses. `last_used_at` comes per-user from the list endpoints
 * (`/llm/models`, `/data_sources/active`); items never used have none.
 *
 * Only the head moves. Re-sorting the whole list by usage would shuffle rows
 * under the cursor between visits, so the rest keeps the caller's stable order
 * (providers A–Z for models, names A–Z for agents). Short lists get no head:
 * when everything fits at a glance a second section is noise.
 */
export const RECENT_LIMIT = 3
export const RECENT_MIN_LIST = 6

type Usable = { id?: unknown; last_used_at?: string | null }

function usedAt(item: Usable): number {
  const t = item?.last_used_at ? Date.parse(item.last_used_at) : NaN
  return Number.isFinite(t) ? t : 0
}

export function splitRecent<T extends Usable>(
  items: readonly T[],
  opts: { limit?: number; minList?: number } = {},
): { recent: T[]; rest: T[] } {
  const list = [...(items || [])]
  const limit = opts.limit ?? RECENT_LIMIT
  const minList = opts.minList ?? RECENT_MIN_LIST
  if (list.length < minList || limit <= 0) return { recent: [], rest: list }

  const recent = list
    .filter((it) => usedAt(it) > 0)
    .sort((a, b) => usedAt(b) - usedAt(a))
    .slice(0, limit)
  const picked = new Set(recent)
  return { recent, rest: list.filter((it) => !picked.has(it)) }
}
