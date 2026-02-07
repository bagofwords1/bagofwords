<template>
  <NuxtLink
    :to="`/reports/${artifact.report_id}`"
    class="group block bg-white rounded-xl border border-gray-200 overflow-hidden hover:shadow-lg hover:border-blue-300 transition-all duration-200"
  >
    <!-- Thumbnail -->
    <div class="aspect-[4/3] bg-gray-100 relative overflow-hidden">
      <img
        v-if="thumbnailUrl && !imageError"
        :src="thumbnailUrl"
        :alt="artifact.title || 'Artifact preview'"
        class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
        @error="onImageError"
      />
      <div
        v-else
        class="w-full h-full flex items-center justify-center bg-gradient-to-br from-gray-50 to-gray-100"
      >
        <Icon
          :name="artifact.mode === 'slides' ? 'heroicons:presentation-chart-bar' : 'heroicons:chart-bar-square'"
          class="w-12 h-12 text-gray-300"
        />
      </div>

      <!-- Mode badge -->
      <div class="absolute top-2 right-2">
        <span
          :class="[
            'px-2 py-0.5 text-xs font-medium rounded-full',
            artifact.mode === 'slides'
              ? 'bg-purple-100 text-purple-700'
              : 'bg-blue-100 text-blue-700'
          ]"
        >
          {{ artifact.mode === 'slides' ? 'Slides' : 'Dashboard' }}
        </span>
      </div>
    </div>

    <!-- Content -->
    <div class="p-3 text-left">
      <h3 class="font-medium text-gray-900 truncate text-sm">
        {{ artifact.title || artifact.report_title || 'Untitled' }}
      </h3>
      <p class="text-xs text-gray-400 mt-1 truncate">
        {{ artifact.user_name ? `by ${artifact.user_name}` : '' }}
      </p>
    </div>
  </NuxtLink>
</template>

<script setup lang="ts">
interface Artifact {
  id: string
  report_id: string
  title?: string
  mode: string
  thumbnail_path?: string
  created_at: string
  report_title?: string
  user_name?: string
}

const props = defineProps<{
  artifact: Artifact
}>()

const config = useRuntimeConfig()
const imageError = ref(false)

const thumbnailUrl = computed(() => {
  if (!props.artifact.thumbnail_path) return null
  return `${config.public.baseURL}/artifacts/${props.artifact.id}/thumbnail`
})

const onImageError = () => {
  imageError.value = true
}
</script>
