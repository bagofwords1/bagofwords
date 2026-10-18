import type { AgentList } from '~/components/lists/types'

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
