// /composables/usePrompts.ts
//
// API wrapper for the Prompt Catalog + Subscriptions feature.
// All calls go through useMyFetch, which injects the Authorization token and
// the X-Organization-Id header (see composables/useMyFetch.ts), matching the
// auth/org pattern used everywhere else in the app.

export type PromptChannel = 'teams' | 'slack' | 'ai_mailbox' | 'smtp'
export type PromptRunMode = 'append' | 'new_report'
export type PromptMode = 'chat' | 'deep' | 'training'
export type PromptScope = 'private' | 'agent'
export type PromptStatus = 'draft' | 'published'
export type PromptSort = 'recent' | 'top'
export type PrincipalType = 'user' | 'group' | 'org'

export interface PromptResponse {
  id: string
  title: string
  text: string
  mode: PromptMode
  model_id?: string | null
  mentions?: any[]
  scope: PromptScope
  is_starter: boolean
  status: PromptStatus
  default_cron?: string | null
  default_channel?: PromptChannel | null
  category?: string | null
  tags?: string[]
  data_source_ids: string[]
  user_id?: string
  created_at?: string
  subscriber_count: number
  can_assign: boolean
  can_manage: boolean
}

export interface PromptListMeta {
  total: number
}

export interface SubscribePayload {
  cron_schedule: string
  channel: PromptChannel
  run_mode: PromptRunMode
}

export interface AssignPayload {
  principal_type: PrincipalType
  principal_id?: string
  cron_schedule: string
  channel: PromptChannel
  run_mode: PromptRunMode
}

export interface AssignResult {
  created: number
  skipped: number
  scheduled_prompt_ids: string[]
}

export interface PromptUpsertPayload {
  title?: string
  text?: string
  mode?: PromptMode
  model_id?: string | null
  mentions?: any[]
  scope?: PromptScope
  is_starter?: boolean
  status?: PromptStatus
  default_cron?: string | null
  default_channel?: PromptChannel | null
  category?: string | null
  tags?: string[]
  data_source_ids?: string[]
}

export const PROMPT_CHANNELS: { value: PromptChannel; label: string }[] = [
  { value: 'teams', label: 'Microsoft Teams' },
  { value: 'slack', label: 'Slack' },
  { value: 'ai_mailbox', label: 'AI Mailbox' },
  { value: 'smtp', label: 'Email (SMTP)' },
]

export const PROMPT_RUN_MODES: { value: PromptRunMode; label: string }[] = [
  { value: 'append', label: 'Append to one report' },
  { value: 'new_report', label: 'New report each run' },
]

export function usePrompts() {
  // List prompts. sort=recent|top, optional category + starters_only filter.
  const listPrompts = async (params: {
    sort?: PromptSort
    category?: string
    starters_only?: boolean
  } = {}) => {
    const query: Record<string, any> = {}
    if (params.sort) query.sort = params.sort
    if (params.category) query.category = params.category
    if (typeof params.starters_only === 'boolean') query.starters_only = params.starters_only

    const res = await useMyFetch('/api/prompts', { method: 'GET', query })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to load prompts')
    }
    const data = res.data.value as { prompts: PromptResponse[]; meta: PromptListMeta }
    return data
  }

  const getPrompt = async (id: string): Promise<PromptResponse> => {
    const res = await useMyFetch(`/api/prompts/${id}`, { method: 'GET' })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to load prompt')
    }
    return res.data.value as PromptResponse
  }

  const createPrompt = async (body: PromptUpsertPayload): Promise<PromptResponse> => {
    const res = await useMyFetch('/api/prompts', { method: 'POST', body })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to create prompt')
    }
    return res.data.value as PromptResponse
  }

  const updatePrompt = async (id: string, body: PromptUpsertPayload): Promise<PromptResponse> => {
    const res = await useMyFetch(`/api/prompts/${id}`, { method: 'PUT', body })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to update prompt')
    }
    return res.data.value as PromptResponse
  }

  const deletePrompt = async (id: string): Promise<void> => {
    const res = await useMyFetch(`/api/prompts/${id}`, { method: 'DELETE' })
    if (res.status.value !== 'success') {
      throw res.error.value || new Error('Failed to delete prompt')
    }
  }

  const runPrompt = async (id: string): Promise<{ report_id: string; completion_id: string }> => {
    const res = await useMyFetch(`/api/prompts/${id}/run`, { method: 'POST' })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to run prompt')
    }
    return res.data.value as { report_id: string; completion_id: string }
  }

  const subscribePrompt = async (id: string, body: SubscribePayload) => {
    const res = await useMyFetch(`/api/prompts/${id}/subscribe`, { method: 'POST', body })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to subscribe')
    }
    return res.data.value as any
  }

  const assignPrompt = async (id: string, body: AssignPayload): Promise<AssignResult> => {
    const res = await useMyFetch(`/api/prompts/${id}/assign`, { method: 'POST', body })
    if (res.status.value !== 'success' || !res.data.value) {
      throw res.error.value || new Error('Failed to assign')
    }
    return res.data.value as AssignResult
  }

  return {
    listPrompts,
    getPrompt,
    createPrompt,
    updatePrompt,
    deletePrompt,
    runPrompt,
    subscribePrompt,
    assignPrompt,
  }
}
