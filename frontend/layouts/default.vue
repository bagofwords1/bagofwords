<template>
  <div>
    <!-- Fixed global onboarding banner shown above everything -->
    <div v-if="showGlobalOnboardingBanner" class="fixed top-0 start-0 end-0 z-[1000]">
      <div
        @click="router.push(showGlobalOnboardingBannerLink)"
        class="text-center cursor-pointer text-white text-sm bg-blue-500/95 hover:bg-blue-600/90 py-2 flex items-center justify-center shadow-md"
      >
        <UIcon name="i-heroicons-rocket-launch" class="h-5 me-2" />
        <span>{{ showGlobalOnboardingBannerText }}</span>
      </div>
    </div>

    <!-- License expiry countdown banner (shown in the last 30 days, and after expiry) -->
    <div v-if="showLicenseBanner" class="fixed top-0 start-0 end-0 z-[1000]">
      <div
        :class="[
          'text-center text-sm py-2 px-4 flex items-center justify-center gap-2 shadow-md',
          licenseExpired
            ? 'bg-red-600/95 text-white'
            : 'bg-amber-500/95 text-white',
          canModifySettings ? 'cursor-pointer hover:opacity-95' : ''
        ]"
        @click="canModifySettings ? router.push('/settings/license') : null"
      >
        <UIcon :name="licenseExpired ? 'i-heroicons-exclamation-circle' : 'i-heroicons-exclamation-triangle'" class="h-5 shrink-0" />
        <span>{{ licenseBannerText }}</span>
        <span v-if="canModifySettings" class="underline underline-offset-2 font-medium ms-1">
          {{ $t('settings.licensePage.banner.viewLicense') }}
        </span>
      </div>
    </div>
  <aside id="separator-sidebar"
    :class="[
      'fixed start-0 z-40 bg-gray-50 transition-all duration-300 -translate-x-full rtl:translate-x-full sm:translate-x-0 sm:rtl:translate-x-0 border-e border-gray-200/80',
      isCollapsed ? 'w-14' : 'w-48',
      showTopBanner ? 'top-10 bottom-0' : 'top-0 bottom-0'
    ]"
    aria-label="Sidebar">
    <button @click="toggleSidebar" :class="[
            'flex items-center gap-3 rounded-lg transition-all duration-200 bg-gray-50',
            isCollapsed
              ? 'px-2 py-2 w-full text-center justify-center -mb-4 text-gray-700 hover:text-blue-500'
              : 'px-1 py-1 mt-3 ms-auto -mb-5 text-gray-400 hover:text-gray-600'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t('nav.expandSidebar')" :popper="{ placement: tooltipPlacement }">
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-right" class="rtl-flip" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-left" class="rtl-flip" />
              </span>
              <span v-if="showText" class="text-xs opacity-75"></span>
            </template>
          </button>
    <div class="h-full px-3 py-4 bg-gray-50 flex flex-col justify-between">

      <ul class="font-normal text-[13px] !ps-0">
        <li>
            <button @click="router.push('/')" class="flex items-center justify-center p-1 text-gray-700 group">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" :class="isCollapsed ? 'w-8 object-contain' : 'max-h-8 max-w-[120px] object-contain'" />
            </button>
        </li>

        <!-- Agent Selector - Context for all navigation below -->
        <li class="mt-4 mb-2">
          <AgentSelector :collapsed="isCollapsed" :show-text="showText" />
        </li>

        <li>
             <button
               name="create-report"
               @click="createNewReport"
               :disabled="creatingReport"
               :class="[
                 'flex items-center px-3 py-1.5 w-full rounded-md text-blue-500 hover:bg-gray-100 disabled:opacity-50 disabled:cursor-not-allowed',
                 isCollapsed ? 'justify-center' : 'gap-2.5'
               ]">
              <UTooltip v-if="isCollapsed" :text="creatingReport ? $t('common.loading') : $t('nav.newReport')" :popper="{ placement: tooltipPlacement }">
                <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
              </UTooltip>
              <template v-else>
                <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
                <span v-if="showText" class="font-medium">{{ creatingReport ? $t('common.loading') : $t('nav.newReport') }}</span>
              </template>
            </button>
        </li>

        <template v-for="item in mainNavItems" :key="item.href">
        <li v-if="item.section && !isCollapsed && (!item.adminOnly || isAdmin)" class="pt-3 pb-1 px-3">
          <span class="text-[11px] font-medium text-gray-400 uppercase tracking-wider">{{ $t(item.section) }}</span>
        </li>
        <li v-if="(!item.permission || useCan(item.permission)) && (!item.adminOnly || isAdmin)" :class="{ hidden: item.hidden }">
          <NuxtLink :to="item.href" :class="[
            'flex items-center px-3 py-1.5 w-full rounded-md',
            isRouteActive(item.activePath || item.href) ? 'text-gray-900 bg-gray-200/70 font-medium' : 'text-gray-500 hover:text-gray-900 hover:bg-gray-100',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </NuxtLink>
        </li>
        </template>
      </ul>
      <ul class="font-normal text-[13px] !ps-0">
        <li v-for="item in bottomNavItems" :key="item.href">
          <a v-if="item.external" :href="item.href" target="_blank" rel="noopener noreferrer" :class="[
            'flex items-center px-3 py-1.5 w-full rounded-md',
            'text-gray-500 hover:text-gray-900 hover:bg-gray-100',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <component v-if="item.component" :is="item.component" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <component v-if="item.component" :is="item.component" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </a>
          <NuxtLink v-else :to="item.href" :class="[
            'flex items-center px-3 py-1.5 w-full rounded-md',
            isRouteActive(item.activePath || item.href) ? 'text-gray-900 bg-gray-200/70 font-medium' : 'text-gray-500 hover:text-gray-900 hover:bg-gray-100',
            isCollapsed ? 'justify-center' : 'gap-2.5'
          ]">
            <UTooltip v-if="isCollapsed" :text="$t(item.label)" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <component v-if="item.component" :is="item.component" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <component v-if="item.component" :is="item.component" />
                <UIcon v-else-if="item.icon" :name="item.icon" />
              </span>
              <span v-if="showText">{{ $t(item.label) }}</span>
            </template>
          </NuxtLink>
        </li>
        <li v-if="isMcpEnabled && useCan('manage_settings')">
          <button
            @click="showMcpModal = true"
            :class="[
              'flex items-center px-3 py-1.5 w-full rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100',
              isCollapsed ? 'justify-center' : 'gap-2.5'
            ]"
          >
            <UTooltip v-if="isCollapsed" :text="$t('mcp.tooltipCollapsed')" :popper="{ placement: tooltipPlacement }">
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <McpIcon :class="isCollapsed ? 'w-5 h-5' : 'w-[18px] h-[18px]'" />
              </span>
            </UTooltip>
            <template v-else>
              <span :class="['flex items-center justify-center', isCollapsed ? 'w-5 h-5 text-[16px]' : 'w-[18px] h-[18px]']">
                <McpIcon :class="isCollapsed ? 'w-5 h-5' : 'w-[18px] h-[18px]'" />
              </span>
              <span v-if="showText">{{ $t('nav.mcpServer') }}</span>
            </template>
          </button>
        </li>
        <li>
          <UDropdown :items="userDropdownItems" :popper="{ placement: 'top-start' }" class="block w-full">
             <button :class="[
               'flex items-center px-3 py-1.5 w-full rounded-md text-gray-500 hover:text-gray-900 hover:bg-gray-100',
               isCollapsed ? 'justify-center' : 'gap-2.5'
             ]">
              <UTooltip v-if="isCollapsed" :text="$t('nav.loggedInAs', { name: currentUserName })" :popper="{ placement: tooltipPlacement }">
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-[10px] font-bold rounded-full">
                  {{ userInitial }}
                </div>
              </UTooltip>
              <template v-else>
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-[10px] font-bold rounded-full">
                  {{ userInitial }}
                </div>
                <span v-if="showText" class="truncate">{{ currentUserName }}</span>
              </template>
            </button>
          </UDropdown>
        </li>
        <li v-if="version && !isCollapsed">
          <UTooltip :text="$t('nav.version')" :popper="{ placement: tooltipPlacement }">
            <div class="text-[10px] text-gray-400 px-3 cursor-pointer hover:text-gray-900">
              {{ version }}
            </div>
          </UTooltip>
        </li>
      </ul>
    </div>

  </aside>

  <div :class="['min-h-screen transition-all duration-300', isCollapsed ? 'sm:ms-14' : 'sm:ms-48', showTopBanner ? 'pt-10' : 'pt-0']">
    <UNotifications />

    <slot />
  </div>

  <McpModal v-if="showMcpModal" v-model="showMcpModal" />

  <!-- Global ⌘K / Ctrl+K command palette -->
  <CommandPalette />
  </div>
</template>

<script setup lang="ts">
  import Spinner from '~/components/Spinner.vue'
  import McpIcon from '~/components/icons/McpIcon.vue'
  import LibraryIcon from '~/components/icons/LibraryIcon.vue'
  import ActivityIcon from '~/components/icons/ActivityIcon.vue'
  import AgentIcon from '~/components/icons/AgentIcon.vue'
  import McpModal from '~/components/McpModal.vue'
  import AgentSelector from '~/components/AgentSelector.vue'
  import { useCan } from '~/composables/usePermissions'

  const { isMcpEnabled } = useOrgSettings()
  const showMcpModal = ref(false)

  const route = useRoute()
  const isRouteActive = (path: string) => {
    if (path === '/') return route.path === '/'
    return route.path === path || route.path.startsWith(path + '/')
  }
  watch(() => route.fullPath, () => {
    showMcpModal.value = false
  })

  interface NavItem {
    href: string
    label: string
    icon?: string
    component?: any
    hidden?: boolean
    adminOnly?: boolean
    permission?: string
    section?: string
    external?: boolean
    activePath?: string
  }
  // Settings tabs and the permission each requires — must mirror the tab list
  // in layouts/settings.vue. The sidebar Settings link uses this to (a) hide
  // itself when no tab is reachable and (b) deep-link to the first reachable
  // tab, so clicking it never lands on a page the user gets redirected out of.
  const settingsTabPermissions: { name: string; permission: string }[] = [
    { name: 'members', permission: 'view_members' },
    { name: 'models', permission: 'manage_llm' },
    { name: 'ai_settings', permission: 'manage_settings' },
    { name: 'general', permission: 'manage_settings' },
    { name: 'integrations', permission: 'manage_settings' },
    { name: 'audit', permission: 'view_audit_logs' },
    { name: 'identity-provider', permission: 'manage_identity_providers' },
    { name: 'license', permission: 'manage_settings' },
  ]
  const firstAccessibleSettingsTab = computed(() =>
    settingsTabPermissions.find(tab => useCan(tab.permission)) || null
  )

  const mainNavItems: NavItem[] = [
    { href: '/reports', icon: 'heroicons-chat-bubble-left-right', label: 'nav.reports' },
    { href: '/dashboards', icon: 'heroicons-chart-bar-square', label: 'nav.dashboards' },
    { href: '/scheduled-tasks', icon: 'heroicons-clock', label: 'nav.scheduled' },
    { href: '/files', icon: 'heroicons-document-duplicate', label: 'nav.files', hidden: true },
    { href: '/instructions', icon: 'heroicons-cube', label: 'nav.instructions' },
    { href: '/queries', component: LibraryIcon, label: 'nav.queries' },
    { href: '/monitoring', component: ActivityIcon, label: 'nav.monitoring', adminOnly: true, section: 'nav.manage' },
    { href: '/evals', icon: 'heroicons-check-circle', label: 'nav.evals', permission: 'manage_evals' },
  ]

  const bottomNavItems = computed<NavItem[]>(() => {
    const items: NavItem[] = [
      { href: '/agents', component: AgentIcon, label: 'nav.dataAgents' },
    ]
    // The Settings entry was always shown but hard-linked to /settings/members,
    // which requires `view_members`. A user on a custom role without that perm
    // would click it and get silently bounced to '/' by permissions.global.ts —
    // i.e. "the Settings button does nothing". Only surface it when the user can
    // actually reach a settings tab, and point it at the first one they can open.
    const tab = firstAccessibleSettingsTab.value
    if (tab) {
      items.push({ href: `/settings/${tab.name}`, activePath: '/settings', icon: 'heroicons-cog-6-tooth', label: 'nav.settings' })
    }
    items.push({ href: 'https://docs.bagofwords.com', icon: 'heroicons-book-open', label: 'nav.documentation', external: true })
    return items
  })
  
  // Agent management - use selectedAgentObjects for new report creation
  const { initAgent, selectedAgentObjects, agents, hasAgents } = useAgent()

  
  const workspaceIconUrl = computed<string | null>(() => {
    const orgId = organization.value?.id
    const orgs = (currentUser.value as any)?.organizations || []
    const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
    return org?.icon_url || null
  })
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization, setOrganization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()
  const canModifySettings = computed(() => useCan('manage_settings'))
  const showGlobalOnboardingBanner = computed(() => {
    if (!canModifySettings.value) return false
    const ob = onboarding.value as any

    if (!ob) return false
    //if (ob.dismissed) return false
    const steps = ob.steps || {}
    const llmDone = steps.llm_configured?.status === 'done'
    const dataDone = steps.data_source_created?.status === 'done'
    return !(llmDone && dataDone)
  })

  const showGlobalOnboardingBannerText = computed(() => {
    const ob = onboarding.value as any
    if (!ob) return 'Continue onboarding'
    return ob.current_step === 'llm_configured' ? 'Configure your LLM' : 'Connect your first data source'
  })

  const showGlobalOnboardingBannerLink = computed(() => {
    const ob = onboarding.value as any
    if (!ob) return '/onboarding'
    return ob.current_step === 'llm_configured' ? '/onboarding/llm' : '/onboarding/data'
  })

  // License expiry countdown banner. Shown to everyone (an expired license affects the
  // whole org), but only admins get the clickable link to the license settings page.
  const { isExpired: licenseExpired, isExpiringSoon, daysUntilExpiry } = useEnterprise()
  const showLicenseBanner = computed<boolean>(() => {
    // Never stack on top of the onboarding banner — they share the same fixed slot,
    // and a brand-new org won't have a near-expiry enterprise license anyway.
    if (showGlobalOnboardingBanner.value) return false
    return licenseExpired.value || isExpiringSoon.value
  })
  // Either fixed top banner pushes the sidebar + content down by the same amount.
  const showTopBanner = computed<boolean>(() => showGlobalOnboardingBanner.value || showLicenseBanner.value)
  const licenseBannerText = computed<string>(() => {
    if (licenseExpired.value) return t('settings.licensePage.banner.expired')
    return t('settings.licensePage.banner.expiring', { days: daysUntilExpiry.value ?? 0 })
  })

  const { isExcel } = useExcel()
  const { isMobile } = useMobile()
  const router = useRouter()
  const { $intercom } = useNuxtApp()

  onMounted(async () => {
    try {
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        // Fetch onboarding and agents in parallel for faster load
        await Promise.all([
          fetchOnboarding({ in_onboarding: false }),
          initAgent()
        ])
      }
    } catch {}

    // Hydrate locale from org config. Runs once per full page load —
    // the user's personal choice (stored under `bow.locale`) always
    // wins; we only apply the org override when they haven't picked
    // anything. Executes here rather than in the i18n plugin because
    // useMyFetch needs the session + org state that are only ready
    // after mount.
    try {
      const stored = typeof localStorage !== 'undefined' ? localStorage.getItem('bow.locale') : null
      if (!stored) {
        const resp = await useMyFetch('/api/organization/locale')
        const body = resp.data?.value as any
        const effective = body?.effective_locale
        const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => void) | undefined
        if (effective && typeof setLocale === 'function') setLocale(effective)
      }
    } catch {
      // non-fatal; user can still pick manually via the settings picker
    }
  })
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  // Sidebar collapse state (shared via composable)
  const { isCollapsed, showText, toggle: toggleSidebar } = useSidebar()
  const creatingReport = ref(false)

  // Collapsed sidebar tooltips need to pop INTO the viewport, not out of it.
  // In LTR the sidebar is on the left so tooltips go right; in RTL the
  // sidebar is on the right so tooltips go left.
  const { locale: i18nLocale } = useI18n({ useScope: 'global' })
  const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
  const isRtl = computed<boolean>(() => RTL_LOCALES.has(i18nLocale.value))
  const tooltipPlacement = computed<'left' | 'right'>(() =>
    isRtl.value ? 'left' : 'right'
  )
  // Intercom launcher should sit on the opposite side of the sidebar so it
  // doesn't collide with it. LTR: sidebar left → launcher right (default).
  // RTL: sidebar right → launcher left.
  const intercomAlignment = computed<'left' | 'right'>(() =>
    isRtl.value ? 'left' : 'right'
  )
  
  const currentUserName = computed<string>(() => {
    const user = currentUser.value as any
    return user?.name || user?.email || 'User'
  })

  const userInitial = computed<string>(() => {
    const name = currentUserName.value
    return name.charAt(0).toUpperCase()
  })

  const { t } = useI18n()
  const userOrganizations = computed<any[]>(() => {
    return ((currentUser.value as any)?.organizations || []) as any[]
  })

  const userDropdownItems = computed(() => {
    const groups: any[] = []
    const orgs = userOrganizations.value
    if (orgs.length > 1) {
      groups.push(
        orgs.map((org: any) => ({
          label: org.name,
          icon: org.id === organization.value?.id ? 'heroicons-check' : undefined,
          disabled: org.id === organization.value?.id,
          click: () => setOrganization(org.id),
        }))
      )
    }
    groups.push([{
      label: t('auth.logout'),
      icon: 'heroicons-arrow-left',
      click: signOff
    }])
    return groups
  })

  const isAdmin = computed<boolean>(() => useCan('full_admin_access'))
 
  if (environment === 'production' && intercom) {
    const hideLauncher = computed<boolean>(() => isExcel.value || isMobile.value)
    $intercom.boot({
      hide_default_launcher: hideLauncher.value,
      alignment: intercomAlignment.value
    })
    watch([currentUser, organization], ([user, org]) => {
      if (user && org) {
        $intercom.update({
          user_id: (user as any).id,
          name: (user as any)?.name,
          email: (user as any)?.email,
          version: version,
          environment: environment,
          app_url: app_url,
          hide_default_launcher: hideLauncher.value,
          alignment: intercomAlignment.value,
          company: {
            company_id: org.id,
            name: org.name
          }
        })
      }
    }, { immediate: true })
    watch(intercomAlignment, (alignment) => {
      $intercom.update({ alignment })
    })
    watch(hideLauncher, (hide) => {
      $intercom.update({ hide_default_launcher: hide })
    })
  }

const createNewReport = async () => {
  if (creatingReport.value) return
  creatingReport.value = true
  
  try {
    // Use selected agents from AgentSelector, or all agents if none selected
    const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)
    
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({
          title: 'untitled report',
          files: [],
          data_sources: dataSourceIds
        })
    });

    if ((response as any).error?.value) {
        throw new Error('Report creation failed');
    }

    const data = ((response as any).data?.value) as any;
    await router.push({
        path: `/reports/${data.id}`
    })
  } finally {
    creatingReport.value = false
  }
}

  async function signOff() {
    await signOut({ 
      callbackUrl: '/' 
    })
    window.location.href = '/'
  }

  </script>
