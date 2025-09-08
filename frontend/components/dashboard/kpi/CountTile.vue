<template>
  <div class="h-full w-full p-2" :style="wrapperStyle">
    <div class="font-bold text-xs" :style="{ color: tokens.value?.textColor }">{{ widget.title }}</div>
    <div v-if="hasValue" class="text-xl font-bold mt-2" :style="{ color: tokens.value?.textColor }">
      {{ countValue ?? 'None' }}
    </div>
    <div v-else class="text-xs" :style="{ color: tokens.value?.textColor }">Loading..</div>
  </div>
</template>

<script setup lang="ts">
import { toRefs, computed, ref, watch } from 'vue'
import { useDashboardTheme } from '../composables/useDashboardTheme'

const props = defineProps<{
  widget?: any
  data?: any
  data_model?: any
  view?: Record<string, any> | null
  reportThemeName?: string | null
  reportOverrides?: Record<string, any> | null
  show_title?: boolean
}>()

const { reportThemeName, reportOverrides } = toRefs(props)
const { tokens, themeName } = useDashboardTheme(reportThemeName?.value, reportOverrides?.value, props.view || null)

const wrapperStyle = computed(() => ({
  backgroundColor: tokens.value?.background || 'transparent',
  color: tokens.value?.textColor || 'inherit'
}))

const tileKey = computed(() => `${themeName.value}:${tokens.value?.background || ''}`)

const countValue = ref<any>(null)
const hasValue = computed(() => countValue.value !== null && countValue.value !== undefined)

const updateData = () => {
  try {
    const rows = props?.data?.rows
    if (Array.isArray(rows) && rows.length > 0) {
      const first = rows[0]
      const firstVal = first ? Object.values(first)[0] : null
      countValue.value = firstVal as any
    } else {
      countValue.value = null
    }
  } catch {
    countValue.value = null
  }
}

watch(() => props.data, updateData, { deep: true, immediate: true })
</script>

<style scoped>
</style>


