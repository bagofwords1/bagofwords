<template>
  <div class="w-full p-2 flex justify-between text-sm sticky top-0 z-50 border-b-2 bg-white">
    <div class="flex items-center gap-2">
      <div class="space-x-0">
        <UTooltip text="Collapse">
          <button @click="$emit('toggleSplitScreen')" class="text-xs items-center flex hover:bg-gray-100 px-0 py-1 rounded">
            <Icon name="heroicons:x-mark" class="w-4 h-4" />
          </button>
        </UTooltip>
      </div>
      <div class="font-medium text-gray-700">
        Dashboard
      </div>
    </div>

    <div class="space-x-2 flex items-center">


      <UPopover v-if="edit" v-model="showAddMenu" :popper="{ placement: 'bottom-start' }">
        <UTooltip text="Add Component">
        <button class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
          <Icon name="heroicons:plus" />
        </button>
        </UTooltip>
        <template #panel>
          <div class="p-1 min-w-[160px]">
              <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-plus-circle" class="w-full justify-start" @click="emitAddText">
                Add Text
              </UButton>
          </div>
        </template>
      </UPopover>

      <UPopover v-model="showThemeMenu" :popper="{ placement: 'bottom-start' }">
        <UTooltip text="Theme">
          <button class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded" type="button">
            <Icon name="heroicons:paint-brush" />
          </button>
        </UTooltip>
        <template #panel>
          <div class="p-1 min-w-[160px]">
            <div v-for="option in themeOptions" :key="option.value" class="w-full">
               <UButton 
                 size="xs" 
                 color="gray" 
                 variant="ghost" 
                 :icon="getThemeIconReactive(option)"
                 class="w-full justify-start capitalize" 
                 @click="selectTheme(option.value)"
               >
                 {{ option.label }}
               </UButton>
            </div>
          </div>
        </template>
      </UPopover>

      <UTooltip text="Rerun report">
        <button @click="$emit('rerun')" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
          <Icon name="heroicons:play" />
        </button>
      </UTooltip>

      <CronModal :report="report" />

      <UTooltip text="Open dashboard in a new tab" v-if="report?.status === 'published'">
        <a :href="`/r/${report.id}`" target="_blank" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
          <Icon name="heroicons:arrow-top-right-on-square" />
        </a>
      </UTooltip>

      <UTooltip text="Full screen">
        <button @click="$emit('openFullscreen')" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
          <Icon name="heroicons:arrows-pointing-out" />
        </button>
      </UTooltip>

      <ShareModal :report="report" />
    </div>
  </div>
</template>

<script setup lang="ts">
import CronModal from '../CronModal.vue'
import ShareModal from '../ShareModal.vue'
import { computed, ref } from 'vue'

const props = defineProps<{
  report: any
  edit: boolean
  themeOverride: string
  themeOptions: Array<any>
  currentThemeDisplay: string
}>()

const emit = defineEmits<{
  (e: 'update:themeOverride', value: string): void
  (e: 'add:text'): void
  (e: 'rerun'): void
  (e: 'openFullscreen'): void
  (e: 'toggleSplitScreen'): void
}>()

const showAddMenu = ref(false)
const showThemeMenu = ref(false)

const internalTheme = computed({
  get: () => props.themeOverride,
  set: (val: string) => emit('update:themeOverride', val)
})

function emitAddText() {
  emit('add:text')
  showAddMenu.value = false
}

function selectTheme(value: string) {
  internalTheme.value = value
}

// This function is no longer used since we switched to the reactive version
// function getThemeIcon(option: any) { ... }

// Reactive version for template
const getThemeIconReactive = (option: any) => {
  const currentTheme = internalTheme.value || ''
  
  let isSelected = false
  
  // Special case: if option.value is empty, it represents the report's default theme
  if (option.value === '') {
    // This option represents "no override" - using the report's original theme
    // It should be selected when currentTheme is empty (no override)
    // OR when currentTheme matches the report's original theme name
    const reportTheme = props.report?.report_theme_name || props.report?.theme_name || 'default'
    isSelected = currentTheme === '' || currentTheme === reportTheme
  } else {
    // Normal theme matching for non-default options
    isSelected = currentTheme === option.value
  }
  return isSelected ? 'i-heroicons-check-20-solid' : undefined
}

</script>


