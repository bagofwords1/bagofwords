<template>
  <NuxtLink
    :to="reportLink"
    class="group block bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-lg hover:border-blue-300 transition-all duration-200"
  >
    <!-- Thumbnail -->
    <div class="aspect-[4/3] relative overflow-hidden" :class="!thumbnailUrl || imageError ? badgeStyle.cardBg : ''">
      <!-- Actual thumbnail -->
      <img
        v-if="thumbnailUrl && !imageError"
        :src="thumbnailUrl"
        :alt="report.title || 'Report preview'"
        class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
        @error="onImageError"
      />
      <!-- Placeholder when no thumbnail -->
      <div
        v-else
        class="w-full h-full flex items-center justify-center"
      >
        <Icon
          :name="reportIcon"
          class="w-12 h-12"
          :class="badgeStyle.iconColor"
        />
      </div>

      <!-- Mode badge - bottom left -->
      <div class="absolute bottom-2 left-2">
        <span
          :class="[
            'px-2 py-0.5 text-xs font-medium rounded-full',
            badgeStyle.bg,
            badgeStyle.text
          ]"
        >
          {{ badgeStyle.label }}
        </span>
      </div>
    </div>

    <!-- Content -->
    <div class="p-3 text-left">
      <h3 class="font-medium text-gray-900 truncate text-sm">
        {{ report.title || 'Untitled' }}
      </h3>
      <p class="text-xs text-gray-400 mt-1 truncate">
        {{ report.user?.name ? `by ${report.user.name}` : '' }}
      </p>
    </div>
  </NuxtLink>
</template>

<script setup lang="ts">
interface Report {
  id: string
  title?: string
  slug: string
  status: string
  user: { id: string; name?: string; email?: string }
  artifact_modes: string[]
  conversation_share_enabled: boolean
  conversation_share_token?: string
  thumbnail_url?: string
}

const props = defineProps<{
  report: Report
  viewMode: 'org' | 'my'
}>()

const config = useRuntimeConfig()
const imageError = ref(false)

const hasArtifact = computed(() => props.report.artifact_modes?.length > 0)
const hasSlides = computed(() => props.report.artifact_modes?.includes('slides'))
const hasDashboard = computed(() => props.report.artifact_modes?.includes('page'))

const thumbnailUrl = computed(() => {
  if (!props.report.thumbnail_url) return null
  return `${config.public.baseURL}${props.report.thumbnail_url}`
})

const onImageError = () => {
  imageError.value = true
}

const reportLink = computed(() => {
  if (props.viewMode === 'my') {
    return `/reports/${props.report.id}`
  }
  // Org view: published reports
  if (hasArtifact.value) {
    return `/r/${props.report.id}`
  }
  if (props.report.conversation_share_enabled && props.report.conversation_share_token) {
    return `/c/${props.report.conversation_share_token}`
  }
  return `/r/${props.report.id}`
})

const reportIcon = computed(() => {
  if (hasSlides.value) return 'heroicons:presentation-chart-bar'
  if (hasDashboard.value) return 'heroicons:chart-bar-square'
  return 'heroicons:chat-bubble-left-right'
})

const badgeStyle = computed(() => {
  if (hasSlides.value) {
    return { bg: 'bg-purple-100', text: 'text-purple-700', label: 'Slides', cardBg: 'bg-purple-50', iconColor: 'text-purple-300' }
  }
  if (hasDashboard.value) {
    return { bg: 'bg-blue-100', text: 'text-blue-700', label: 'Dashboard', cardBg: 'bg-blue-50', iconColor: 'text-blue-300' }
  }
  return { bg: 'bg-gray-100', text: 'text-gray-700', label: 'Chat', cardBg: 'bg-gray-50', iconColor: 'text-gray-300' }
})
</script>
