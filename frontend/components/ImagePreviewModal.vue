<template>
  <Teleport to="body">
    <div v-if="isOpen"
         class="fixed inset-0 z-50 flex items-center justify-center bg-black/80"
         @click.self="close">
      <div class="relative max-w-4xl max-h-[90vh]">
        <button @click="close"
                class="absolute -top-10 right-0 text-white hover:text-gray-300 p-2">
          <Icon name="heroicons-x-mark" class="w-6 h-6" />
        </button>
        <img v-if="imageUrl"
             :src="imageUrl"
             :alt="imageName"
             class="max-w-full max-h-[85vh] object-contain rounded-lg" />
        <div v-else class="w-64 h-64 bg-gray-800 rounded-lg flex items-center justify-center">
          <Spinner class="w-8 h-8 text-white" />
        </div>
        <div class="text-white text-sm text-center mt-2 opacity-75">{{ imageName }}</div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const isOpen = ref(false)
const imageUrl = ref('')
const imageName = ref('')

const { getImageUrl } = useAuthenticatedImage()

async function open(file: { id: string; filename: string }) {
  imageName.value = file.filename
  isOpen.value = true
  imageUrl.value = await getImageUrl(file.id)
}

function close() {
  isOpen.value = false
  imageUrl.value = ''
  imageName.value = ''
}

defineExpose({ open, close })
</script>
