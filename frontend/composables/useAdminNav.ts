import { useCan } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'

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
  feature?: string
}

export interface AdminNavItem {
  name: string
  label: string
  icon: string
  permission: string
  description?: string
  width?: AdminPageWidth
  feature?: string
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
      { key: 'groups', label: 'settings.membersTabs.groups', icon: 'i-heroicons-rectangle-group', description: 'settings.adminPages.groups', width: 'wide', permission: 'manage_groups', feature: 'custom_roles' },
      // Service accounts are a core (non-EE) capability — no feature flag.
      { key: 'service_accounts', label: 'settings.membersTabs.serviceAccounts', icon: 'i-heroicons-command-line', description: 'serviceAccounts.subtitle', width: 'wide', permission: 'manage_service_accounts' },
      { key: 'quotas', label: 'settings.membersTabs.quotas', icon: 'i-heroicons-scale', description: 'settings.adminPages.quotas', width: 'wide', permission: 'manage_settings', feature: 'usage_limits' },
      { key: 'signup', label: 'settings.membersTabs.signup', icon: 'i-heroicons-envelope-open', description: 'signupPolicy.description', width: 'narrow', permission: 'full_admin_access', feature: 'domain_signup' },
    ],
  },
  // cpu-chip is the model icon everywhere else (the composer's model picker).
  { name: 'models', label: 'settings.llm', icon: 'i-heroicons-cpu-chip', permission: 'manage_llm', description: 'settings.adminPages.models', width: 'wide' },
  { name: 'ai_settings', label: 'settings.aiSettings', icon: 'i-heroicons-adjustments-horizontal', permission: 'manage_settings', description: 'settings.aiSettingsPage.subtitle', width: 'narrow' },
  { name: 'pii', label: 'settings.piiTab', icon: 'i-heroicons-eye-slash', permission: 'manage_settings', feature: 'pii_protection', description: 'settings.pii.subtitle', width: 'narrow' },
  { name: 'general', label: 'settings.general', icon: 'i-heroicons-building-office', permission: 'manage_settings', description: 'settings.subtitle', width: 'narrow' },
  { name: 'integrations', label: 'settings.integrations.title', icon: 'i-heroicons-chat-bubble-left-right', permission: 'manage_settings', description: 'settings.integrations.subtitle', width: 'wide' },
  { name: 'audit', label: 'settings.auditLogs', icon: 'i-heroicons-clipboard-document-list', permission: 'view_audit_logs', description: 'settings.audit.subtitle', width: 'wide' },
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

  const allowed = (permission?: string, feature?: string) => {
    if (feature && !hasFeature(feature)) return false
    if (permission && !useCan(permission)) return false
    return true
  }

  const visibleItems = computed(() =>
    ADMIN_NAV_ITEMS
      .filter(item => allowed(item.permission, item.feature))
      .map(item => ({
        ...item,
        children: item.children?.filter(child => allowed(child.permission, child.feature)),
      }))
  )

  // Where the sidebar's Admin entry should land: the first page the user can
  // actually open, so the click never bounces off permissions.global.ts.
  const firstAccessibleItem = computed(() => visibleItems.value[0] || null)

  return { visibleItems, firstAccessibleItem }
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
