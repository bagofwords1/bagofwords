<template>
  <div class="domain-selector">
    <UPopover 
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
        <UTooltip v-if="collapsed" :text="currentDomainName" :popper="{ placement: 'right' }">
          <span class="flex items-center justify-center w-5 h-5">
            <Spinner v-if="loading" class="w-4 h-4 text-gray-400 animate-spin" />
            <UIcon v-else name="heroicons-chevron-down" class="w-4 h-4 text-gray-500" />
          </span>
        </UTooltip>
        <template v-else>
          <span v-if="showText" class="flex-1 text-left min-w-0">
            <span v-if="showLabel" class="block text-[8px] uppercase tracking-wide text-gray-400 font-semibold leading-none">CONTEXT</span>
            <span :class="['flex items-center gap-1.5', showLabel ? 'mt-0.5' : '']">
              <Spinner v-if="loading" class="w-3 h-3 text-gray-400 animate-spin flex-shrink-0" />
              <span class="text-xs font-medium text-gray-700 truncate">
                {{ currentDomainName }}
              </span>
            </span>
          </span>
          <UIcon v-if="showText" name="heroicons-chevron-down" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </template>
      </button>

      <template #panel>
        <div class="overflow-visible">
          <!-- Domain list - compact -->
          <div class="w-44 bg-white rounded-lg shadow-lg border border-gray-200 overflow-hidden flex-shrink-0">
            <div class="p-1">
              <!-- Loading state inside panel -->
              <div v-if="loading" class="flex items-center justify-center py-4">
                <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
              </div>

              <template v-else>
                <!-- All Domains option -->
                <button 
                  @click="toggleDomain(null)"
                  @mouseenter="hoveredDomainId = null"
                  @mouseleave="onDomainHoverLeave()"
                  :class="[
                    'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                    isAllDomains ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                  ]"
                >
                  <span class="text-xs font-medium">All Agents</span>
                  <UIcon v-if="isAllDomains" name="heroicons-check" class="w-3 h-3 ml-auto text-indigo-600" />
                </button>

                <!-- Divider -->
                <div class="my-1 border-t border-gray-100" />

                <!-- Domain list -->
                <div class="max-h-48 overflow-y-auto">
                  <button 
                    v-for="d in domains" 
                    :key="d.id"
                    @click="toggleDomain(d.id)"
                    @mouseenter="onDomainHover(d.id, $event)"
                    @mouseleave="onDomainHoverLeave()"
                    :class="[
                      'w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left transition-colors',
                      isDomainSelected(d.id) ? 'bg-indigo-50 text-indigo-700' : 'text-gray-600 hover:bg-gray-50'
                    ]"
                  >
                    <span class="text-xs font-medium truncate flex-1">{{ d.name }}</span>
                    <UIcon v-if="isDomainSelected(d.id)" name="heroicons-check" class="w-3 h-3 text-indigo-600 flex-shrink-0" />
                  </button>
                </div>

                <!-- Divider -->
                <div class="my-1 border-t border-gray-100" />

                <!-- Manage link -->
                <a 
                  href="/data"
                  class="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-left text-gray-400 hover:bg-gray-50 hover:text-gray-600 transition-colors"
                >
                  <UIcon name="heroicons-cog-6-tooth" class="w-3 h-3 flex-shrink-0" />
                  <span class="text-[11px]">Manage</span>
                </a>
              </template>
            </div>
          </div>
        </div>
      </template>
    </UPopover>

    <!-- Agent flyout component -->
    <AgentFlyout
      :agent-id="hoveredDomainId"
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

const props = withDefaults(defineProps<{
  collapsed?: boolean
  showText?: boolean
  showLabel?: boolean
}>(), {
  collapsed: false,
  showText: true,
  showLabel: true
})

// Domain management
const {
  domains,
  loading,
  isAllDomains,
  currentDomainName,
  toggleDomain,
  isDomainSelected
} = useDomain()

// Domain hover preview
const hoveredDomainId = ref<string | null>(null)
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

const onDomainHover = (domainId: string, evt: MouseEvent) => {
  if (flyoutHideTimer) {
    clearTimeout(flyoutHideTimer)
    flyoutHideTimer = null
  }
  if (typeof window !== 'undefined') showFlyoutAtEvent(evt)
  hoveredDomainId.value = domainId
}

const onDomainHoverLeave = () => {
  // Give the user time to move cursor from list → flyout
  if (flyoutHideTimer) clearTimeout(flyoutHideTimer)
  flyoutHideTimer = setTimeout(() => {
    flyout.visible = false
    hoveredDomainId.value = null
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
  onDomainHoverLeave()
}
</script>

