/**
 * Manager-set publishing lifecycle for an agent (data source).
 *
 * Distinct from connection health (see useConnectionStatus.ts). This is the
 * intentional, human-set state that decides who can see/use the agent:
 *   - published — visible to everyone with access
 *   - draft     — visible only to users who can `manage` the agent (builders)
 *   - disabled  — off; hidden everywhere, excluded from AI context
 *
 * Keeps badge/label/option rendering in one place so the agent page, the
 * agents list, and the data source selector stay consistent.
 */

export type PublishStatus = 'published' | 'draft' | 'disabled'

export const PUBLISH_STATUSES: PublishStatus[] = ['published', 'draft', 'disabled']

export function publishStatusLabel(status?: string | null): string {
  switch (status) {
    case 'published':
      return 'Published'
    case 'draft':
      return 'Draft'
    case 'disabled':
      return 'Disabled'
    default:
      return 'Published'
  }
}

export function publishStatusBadgeClass(status?: string | null): string {
  switch (status) {
    case 'published':
      return 'bg-green-50 text-green-700 border-green-200'
    case 'draft':
      return 'bg-amber-50 text-amber-700 border-amber-200'
    case 'disabled':
      return 'bg-gray-100 text-gray-600 border-gray-200'
    default:
      return 'bg-green-50 text-green-700 border-green-200'
  }
}

export function publishStatusDotClass(status?: string | null): string {
  switch (status) {
    case 'published':
      return 'bg-green-500'
    case 'draft':
      return 'bg-amber-500'
    case 'disabled':
      return 'bg-gray-400'
    default:
      return 'bg-green-500'
  }
}

export function publishStatusDescription(status?: string | null): string {
  switch (status) {
    case 'published':
      return 'Visible to everyone with access'
    case 'draft':
      return 'Visible only to people who can manage this agent'
    case 'disabled':
      return 'Turned off and hidden everywhere'
    default:
      return 'Visible to everyone with access'
  }
}

/** Options for a select/dropdown control, in lifecycle order. */
export function publishStatusOptions(): Array<{ value: PublishStatus; label: string; description: string }> {
  return PUBLISH_STATUSES.map((value) => ({
    value,
    label: publishStatusLabel(value),
    description: publishStatusDescription(value),
  }))
}

// ── Unified agent lifecycle stage ────────────────────────────────────────────
// The agent's status is two underlying fields — publish_status (manual
// visibility) and reliability_status (ok|training|development). We surface them
// as a single lifecycle stage: Development → Training → Production (+ Disabled).
export type AgentStage = 'production' | 'training' | 'development' | 'disabled'

export function deriveStage(publishStatus?: string, reliabilityStatus?: string): AgentStage {
  if (publishStatus === 'disabled') return 'disabled'
  if (publishStatus === 'draft' || reliabilityStatus === 'development') return 'development'
  if (reliabilityStatus === 'training') return 'training'
  return 'production'
}

/** Fields to PUT when a stage is chosen (manual override of both axes). */
export function stageWrite(stage: AgentStage): Record<string, string> {
  switch (stage) {
    case 'production': return { publish_status: 'published', reliability_status: 'ok' }
    case 'training': return { publish_status: 'published', reliability_status: 'training' }
    case 'development': return { publish_status: 'draft', reliability_status: 'development' }
    case 'disabled': return { publish_status: 'disabled' }  // leave reliability as-is
  }
}

export interface StageMeta { value: AgentStage; label: string; description: string; dot: string; badge: string }
export const STAGE_OPTIONS: StageMeta[] = [
  { value: 'production', label: 'Production', description: 'Anyone with access can use it.', dot: 'bg-green-500', badge: 'border-green-200 bg-green-50 text-green-700' },
  { value: 'training', label: 'Training', description: 'Live for everyone, while you keep improving it.', dot: 'bg-blue-500', badge: 'border-blue-200 bg-blue-50 text-blue-700' },
  { value: 'development', label: 'Development', description: 'Only people who can manage this agent can see it.', dot: 'bg-amber-500', badge: 'border-amber-200 bg-amber-50 text-amber-700' },
  { value: 'disabled', label: 'Disabled', description: 'Turned off and hidden everywhere, including from the AI.', dot: 'bg-gray-400', badge: 'border-gray-200 bg-gray-100 text-gray-600' },
]
export function stageMeta(stage: AgentStage): StageMeta {
  return STAGE_OPTIONS.find((o) => o.value === stage) || STAGE_OPTIONS[0]
}
