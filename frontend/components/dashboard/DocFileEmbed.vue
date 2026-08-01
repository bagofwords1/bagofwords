<template>
  <figure class="my-4">
    <div v-if="loading" class="flex items-center justify-center h-40 rounded-lg bg-gray-50 dark:bg-gray-800/50">
      <Spinner class="w-5 h-5" />
    </div>
    <div v-else-if="!src"
      class="flex items-center justify-center h-40 rounded-lg border border-dashed border-gray-200 dark:border-gray-700 text-[13px] text-gray-400 dark:text-gray-500">
      {{ $t('docViewer.imageUnavailable') }}
    </div>
    <img v-else :src="src" :alt="alt || ''" class="max-w-full h-auto rounded-lg mx-auto" />
    <figcaption v-if="alt" class="mt-2 text-center text-xs text-gray-500 dark:text-gray-400">{{ alt }}</figcaption>
  </figure>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'

const props = defineProps<{ fileId: string; alt?: string }>()

const src = ref('')
const loading = ref(true)
const { getImageUrl } = useAuthenticatedImage()

// The file endpoint needs a bearer token, so <img src> can't fetch it directly;
// the composable pulls the bytes and hands back a blob URL.
async function load() {
  loading.value = true
  try {
    src.value = await getImageUrl(props.fileId)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.fileId, load)
</script>
