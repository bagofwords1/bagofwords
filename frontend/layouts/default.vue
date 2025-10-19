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
            <button @click="router.push('/')" class="flex items-center text-center p-1 text-gray-700 group">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" class="w-10 " />
            </button>
        </li>
        <li>

        </li>
        <li class="">
          <div class="flex mt-10">
             <button
               name="create-report"
               @click="createNewReport"
               :class="[
                 'flex items-center px-2 py-2 w-full rounded-lg text-blue-500 hover:bg-gray-200',
                 isCollapsed ? 'justify-center' : 'gap-3'
               ]">
              <UTooltip v-if="isCollapsed" text="New report" :popper="{ placement: 'right' }">
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <UIcon name="heroicons-plus-circle" />
                </span>
              </UTooltip>
              <template v-else>
                <span class="flex items-center justify-center w-5 h-5 text-lg">
                  <UIcon name="heroicons-plus-circle" />
                </span>
                <span v-if="showText" class="text-sm font-medium">New report</span>
              </template>
            </button>
          </div>
        </li>

        <li class="">
           <a href="/reports" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Reports" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-chart-pie" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-chart-pie" />
              </span>
              <span v-if="showText" class="text-sm">Reports</span>
            </template>
          </a>
        </li>

        <li class="hidden">
           <NuxtLink to="/files" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Files" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-document-duplicate" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-document-duplicate" />
              </span>
              <span v-if="showText" class="text-sm">Files</span>
            </template>
          </NuxtLink>
        </li>

        <li class="">
           <a href="/instructions" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Instructions" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cube" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cube" />
              </span>
              <span v-if="showText" class="text-sm">Instructions</span>
            </template>
          </a>
        </li>
        <li>
          <a href="/catalog" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Catalog" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-library-icon lucide-library"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Catalog</span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-library-icon lucide-library"><path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Catalog</span>
            </template>
          </a>
        </li>
        <li class="" v-if="isAdmin">
           <a href="/monitoring" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Monitoring" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-activity-icon lucide-activity"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/></svg>
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-activity-icon lucide-activity"><path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/></svg>
              </span>
              <span v-if="showText" class="text-sm">Monitoring</span>
            </template>
          </a>
        </li>
      </ul>
      <ul class="font-normal text-sm">
        <li>
           <a href="/integrations" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Data Sources" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-circle-stack" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-circle-stack" />
              </span>
              <span v-if="showText" class="text-sm">Data Sources</span>
            </template>
          </a>
        </li>
        <li>
           <a href="/settings" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Settings" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cog-6-tooth" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-cog-6-tooth" />
              </span>
              <span v-if="showText" class="text-sm">Settings</span>
            </template>
          </a>
        </li>
        <li>
           <a href="https://docs.bagofwords.com" target="_blank" :class="[
             'flex items-center px-2 py-2 w-full rounded-lg text-gray-600 hover:text-black hover:bg-gray-200',
             isCollapsed ? 'justify-center' : 'gap-3'
           ]">
            <UTooltip v-if="isCollapsed" text="Documentation" :popper="{ placement: 'right' }">
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-book-open" />
              </span>
            </UTooltip>
            <template v-else>
              <span class="flex items-center justify-center w-5 h-5 text-lg">
                <UIcon name="heroicons-book-open" />
              </span>
              <span v-if="showText" class="text-sm">Documentation</span>
            </template>
          </a>
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
</template>

<script setup lang="ts">
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

  onMounted(async () => {
    try {
      const route = useRoute()
      const inOnboarding = route.path.startsWith('/onboarding')
      if (!inOnboarding) {
        await fetchOnboarding({ in_onboarding: false })
      }
    } catch {}
  })
  const { isExcel } = useExcel()
  const router = useRouter()
  const selectedDataSources = ref<Array<{ id: string | number }>>([])
  const { $intercom } = useNuxtApp()
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  // Sidebar collapse state
  const isCollapsed = ref(false)
  const showText = ref(true) // Controls text visibility during transitions
  
  const toggleSidebar = () => {
    if (!isCollapsed.value) {
      // Collapsing: hide text immediately
      showText.value = false
      isCollapsed.value = true
    } else {
      // Expanding: show sidebar first, then text after transition
      isCollapsed.value = false
      setTimeout(() => {
        showText.value = true
      }, 300) // Match the transition duration
    }
  }
  
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

  const getDataSourceOptions = async () => {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    });

    if ((response as any).error?.value) {
        throw new Error('Could not fetch data sources');
    }

    const list = (((response as any).data?.value) as Array<{ id: string | number }> | undefined);
    selectedDataSources.value = list || [];
}


const createNewReport = async () => {
  await getDataSourceOptions()
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({title: 'untitled report',
         files: [],
         data_sources: selectedDataSources.value.map((ds) => ds.id)})
    });

    if ((response as any).error?.value) {
        throw new Error('Report creation failed');
    }

    const data = ((response as any).data?.value) as any;
    router.push({
        path: `/reports/${data.id}`
    })
}

  async function signOff() {
    await signOut({ 
      callbackUrl: '/' 
    })
    window.location.href = '/'
  }

  </script>

