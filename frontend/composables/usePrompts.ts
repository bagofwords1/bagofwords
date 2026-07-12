// API helpers for the /prompts management hub. Thin wrappers over useMyFetch
// (which prepends nothing — callers pass the full /api path — injects auth +
// X-Organization-Id, and on the client returns a { data } ref).
import type { PromptParameter, PromptParamValue } from '~/composables/usePromptFill'

export type PromptScope = 'global' | 'agent' | 'private'
export type PromptMode = 'chat' | 'deep' | 'training'

export interface Prompt {
  id: string
  title?: string | null
  text: string
  mode: string
  model_id?: string | null
  mentions?: any[] | null
  parameters?: PromptParameter[] | null
  scope: PromptScope
  data_source_ids: string[]
  user_id?: string | null
  created_at?: string | null
  can_manage: boolean
}

export interface PromptListMeta {
  [key: string]: any
}

export interface PromptListFilters {
  search?: string
  scope?: string
  data_source_id?: string
  created_by?: string
}

export interface PromptWritePayload {
  title?: string | null
  text: string
  mode?: string
  model_id?: string | null
  mentions?: any[] | null
  parameters?: PromptParameter[] | null
  scope?: PromptScope
  data_source_ids?: string[]
}

export function usePrompts() {
  async function listPrompts(filters: PromptListFilters = {}): Promise<{ prompts: Prompt[]; meta: PromptListMeta }> {
    const query: Record<string, string> = {}
    if (filters.search) query.search = filters.search
    if (filters.scope) query.scope = filters.scope
    if (filters.data_source_id) query.data_source_id = filters.data_source_id
    if (filters.created_by) query.created_by = filters.created_by

    const { data } = await useMyFetch<{ prompts: Prompt[]; meta: PromptListMeta }>('/api/prompts', {
      method: 'GET',
      query,
    })
    return (data.value as any) || { prompts: [], meta: {} }
  }

  async function getPrompt(id: string): Promise<Prompt | null> {
    const { data } = await useMyFetch<Prompt>(`/api/prompts/${id}`, { method: 'GET' })
    return (data.value as any) || null
  }

  async function createPrompt(body: PromptWritePayload): Promise<Prompt | null> {
    const { data } = await useMyFetch<Prompt>('/api/prompts', { method: 'POST', body })
    return (data.value as any) || null
  }

  async function updatePrompt(id: string, body: Partial<PromptWritePayload>): Promise<Prompt | null> {
    const { data } = await useMyFetch<Prompt>(`/api/prompts/${id}`, { method: 'PUT', body })
    return (data.value as any) || null
  }

  async function deletePrompt(id: string): Promise<boolean> {
    const { data, error } = await useMyFetch(`/api/prompts/${id}`, { method: 'DELETE' })
    if ((error as any)?.value) return false
    return !!(data.value as any)?.ok
  }

  async function runPrompt(id: string, parameters?: Record<string, PromptParamValue>): Promise<{ report_id: string } | null> {
    const { data } = await useMyFetch<{ report_id: string }>(`/api/prompts/${id}/run`, {
      method: 'POST',
      body: { parameters: parameters || null },
    })
    return (data.value as any) || null
  }

  async function runPromptFor(
    id: string,
    payload: {
      principal_type: 'users' | 'group'
      user_ids?: string[]
      group_id?: string
      parameters?: Record<string, PromptParamValue>
      delivery_channels?: string[]
    },
  ): Promise<{ ran: number; skipped: number; skipped_user_ids: string[] } | null> {
    const { data } = await useMyFetch<{ ran: number; skipped: number; skipped_user_ids: string[] }>(
      `/api/prompts/${id}/run-for`,
      { method: 'POST', body: payload },
    )
    return (data.value as any) || null
  }

  async function getRunForTargets(id: string): Promise<{
    users: { id: string; name?: string; email?: string }[]
    groups: { id: string; name: string; member_count: number; eligible_count: number }[]
  } | null> {
    const { data } = await useMyFetch<any>(`/api/prompts/${id}/run-for/targets`)
    return (data.value as any) || null
  }

  return { listPrompts, getPrompt, createPrompt, updatePrompt, deletePrompt, runPrompt, runPromptFor, getRunForTargets }
}
