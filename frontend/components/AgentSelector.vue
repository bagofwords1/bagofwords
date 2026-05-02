<template>
  <div class="agent-selector">
    <!-- Loading / empty placeholder — reserves layout space while agents load -->
    <div
      v-if="loading || (!loading && agents.length === 0)"
      :class="[
        'flex items-center w-full rounded-lg',
        'bg-white border border-gray-200 shadow-sm',
        collapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
      ]"
    >
      <UTooltip v-if="collapsed" :text="loading ? $t('common.loading') : $t('nav.noAgents')" :popper="{ placement: 'right' }">
        <Spinner v-if="loading" class="w-4 h-4 text-gray-300 animate-spin" />
        <AgentIcon class="w-4 h-4 text-gray-300" />
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
      :popper="{ placement: 'bottom-start', offsetDistance: 4, strategy: 'fixed' }"
      :ui="{
        width: 'max-w-none',
        container: 'overflow-visible',
        inner: 'overflow-visible'
      }"
    >
      <button
        :class="[
          'flex items-center w-full rounded-lg transition-all duration-200',
          'bg-white hover:bg-gray-50',
          'border border-gray-200 shadow-sm hover:shadow hover:border-gray-300',
          collapsed ? 'justify-center p-2' : 'gap-1.5 px-2.5 py-2'
        ]"
      >
        <UTooltip v-if="collapsed" :text="currentAgentName" :popper="{ placement: 'right' }">
          <span class="flex items-center justify-center w-5 h-5">
            <Spinner v-if="loading" class="w-4 h-4 text-gray-400 animate-spin" />
            <UIcon v-else name="heroicons-chevron-down" class="w-4 h-4 text-gray-500" />
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
              <span class="text-xs font-medium text-gray-700 truncate">{{ currentAgentName }}</span>
            </span>
          </span>
          <UIcon v-if="showText" name="heroicons-chevron-up-down" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </template>
      </button>

      <template #panel>
        <div class="overflow-visible">
          <!-- Agent list -->
          <div class="w-56 bg-white rounded-xl shadow-xl border border-gray-200 overflow-hidden">
            <div class="p-1.5">
              <div v-if="loading" class="flex items-center justify-center py-6">
                <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
              </div>

              <template v-else>
                <!-- All Agents -->
                <button
                  @click="toggleAgent(null)"
                  @mouseenter="hoveredAgentId = null"
                  @mouseleave="onAgentHoverLeave()"
                  :class="[
                    'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-start transition-colors',
                    isAllAgents ? 'bg-indigo-50' : 'hover:bg-gray-50'
                  ]"
                >
                  <AgentIcon class="w-4 h-4 text-gray-400 flex-shrink-0" />
                  <span :class="['text-xs font-medium flex-1', isAllAgents ? 'text-indigo-700' : 'text-gray-700']">{{ $t('nav.allAgents') }}</span>
                  <UIcon v-if="isAllAgents" name="heroicons-check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
                </button>

                <div class="my-1 border-t border-gray-100" />

                <!-- Agent list -->
                <div class="max-h-52 overflow-y-auto">
                  <button
                    v-for="a in agents"
                    :key="a.id"
                    @click="toggleAgent(a.id)"
                    @mouseenter="onAgentHover(a.id, $event)"
                    @mouseleave="onAgentHoverLeave()"
                    :class="[
                      'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-start transition-colors',
                      isAgentSelected(a.id) ? 'bg-indigo-50' : 'hover:bg-gray-50'
                    ]"
                  >
                    <DataSourceIcon
                      v-if="a.connections?.[0]?.type"
                      :type="a.connections[0].type"
                      class="h-4 w-4 flex-shrink-0"
                    />
                    <UIcon v-else name="heroicons-circle-stack" class="w-4 h-4 text-gray-400 flex-shrink-0" />
                    <span :class="['text-xs font-medium truncate flex-1', isAgentSelected(a.id) ? 'text-indigo-700' : 'text-gray-700']">{{ a.name }}</span>
                    <UIcon v-if="isAgentSelected(a.id)" name="heroicons-check" class="w-3.5 h-3.5 text-indigo-600 flex-shrink-0" />
                  </button>
                </div>

                <div class="my-1 border-t border-gray-100" />

                <!-- View all -->
                <a
                  href="/agents"
                  class="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-colors"
                >
                  <AgentIcon class="w-3.5 h-3.5 flex-shrink-0" />
                  <span class="text-xs">{{ $t('nav.viewAllAgents') }}</span>
                </a>
              </template>
            </div>
          </div>
        </div>
      </template>
    </UPopover>

    <!-- Agent flyout component -->
    <AgentFlyout
      :agent-id="hoveredAgentId"
      :visible="flyout.visible"
      :position="flyout"
      @mouseenter="onFlyoutEnter"
      @mouseleave="onFlyoutLeave"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import AgentFlyout from '~/components/AgentFlyout.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import AgentIcon from '~/components/icons/AgentIcon.vue'

const props = withDefaults(defineProps<{
  collapsed?: boolean
  showText?: boolean
  showLabel?: boolean
}>(), {
  collapsed: false,
  showText: true,
  showLabel: true
})

// Agent management
const {
  agents,
  loading,
  isAllAgents,
  currentAgentName,
  selectedAgentObjects,
  toggleAgent,
  isAgentSelected
} = useAgent()

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
  flyoutHideTimer = setTimeout(() => {
    flyout.visible = false
    hoveredAgentId.value = null
  }, 120)
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
</script>

