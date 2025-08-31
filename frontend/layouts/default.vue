<template>

  <aside id="separator-sidebar"
    class="fixed top-0 left-0 z-40 w-14 h-screen transition-transform -translate-x-full sm:translate-x-0
    border-r-[3px] border-gray-100"
    aria-label="Sidebar">
    <div class="h-full px-3 py-4 bg-gray-50 flex flex-col justify-between">
      <ul class="font-normal text-sm">
        <li>
            <button @click="router.push('/')" class="flex items-center text-center p-1 text-gray-700 group">
              <img :src="workspaceIconUrl || '/assets/logo-128.png'" alt="Bag of words" class="w-10 " />
            </button>
        </li>
        <li class="">
          <div  class="flex mt-10 items-center p-1 text-gray-700 rounded-lg group hover:text-blue-500 ">
            <UTooltip text="New report" :popper="{ placement: 'right' }">
              <button
                name="create-report"
                @click="createNewReport"
                class="flex items-center text-center p-1 pb-0 rounded-lg group text-blue-500 hover:bg-gray-200">
                <span class="font-medium text-lg ">
                  <UIcon name="heroicons-plus-circle" />
                </span>
              </button>
            </UTooltip>
          </div>
        </li>

        <li class="">
          <a href="/reports" class="flex items-center p-2 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Reports" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg ">
                <UIcon name="heroicons-chart-pie" />
              </span>
            </UTooltip>
          </a>
        </li>
        <li class="hidden">
          <NuxtLink to="/memory" class="flex mt-1 items-center p-2 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Memory" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg">
                <UIcon name="heroicons-cube" />
              </span>
            </UTooltip>
          </NuxtLink>
        </li>
        <li>
          <NuxtLink to="/library" class="flex mt-1 items-center p-2 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Library" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg">
                <UIcon name="heroicons-document-duplicate" />
              </span>
            </UTooltip>
          </NuxtLink>
        </li>

        <li class="" v-if="isAdmin">
          <a href="/console" class="flex items-center p-2 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Console" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg ">
                <UIcon name="heroicons-cpu-chip" />
              </span>
            </UTooltip>
          </a>
        </li>
      </ul>
      <ul class="font-normal text-sm">
        <li>
          <a href="/integrations" class="flex items-center text-center p-1 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Integrations" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg ">
                <UIcon name="heroicons-circle-stack" />
              </span>
            </UTooltip>
          </a>
        </li>
        <li>
          <a href="#" class="flex items-center text-center p-1 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="" :popper="{ placement: 'right' }">
              <template #text>
                Logged in as {{ currentUserName }}
              </template>
              <span class="font-medium text-lg">
                <UIcon name="heroicons-user" />
              </span>
            </UTooltip>
          </a>
        </li>
        <li>
          <a href="/settings" class="flex items-center text-center p-1 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="" :popper="{ placement: 'right' }">
              <template #text>
                Organization: {{ organization?.name }}
              </template>
              <span class="font-medium text-lg">
                <UIcon name="heroicons-cog-6-tooth" />
              </span>
            </UTooltip>
          </a>
        </li>
        <li>
          <a href="https://docs.bagofwords.com" target="_blank" class="flex items-center text-center p-1 text-gray-700 rounded-lg group hover:text-blue-500">
            <UTooltip text="Documentation" :popper="{ placement: 'right' }">
              <span class="font-medium text-lg">
                <UIcon name="heroicons-book-open" />
              </span>
            </UTooltip>
          </a>
        </li>
        <li>
          <UTooltip text="Sign out" :popper="{ placement: 'right' }">
            <button @click="signOff()"
              class="flex items-center text-center p-1 text-gray-700 rounded-lg group hover:text-blue-500">
              <UIcon name="heroicons-arrow-left" />

            </button>
          </UTooltip>
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

  <div class="sm:ml-14 h-[100vh]">
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
  const { isExcel } = useExcel()
  const router = useRouter()
  const selectedDataSources = ref<any[]>([])
  const { $intercom } = useNuxtApp()
  const { version, environment, app_url, intercom } = useRuntimeConfig().public
  
  const isAdmin = computed(() => {
    const orgs = (currentUser.value as any)?.organizations || []
    return orgs?.[0]?.role === 'admin'
  })

  const currentUserName = computed(() => (currentUser.value as any)?.name || 'User')
 
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

    selectedDataSources.value = (((response as any).data?.value) as any[]) || [];
}


const createNewReport = async () => {
  await getDataSourceOptions()
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({title: 'untitled report',
         files: [],
         data_sources: selectedDataSources.value.map((ds: any) => ds.id)})
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

