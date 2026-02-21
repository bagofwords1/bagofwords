<template>
    <!-- Fixed global onboarding banner shown above everything -->
    <div v-if="showGlobalOnboardingBanner" class="fixed top-0 left-0 right-0 z-[1000]">
      <div
        @click="router.push(showGlobalOnboardingBannerLink)"
        class="mx-auto max-w-screen-2xl text-center cursor-pointer text-white text-sm bg-blue-500/95 hover:bg-blue-600/90 py-2 flex items-center justify-center shadow-md"
      >
        <UIcon name="i-heroicons-rocket-launch" class="h-5 mr-2" />
        <span>{{ showGlobalOnboardingBannerText }}</span>
      </div>
    </div>
  <aside id="separator-sidebar"
    :class="[
      'fixed left-0 z-40 bg-gray-50 transition-all duration-300 -translate-x-full sm:translate-x-0 border-r-[3px] border-gray-100',
      isCollapsed ? 'w-14' : 'w-48',
      showGlobalOnboardingBanner ? 'top-10 bottom-0' : 'top-0 bottom-0'
    ]"
    aria-label="Sidebar">
    <button @click="toggleSidebar" :class="[
            'flex items-center gap-3 rounded-lg transition-all duration-200 bg-gray-50',
            isCollapsed 
              ? 'px-2 py-2 w-full text-center justify-center -mb-4 text-gray-700 hover:text-blue-500' 
              : 'px-1 py-1 mt-3 ml-auto -mb-5 text-gray-400 hover:text-gray-600'
          ]">
            <UTooltip v-if="isCollapsed" text="Expand sidebar" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-right" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-4 h-4 text-sm">
                <UIcon name="heroicons-chevron-left" />
              </span>
              <span v-if="showText" class="text-xs opacity-75"></span>
            </template>
          </button>
    <div class="h-full px-3 py-4 bg-gray-50 flex flex-col justify-between">

      <ul class="font-normal text-sm">
        <li>
            <button @click="router.push('/')" class="flex items-center justify-center p-1 text-gray-700 group">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" :class="isCollapsed ? 'w-8 object-contain' : 'max-h-10 max-w-[120px] object-contain'" />
            </button>
        </li>

        <!-- Domain Selector - Context for all navigation below (hidden if no domains) -->
        <li v-if="hasDomains" class="mt-6 mb-4">
          <DomainSelector :collapsed="isCollapsed" :show-text="showText" />
        </li>

        <li class="">
          <div class="flex mt-2">
             <button
               name="create-report"
               @click="createNewReport"
               :disabled="creatingReport"
               :class="[
                 'flex items-center px-2 py-2 w-full rounded-lg text-blue-500 hover:bg-gray-200 disabled:opacity-50 disabled:cursor-not-allowed',
                 isCollapsed ? 'justify-center' : 'gap-3'
               ]">
              <UTooltip v-if="isCollapsed" :text="creatingReport ? 'Creating...' : 'New report'" :popper="{ placement: 'right' }">
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
              </UTooltip>
              <template v-else>
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <Spinner v-if="creatingReport" class="animate-spin" />
                  <UIcon v-else name="heroicons-plus-circle" />
                </span>
                <span v-if="showText" class="text-sm font-medium">{{ creatingReport ? 'Creating...' : 'New report' }}</span>
              </template>
            </button>
          </div>
        </li>

        <template v-for="item in mainNavItems" :key="item.href">
        <li v-if="!item.adminOnly || isAdmin" :class="{ hidden: item.hidden }">
          <a :href="item.href" :class="[
            'flex items-center px-2 py-2 w-full rounded-lg',
            isRouteActive(item.href) ? 'text-black bg-gray-200' : 'text-gray-600 hover:text-black hover:bg-gray-200',
            isCollapsed ? 'justify-center' : 'gap-3'
          ]">
            <UTooltip v-if="isCollapsed" :text="item.label" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon v-if="item.icon" :name="item.icon" />
                <component v-else-if="item.component" :is="item.component" />
              </span>
              <span v-if="showText" class="text-sm">{{ item.label }}</span>
            </template>
          </a>
        </li>
        </template>
      </ul>
      <ul class="font-normal text-sm">
        <li v-for="item in bottomNavItems" :key="item.href">
          <a :href="item.href" :target="item.external ? '_blank' : undefined" :class="[
            'flex items-center px-2 py-2 w-full rounded-lg',
            item.external ? 'text-gray-600 hover:text-black hover:bg-gray-200' : (isRouteActive(item.href) ? 'text-black bg-gray-200' : 'text-gray-600 hover:text-black hover:bg-gray-200'),
            isCollapsed ? 'justify-center' : 'gap-3'
          ]">
            <UTooltip v-if="isCollapsed" :text="item.label" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon :name="item.icon" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon :name="item.icon" />
              </span>
              <span v-if="showText" class="text-sm">{{ item.label }}</span>
            </template>
          </a>
        </li>
        <li v-if="isMcpEnabled">
          <button
            @click="showMcpModal = true"
            :class="[
              'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
              isCollapsed ? 'justify-center' : 'gap-3'
            ]"
          >
            <UTooltip v-if="isCollapsed" text="MCP - Connect Claude/Cursor to your data" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <McpIcon class="w-5 h-5" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <McpIcon class="w-5 h-5" />
              </span>
              <span v-if="showText" class="text-sm">MCP Server</span>
            </template>
          </button>
        </li>
        <li>
          <UDropdown :items="userDropdownItems" :popper="{ placement: 'top-start' }">
             <button :class="[
               'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
               isCollapsed ? 'justify-center' : 'gap-3'
             ]">
              <UTooltip v-if="isCollapsed" :text="`Logged in as ${currentUserName}`" :popper="{ placement: 'right' }">
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-xs font-bold rounded-full">
                  {{ userInitial }}
                </div>
              </UTooltip>
              <template v-else>
                <div class="flex items-center justify-center w-5 h-5 bg-blue-500 text-white text-xs font-bold rounded-full">
                  {{ userInitial }}
                </div>
                <span v-if="showText" class="text-sm truncate">{{ currentUserName }}</span>
              </template>
            </button>
          </UDropdown>
        </li>
        <li v-if="version">
          <UTooltip text="Version" :popper="{ placement: 'right' }">
            <div class="text-[9px] text-gray-500 p-0 cursor-pointer hover:text-gray-900">
              {{ version }}
            </div>
          </UTooltip>
        </li>
      </ul>
    </div>

  </aside>

  <div :class="['min-h-screen transition-all duration-300', isCollapsed ? 'sm:ml-14' : 'sm:ml-48', showGlobalOnboardingBanner ? 'pt-10' : 'pt-0']">
    <UNotifications />

    <slot />
  </div>

  <McpModal v-model="showMcpModal" />
</template>

<script setup lang="ts">
  import Spinner from '~/components/Spinner.vue'
  import McpIcon from '~/components/icons/McpIcon.vue'
  import LibraryIcon from '~/components/icons/LibraryIcon.vue'
  import ActivityIcon from '~/components/icons/ActivityIcon.vue'
  import McpModal from '~/components/McpModal.vue'
  import DomainSelector from '~/components/DomainSelector.vue'

  const { isMcpEnabled } = useOrgSettings()
  const showMcpModal = ref(false)

  const route = useRoute()
  const isRouteActive = (path: string) => {
    if (path === '/') return route.path === '/'
    return route.path === path || route.path.startsWith(path + '/')
  }

  const mainNavItems = [
    { href: '/reports', icon: 'heroicons-chart-pie', label: 'Reports' },
    { href: '/files', icon: 'heroicons-document-duplicate', label: 'Files', hidden: true },
    { href: '/instructions', icon: 'heroicons-cube', label: 'Instructions' },
    { href: '/queries', component: LibraryIcon, label: 'Queries' },
    { href: '/monitoring', component: ActivityIcon, label: 'Monitoring', adminOnly: true },
    { href: '/evals', icon: 'heroicons-check-circle', label: 'Evals', adminOnly: true },
  ]

  const bottomNavItems = [
    { href: '/data', icon: 'heroicons-circle-stack', label: 'Data Agents' },
    { href: '/settings', icon: 'heroicons-cog-6-tooth', label: 'Settings' },
    { href: 'https://docs.bagofwords.com', icon: 'heroicons-book-open', label: 'Documentation', external: true },
  ]
  
  // Domain management - use selectedDomainObjects for new report creation
  const { initDomain, selectedDomainObjects, domains, hasDomains } = useDomain()

  
  const workspaceIconUrl = computed<string | null>(() => {
    const orgId = organization.value?.id
    const orgs = (currentUser.value as any)?.organizations || []
    const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
    return org?.icon_url || null
  })
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()
  const { useCan } = await import('~/composables/usePermissions')
  const canModifySettings = computed(() => useCan('modify_settings'))
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

  const { isExcel } = useExcel()
  const router = useRouter()
  const { $intercom } = useNuxtApp()

  onMounted(async () => {
    try {
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        // Fetch onboarding and domains in parallel for faster load
        await Promise.all([
          fetchOnboarding({ in_onboarding: false }),
          initDomain()
        ])
      }
    } catch {}
  })
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  // Sidebar collapse state (shared via composable)
  const { isCollapsed, showText, toggle: toggleSidebar } = useSidebar()
  const creatingReport = ref(false)
  
  const currentUserName = computed<string>(() => {
    const user = currentUser.value as any
    return user?.name || user?.email || 'User'
  })

  const userInitial = computed<string>(() => {
    const name = currentUserName.value
    return name.charAt(0).toUpperCase()
  })

  const userDropdownItems = computed(() => [
    [{
      label: 'Sign out',
      icon: 'heroicons-arrow-left',
      click: signOff
    }]
  ])

  const isAdmin = computed<boolean>(() => {
    const user = currentUser.value as any
    const orgs = user?.organizations || []
    const role = orgs[0]?.role
    return role === 'admin'
  })
 
  if (environment === 'production' && intercom) {
    $intercom.boot()
    watch([currentUser, organization], ([user, org]) => {
      if (user && org) {
        $intercom.update({
          user_id: (user as any).id,
          name: (user as any)?.name,
          email: (user as any)?.email,
          version: version,
          environment: environment,
          app_url: app_url,
          company: {
            company_id: org.id,
            name: org.name
          }
        })
      }
    }, { immediate: true })
  }

const createNewReport = async () => {
  if (creatingReport.value) return
  creatingReport.value = true
  
  try {
    // Use selected domains from DomainSelector, or all domains if none selected
    const dataSourceIds = selectedDomainObjects.value.map((ds: any) => ds.id)
    
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

