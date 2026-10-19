import type { AgentList } from '~/components/lists/types'
import { useMyFetch } from '~/composables/useMyFetch'

type T = (key: string, named?: Record<string, unknown>, plural?: number) => string

/** "Shared with …" for a list linked to more than one agent; '' otherwise. */
export function sharedWithLabel(list: AgentList, t: T): string {
  if (!list.shared) return ''
  const others = (list.agents || []).filter(a => a.id !== list.data_source_id).map(a => a.name)
  const hidden = list.hidden_agent_count || 0
  if (!others.length) return t('lists.sharedWithHidden', { n: hidden }, hidden)
  const names = others.join(', ')
  return hidden ? t('lists.sharedWithMore', { names, n: hidden }, hidden) : t('lists.sharedWith', { names })
}

/**
 * The agent to reach a list through now. A chat card remembers the agent a
 * row was saved through; once the list leaves that agent (or the agent is
 * deleted) its agent-scoped address 404s, while the list itself still answers
 * by id through an agent the viewer can see. Null when it doesn't.
 */
export async function resolveListAgent(listId: string): Promise<string | null> {
  const { data } = await useMyFetch<AgentList>(`/api/agent_lists/${listId}`, { method: 'GET' })
  return data.value?.data_source_id || null
}
