import { useCan } from '~/composables/usePermissions'
import { useEnterprise } from '~/ee/composables/useEnterprise'

// The admin area's navigation, in one place. Before this, the same list lived
// in layouts/settings.vue (tabs), layouts/default.vue (the Admin link's
// first-reachable-tab lookup) and pages/settings/members.vue (sub-tabs), and
// the copies had drifted — the sidebar lookup was missing pii and smtp.
//
// Everything that renders or links into /settings reads from here.

export interface AdminNavChild {
  key: string
  label: string
  icon: string
  permission?: string
  feature?: string
}

export interface AdminNavItem {
  name: string
  label: string
  icon: string
  permission: string
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
      { key: 'members', label: 'settings.membersTabs.members', icon: 'i-heroicons-users' },
      { key: 'roles', label: 'settings.membersTabs.roles', icon: 'i-heroicons-identification', permission: 'manage_roles', feature: 'custom_roles' },
      { key: 'groups', label: 'settings.membersTabs.groups', icon: 'i-heroicons-rectangle-group', permission: 'manage_groups', feature: 'custom_roles' },
      // Service accounts are a core (non-EE) capability — no feature flag.
      { key: 'service_accounts', label: 'settings.membersTabs.serviceAccounts', icon: 'i-heroicons-cpu-chip', permission: 'manage_service_accounts' },
      { key: 'quotas', label: 'settings.membersTabs.quotas', icon: 'i-heroicons-scale', permission: 'manage_settings', feature: 'usage_limits' },
      { key: 'signup', label: 'settings.membersTabs.signup', icon: 'i-heroicons-envelope-open', permission: 'full_admin_access', feature: 'domain_signup' },
    ],
  },
  { name: 'models', label: 'settings.llm', icon: 'i-heroicons-sparkles', permission: 'manage_llm' },
  { name: 'ai_settings', label: 'settings.aiSettings', icon: 'i-heroicons-adjustments-horizontal', permission: 'manage_settings' },
  { name: 'pii', label: 'settings.piiTab', icon: 'i-heroicons-eye-slash', permission: 'manage_settings', feature: 'pii_protection' },
  { name: 'general', label: 'settings.general', icon: 'i-heroicons-building-office', permission: 'manage_settings' },
  { name: 'integrations', label: 'settings.integrations.title', icon: 'i-heroicons-chat-bubble-left-right', permission: 'manage_settings' },
  { name: 'audit', label: 'settings.auditLogs', icon: 'i-heroicons-clipboard-document-list', permission: 'view_audit_logs' },
  { name: 'identity-provider', label: 'settings.identityProviderTab', icon: 'i-heroicons-finger-print', permission: 'manage_identity_providers' },
  { name: 'smtp', label: 'settings.smtpTab', icon: 'i-heroicons-envelope', permission: 'manage_settings' },
  { name: 'license', label: 'settings.license', icon: 'i-heroicons-key', permission: 'manage_settings' },
]

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
