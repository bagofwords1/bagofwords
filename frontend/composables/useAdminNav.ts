import { useCan, useCanAny } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'
import { useAppSettings } from '~/composables/useAppSettings'

// The admin area's navigation, in one place. Before this, the same list lived
// in layouts/settings.vue (tabs), layouts/default.vue (the Admin link's
// first-reachable-tab lookup) and pages/settings/members.vue (sub-tabs), and
// the copies had drifted — the sidebar lookup was missing pii and smtp.
//
// Everything that renders or links into /settings reads from here.

// How wide a page's content runs. `narrow` is a reading measure for forms and
// settings (768px); `wide` is for tables and lists (1152px). Fixed widths, not
// percentages, so a page keeps its proportions as the window changes.
export type AdminPageWidth = 'narrow' | 'wide'

export interface AdminNavChild {
  key: string
  label: string
  icon: string
  // i18n key for the one-line description under the page title.
  description: string
  width: AdminPageWidth
  permission?: string
  // With `resourceType`, holding `permission` on any one resource of that type
  // is enough (e.g. a group manager sees the Groups page for their groups).
  resourceType?: string
  feature?: string
}

export interface AdminNavItem {
  name: string
  label: string
  icon: string
  permission: string
  // With `resourceType`, holding `permission` on any one resource of that type
  // is enough (a provider manager sees the LLM page for their providers).
  resourceType?: string
  description?: string
  width?: AdminPageWidth
  feature?: string
  superuserOnly?: boolean
  // Offered only when GET /api/settings reports `<key>.enabled === true`: a
  // feature the deployment turns on in its configuration.
  enabledBySetting?: string
  // Sub-pages rendered by the same route, selected with ?tab=<key>.
  children?: AdminNavChild[]
}

export const ADMIN_NAV_ITEMS: AdminNavItem[] = [
  {
    name: 'members',
    label: 'settings.membersTab',
    icon: 'i-heroicons-user-group',
    permission: 'view_members',
    children: [
      { key: 'members', label: 'settings.membersTabs.members', icon: 'i-heroicons-users', description: 'settings.adminPages.members', width: 'wide' },
      { key: 'roles', label: 'settings.membersTabs.roles', icon: 'i-heroicons-identification', description: 'settings.adminPages.roles', width: 'wide', permission: 'manage_roles', feature: 'custom_roles' },
      // `manage_members` org-wide, or as the manager of at least one group.
      { key: 'groups', label: 'settings.membersTabs.groups', icon: 'i-heroicons-rectangle-group', description: 'settings.adminPages.groups', width: 'wide', permission: 'manage_members', resourceType: 'group', feature: 'custom_roles' },
      // Service accounts are a core (non-EE) capability — no feature flag.
      { key: 'service_accounts', label: 'settings.membersTabs.serviceAccounts', icon: 'i-heroicons-command-line', description: 'serviceAccounts.subtitle', width: 'wide', permission: 'manage_service_accounts' },
      { key: 'quotas', label: 'settings.membersTabs.quotas', icon: 'i-heroicons-scale', description: 'settings.adminPages.quotas', width: 'wide', permission: 'manage_settings', feature: 'usage_limits' },
      { key: 'signup', label: 'settings.membersTabs.signup', icon: 'i-heroicons-envelope-open', description: 'signupPolicy.description', width: 'narrow', permission: 'full_admin_access', feature: 'domain_signup' },
    ],
  },
  // cpu-chip is the model icon everywhere else (the composer's model picker).
  // `manage_llm` org-wide, or as the manager of at least one LLM provider.
  { name: 'models', label: 'settings.llm', icon: 'i-heroicons-cpu-chip', permission: 'manage_llm', resourceType: 'llm_provider', description: 'settings.adminPages.models', width: 'wide' },
  { name: 'ai_settings', label: 'settings.aiSettings', icon: 'i-heroicons-adjustments-horizontal', permission: 'manage_settings', description: 'settings.aiSettingsPage.subtitle', width: 'narrow' },
  // Agent sandboxes: offered only when the deployment turns the feature on
  // (GET /api/settings → agent_sandboxes.enabled). `manage_sandboxes` is its
  // own permission, separate from `manage_settings`.
  {
    name: 'sandbox',
    label: 'settings.sandboxTab',
    icon: 'i-heroicons-cube-transparent',
    permission: 'manage_sandboxes',
    enabledBySetting: 'agent_sandboxes',
    children: [
      { key: 'sandboxes', label: 'settings.sandboxTabs.sandboxes', icon: 'i-heroicons-queue-list', description: 'settings.adminPages.sandboxSandboxes', width: 'wide' },
      { key: 'computes', label: 'settings.sandboxTabs.computes', icon: 'i-heroicons-server-stack', description: 'settings.adminPages.sandboxComputes', width: 'wide' },
      { key: 'environments', label: 'settings.sandboxTabs.environments', icon: 'i-heroicons-cube', description: 'settings.adminPages.sandboxEnvironments', width: 'wide' },
      { key: 'settings', label: 'settings.sandboxTabs.settings', icon: 'i-heroicons-cog-6-tooth', description: 'settings.adminPages.sandboxSettings', width: 'narrow' },
    ],
  },
  { name: 'pii', label: 'settings.piiTab', icon: 'i-heroicons-eye-slash', permission: 'manage_settings', feature: 'pii_protection', description: 'settings.pii.subtitle', width: 'narrow' },
  { name: 'general', label: 'settings.general', icon: 'i-heroicons-building-office', permission: 'manage_settings', description: 'settings.subtitle', width: 'narrow' },
  { name: 'integrations', label: 'settings.integrations.title', icon: 'i-heroicons-chat-bubble-left-right', permission: 'manage_settings', description: 'settings.integrations.subtitle', width: 'wide' },
  { name: 'audit', label: 'settings.auditLogs', icon: 'i-heroicons-clipboard-document-list', permission: 'view_audit_logs', description: 'settings.audit.subtitle', width: 'wide' },
  { name: 'system-logs', label: 'settings.systemLogsTab', icon: 'i-heroicons-command-line', permission: 'full_admin_access', superuserOnly: true, enabledBySetting: 'system_logs', description: 'settings.systemLogs.subtitle', width: 'wide' },
  { name: 'identity-provider', label: 'settings.identityProviderTab', icon: 'i-heroicons-finger-print', permission: 'manage_identity_providers', description: 'settings.adminPages.identityProvider', width: 'narrow' },
  { name: 'smtp', label: 'settings.smtpTab', icon: 'i-heroicons-envelope', permission: 'manage_settings', description: 'settings.adminPages.smtp', width: 'narrow' },
  { name: 'license', label: 'settings.license', icon: 'i-heroicons-key', permission: 'manage_settings', description: 'settings.licensePage.subtitle', width: 'narrow' },
]

// The title, description and width for the page at `path` (+ `?tab=`).
export function resolveAdminPage(path: string, tab?: string) {
  const name = path.replace(/^\/settings\/?/, '').split('/')[0] || ''
  const item = ADMIN_NAV_ITEMS.find(i => i.name === name)
  if (!item) return null
  if (item.children?.length) {
    const child = item.children.find(c => c.key === tab) || item.children[0]
    return { key: `${item.name}:${child.key}`, label: child.label, description: child.description, width: child.width }
  }
  return { key: item.name, label: item.label, description: item.description, width: item.width || 'wide' }
}

export function useAdminNav() {
  const { hasFeature } = useEnterprise()
  const { data: currentUser } = useAuth()
  const { settings: appSettings } = useAppSettings()

  const allowed = (permission?: string, feature?: string, resourceType?: string) => {
    if (feature && !hasFeature(feature)) return false
    if (permission && !(resourceType ? useCanAny(permission, resourceType) : useCan(permission))) return false
    return true
  }

  const visibleItems = computed(() =>
    ADMIN_NAV_ITEMS
      .filter(item => allowed(item.permission, item.feature, item.resourceType))
      .filter(item => !item.superuserOnly || currentUser.value?.is_superuser === true)
      .filter(item => !item.enabledBySetting || appSettings.value?.[item.enabledBySetting]?.enabled === true)
      .map(item => ({
        ...item,
        children: item.children?.filter(child => allowed(child.permission, child.feature, child.resourceType)),
      }))
  )

  // Where the sidebar's Admin entry should land: the first page the user can
  // actually open, so the click never bounces off permissions.global.ts.
  const firstAccessibleItem = computed(() => visibleItems.value[0] || null)

  // Where the Admin entry opens: the first page this person's admin powers
  // unlock, rather than Members for everyone. Members is visible to every
  // member (view_members), so it only makes sense as the landing page for
  // whoever manages people org-wide. Anyone else opens their own page: LLM for
  // an LLM manager, Audit logs for an auditor, AI settings for a settings
  // manager, Groups for a group manager, Service accounts, and so on.
  const adminLanding = computed<string | null>(() => {
    const items = visibleItems.value
    if (!items.length) return null
    if (useCan('manage_members')) return '/settings/members'
    const page = items.find(item => item.name !== 'members' && !item.children?.length)
    if (page) return `/settings/${page.name}`
    // Members sub-pages behind a real admin permission (the Members list
    // itself carries none).
    const sub = items.find(item => item.name === 'members')?.children?.find(child => child.permission)
    if (sub) return `/settings/members?tab=${sub.key}`
    // A page made only of sub-tabs (Sandbox) for someone whose one admin
    // permission is that page's.
    const tabbed = items.find(item => item.name !== 'members')
    if (tabbed) return `/settings/${tabbed.name}`
    return `/settings/${items[0].name}`
  })

  return { visibleItems, firstAccessibleItem, adminLanding }
}

// Paths under /settings that are NOT part of the admin console. The channel
// verify page is opened by ordinary members from a Slack/Teams link; it must
// keep the normal app sidebar and no admin chrome.
const NON_ADMIN_SETTINGS_PREFIXES = ['/settings/integrations/verify']

export function isAdminRoute(path: string): boolean {
  if (path !== '/settings' && !path.startsWith('/settings/')) return false
  return !NON_ADMIN_SETTINGS_PREFIXES.some(p => path === p || path.startsWith(p + '/'))
}

// The last route outside the admin console, so "Back to app" returns the user
// to where they were rather than to home.
export const useAdminReturnPath = () => useState<string>('admin-return-path', () => '/')
