<template>
  <img v-if="blobUrl" :src="blobUrl" :alt="alt" :class="imgClass" @click="$emit('click')" />
  <div v-else :class="imgClass" class="bg-gray-100 animate-pulse flex items-center justify-center" @click="$emit('click')">
    <Icon name="heroicons-photo" class="w-4 h-4 text-gray-400" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'

const props = defineProps<{
  fileId: string
  alt?: string
  imgClass?: string
}>()

defineEmits(['click'])

const blobUrl = ref<string>('')
const { getImageUrl } = useAuthenticatedImage()

async function loadImage() {
  if (props.fileId) {
    blobUrl.value = await getImageUrl(props.fileId)
  }
}

onMounted(loadImage)

watch(() => props.fileId, loadImage)
</script>
