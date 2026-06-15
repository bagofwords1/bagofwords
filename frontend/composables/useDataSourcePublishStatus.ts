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
      return 'Turned off — hidden everywhere'
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
