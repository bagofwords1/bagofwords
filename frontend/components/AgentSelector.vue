<template>
  <div class="agent-selector">
    <!-- Loading / empty placeholder — reserves layout space while agents load -->
    <div
      v-if="loading || (!loading && agents.length === 0)"
      :class="[
        'flex items-center w-full rounded-lg',
        'bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-sm',
        collapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
      ]"
    >
      <UTooltip v-if="collapsed" :text="loading ? $t('common.loading') : $t('nav.noAgents')" :popper="{ placement: 'right' }">
        <Spinner v-if="loading" class="w-4 h-4 text-gray-300 animate-spin" />
        <AgentIcon class="w-4 h-4 text-gray-300 dark:text-gray-600" />
      </UTooltip>
      <template v-else>
        <span v-if="showText" class="flex-1 text-start min-w-0">
          <span v-if="showLabel" class="block text-[8px] uppercase tracking-wide text-gray-400 font-semibold leading-none">{{ $t('nav.context') }}</span>
          <span :class="['flex items-center gap-1.5', showLabel ? 'mt-0.5' : '']">
            <Spinner v-if="loading" class="w-3 h-3 text-gray-300 animate-spin flex-shrink-0" />
            <span class="text-xs font-medium text-gray-400 truncate">
              {{ loading ? $t('common.loading') : $t('nav.noAgents') }}
            </span>
          </span>
        </span>
      </template>
    </div>

    <UPopover
      v-else
      :mode="nav ? 'hover' : 'click'"
      :popper="{ placement: nav ? 'right-start' : 'bottom-start', offsetDistance: 4, strategy: 'fixed' }"
      :ui="{
        width: 'max-w-none',
        container: 'overflow-visible',
        inner: 'overflow-visible'
      }"
    >
      <!-- Nav mode: the trigger is a link to the Agents explorer; the dropdown opens on hover. -->
      <component
        :is="nav ? 'a' : 'button'"
        :href="nav ? '/agents' : undefined"
        @click="nav ? onNavClick($event) : undefined"
        :class="nav ? [
          'flex items-center w-full rounded-lg border transition-all duration-200',
          isRouteActive('/agents') ? 'border-gray-300 bg-white shadow-sm text-gray-900 font-semibold' : 'border-gray-200 bg-white shadow-sm text-gray-700 hover:border-gray-300 hover:shadow',
          collapsed ? 'justify-center p-2' : 'gap-2.5 px-3 py-2'
        ] : [
          'flex items-center w-full rounded-lg transition-all duration-200',
          'bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800',
          'border border-gray-200 dark:border-gray-700 shadow-sm hover:shadow hover:border-gray-300 dark:hover:border-gray-600',
          collapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
        ]"
      >
        <!-- Nav mode trigger: reflects a single selected agent, else stacked icons + "Agents" -->
        <template v-if="nav">
          <UTooltip v-if="collapsed" :text="navLabel" :popper="{ placement: 'right' }">
            <span class="relative flex items-center justify-center w-5 h-5">
              <DataSourceIcon v-if="navSelected" :type="navSelected.connections?.[0]?.type" :connector-key="navSelected.connections?.[0]?.connector_key" :icon="navSelected.icon" class="w-[18px] h-[18px] rounded" />
              <DataSourceIcon v-else-if="navIconAgents.length" :type="navIconAgents[0].type" :connector-key="navIconAgents[0].connector_key" :icon="navIconAgents[0].icon" class="w-[18px] h-[18px] rounded" />
              <UIcon v-else name="heroicons-cube" class="w-[18px] h-[18px]" />
              <span v-if="!navSelected && agents.length > 1" class="absolute -top-1.5 -right-1.5 min-w-[14px] h-3.5 px-1 rounded-full bg-gray-900 text-white text-[8px] font-semibold leading-none flex items-center justify-center">{{ agents.length > 9 ? '9+' : agents.length }}</span>
            </span>
          </UTooltip>
          <template v-else>
            <span class="flex items-center justify-center">
              <DataSourceIcon v-if="navSelected" :type="navSelected.connections?.[0]?.type" :connector-key="navSelected.connections?.[0]?.connector_key" :icon="navSelected.icon" class="w-[18px] h-[18px] rounded" />
              <span v-else-if="navIconAgents.length" class="flex -space-x-1.5 items-center">
                <DataSourceIcon v-for="(it, i) in navIconAgents" :key="i" :type="it.type" :connector-key="it.connector_key" :icon="it.icon" class="w-[18px] h-[18px] ring-2 ring-white rounded-full bg-white" />
              </span>
              <UIcon v-else name="heroicons-cube" class="w-[18px] h-[18px]" />
            </span>
            <span v-if="showText" class="font-medium truncate">{{ navLabel }}</span>
          </template>
        </template>
        <template v-else>
        <UTooltip v-if="collapsed" :text="currentAgentName" :popper="{ placement: 'right' }">
          <span class="flex items-center justify-center w-5 h-5">
            <Spinner v-if="loading" class="w-4 h-4 text-gray-400 animate-spin" />
            <UIcon v-else name="heroicons-chevron-down" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
          </span>
        </UTooltip>
        <template v-else>
          <span class="flex-shrink-0">
            <DataSourceIcon v-if="singleSelectedConnection" :type="singleSelectedConnection" class="h-3.5 w-3.5" />
            <AgentIcon v-else class="w-3.5 h-3.5 text-gray-400" />
          </span>
          <span v-if="showText" class="flex-1 text-start min-w-0">
            <span v-if="showLabel" class="block text-[8px] uppercase tracking-wide text-gray-400 font-semibold leading-none">{{ $t('nav.context') }}</span>
            <span :class="['flex items-center gap-1.5', showLabel ? 'mt-0.5' : '']">
              <Spinner v-if="loading" class="w-3 h-3 text-gray-400 animate-spin flex-shrink-0" />
              <span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">{{ currentAgentName }}</span>
            </span>
          </span>
          <UIcon v-if="showText" name="heroicons-chevron-up-down" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </template>
        </template>
      </component>

      <template #panel>
        <div class="overflow-visible">
          <!-- Agent list -->
          <div class="w-max min-w-[14rem] max-w-[24rem] bg-white dark:bg-gray-900 rounded-xl shadow-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
            <div class="p-1.5">
              <div v-if="loading" class="flex items-center justify-center py-6">
                <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
              </div>

              <template v-else>
                <!-- All Agents -->
                <button
                  @click="toggleAgent(null)"
                  :class="[
                    'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-start transition-colors',
                    isAllAgents ? 'bg-indigo-50 dark:bg-indigo-950' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                  ]"
                >
                  <AgentIcon class="w-4 h-4 text-gray-400 flex-shrink-0" />
                  <span :class="['text-xs font-medium flex-1', isAllAgents ? 'text-indigo-700' : 'text-gray-700 dark:text-gray-300']">{{ $t('nav.allAgents') }}</span>
                  <UIcon v-if="isAllAgents" name="heroicons-check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
                </button>

                <div class="my-1 border-t border-gray-100 dark:border-gray-800" />

                <!-- Agent list -->
                <div class="max-h-52 overflow-y-auto">
                  <button
                    v-for="a in agents"
                    :key="a.id"
                    @click="toggleAgent(a.id)"
                    :class="[
                      'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-start transition-colors',
                      isAgentSelected(a.id) ? 'bg-indigo-50 dark:bg-indigo-950' : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                    ]"
                  >
                    <DataSourceIcon
                      v-if="a.connections?.[0]?.type || a.icon"
                      :type="a.connections?.[0]?.type"
                      :connector-key="a.connections?.[0]?.connector_key"
                      :icon="a.icon"
                      class="h-4 w-4 flex-shrink-0"
                    />
                    <UIcon v-else name="heroicons-circle-stack" class="w-4 h-4 text-gray-400 flex-shrink-0" />
                    <span :class="['text-xs font-medium truncate flex-1', isAgentSelected(a.id) ? 'text-indigo-700' : 'text-gray-700 dark:text-gray-300']">{{ a.name }}</span>
                    <!-- Connect chip for user_required agents not yet authenticated.
                         Nested as a span (the row is a <button>) to keep markup valid. -->
                    <span
                      v-if="needsUserConnection(a)"
                      role="button"
                      tabindex="0"
                      @click.stop="onConnect(a)"
                      @keydown.enter.stop="onConnect(a)"
                      :class="[
                        'flex-shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] rounded border transition-colors',
                        connectingId === a.id
                          ? 'text-blue-400 bg-blue-50 dark:bg-blue-950 border-blue-100 cursor-default'
                          : 'text-blue-600 bg-blue-50 dark:bg-blue-950 border-blue-200 hover:bg-blue-100 dark:hover:bg-blue-900/50'
                      ]"
                    >
                      <Spinner v-if="connectingId === a.id" class="w-3 h-3" />
                      <UIcon v-else name="heroicons-key" class="w-3 h-3" />
                      {{ $t('data.connect') }}
                    </span>
                    <UIcon v-else-if="isAgentSelected(a.id)" name="heroicons-check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
                  </button>
                </div>

                <div class="my-1 border-t border-gray-100 dark:border-gray-800" />

                <!-- View all -->
                <NuxtLink
                  to="/agents"
                  class="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 hover:text-gray-600 dark:hover:text-gray-400 transition-colors"
                >
                  <AgentIcon class="w-3.5 h-3.5 flex-shrink-0" />
                  <span class="text-xs">{{ $t('nav.viewAllAgents') }}</span>
                </NuxtLink>
              </template>
            </div>
          </div>
        </div>
      </template>
    </UPopover>

    <!-- User credentials / OAuth modal for connecting user_required agents -->
    <UserDataSourceCredentialsModal
      v-model="showCredsModal"
      :data-source="selectedConnectDs"
      @saved="onCredentialsSaved"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import AgentFlyout from '~/components/AgentFlyout.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import AgentIcon from '~/components/icons/AgentIcon.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'

const props = withDefaults(defineProps<{
  collapsed?: boolean
  showText?: boolean
  showLabel?: boolean
  // Nav mode: render as a left-nav item. Dropdown opens on hover, clicking the
  // trigger goes to the Agents explorer, and clicking an agent deep-links to it.
  nav?: boolean
}>(), {
  collapsed: false,
  showText: true,
  showLabel: true,
  nav: false
})

const router = useRouter()
const isRouteActive = (path: string) => {
  const p = useRoute().path
  return p === path || p.startsWith(path + '/')
}
const onNavClick = (e: MouseEvent) => { e.preventDefault(); router.push('/agents') }
const goToAgents = () => router.push('/agents')
const goToAgent = (id: string) => router.push(`/agents?agent=${id}`)
// Nav trigger reflects a single selected agent (like the old context selector),
// else shows "Agents" with stacked icons.
const navSelected = computed(() => (!isAllAgents.value && selectedAgentObjects.value.length === 1) ? selectedAgentObjects.value[0] : null)
const navLabel = computed(() => {
  if (navSelected.value) return navSelected.value.name || 'Agent'
  const n = selectedAgentObjects.value.length
  return (!isAllAgents.value && n > 1) ? `${n} agents` : 'Agents'
})
// First 3 agents' icons for the stacked nav preview: connection type + brand
// connector_key (for MCP provider icons) + any per-agent custom icon override.
const navIconAgents = computed(() => {
  const items: { type?: string; connector_key?: string | null; icon?: string | null }[] = []
  for (const a of (agents.value || [])) {
    const c = a.connections?.[0]
    if (c?.type || a.icon) items.push({ type: c?.type, connector_key: c?.connector_key, icon: a.icon })
    if (items.length >= 3) break
  }
  return items
})

// Agent management
const {
  agents,
  loading,
  isAllAgents,
  currentAgentName,
  selectedAgentObjects,
  toggleAgent,
  isAgentSelected,
  initAgent
} = useAgent()

// Connect (user credentials / OAuth) affordance for user_required agents.
const { connectingId, needsUserConnection, startConnect, asCredentialsModalSource } = useDataSourceConnect()
const showCredsModal = ref(false)
const selectedConnectDs = ref<any>(null)

async function onConnect(agent: any) {
  // OAuth-only (Entra/OBO) redirects straight to the provider; anything else
  // falls back to the credentials modal.
  const openModal = await startConnect(agent)
  if (!openModal) return
  selectedConnectDs.value = asCredentialsModalSource(agent)
  showCredsModal.value = true
}

async function onCredentialsSaved() {
  showCredsModal.value = false
  // Re-fetch so the freshly-connected agent drops its Connect chip.
  await initAgent()
}

// Returns the connection type when exactly one agent is selected (for icon display)
const singleSelectedConnection = computed(() => {
  const selected = selectedAgentObjects.value
  if (selected.length === 1) {
    return selected[0].connections?.[0]?.type || null
  }
  return null
})

// Agent hover preview
const hoveredAgentId = ref<string | null>(null)
const flyout = reactive({ visible: false, top: 0, left: 0 })
let flyoutHideTimer: ReturnType<typeof setTimeout> | null = null

const hideFlyoutNow = () => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  flyout.visible = false
  hoveredAgentId.value = null
}

const showFlyoutAtEvent = (evt: MouseEvent) => {
  const el = evt.currentTarget as HTMLElement | null
  if (!el) return
  const rect = el.getBoundingClientRect()

  // Position to the right of the hovered row, with a small gap.
  // Clamp to viewport height to avoid going off-screen.
  const desiredLeft = rect.right + 12
  const desiredTop = rect.top - 8
  const maxTop = window.innerHeight - 720 // flyout approx height
  flyout.left = Math.max(12, desiredLeft)
  flyout.top = Math.max(12, Math.min(desiredTop, maxTop))
  flyout.visible = true
}

const onAgentHover = (agentId: string, evt: MouseEvent) => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  if (typeof window !== 'undefined') showFlyoutAtEvent(evt)
  hoveredAgentId.value = agentId
}

const onAgentHoverLeave = () => {
  // Give the user time to move cursor from list → flyout
  if (flyoutHideTimer) clearTimeout(flyoutHideTimer)
  flyoutHideTimer = setTimeout(hideFlyoutNow, 120)
}

const onFlyoutEnter = () => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  flyout.visible = true
}

const onFlyoutLeave = () => {
  onAgentHoverLeave()
}

const route = useRoute()
watch(() => route.fullPath, hideFlyoutNow)

onBeforeUnmount(hideFlyoutNow)
</script>
