<template>
  <div class="slide-viewer h-full w-full flex flex-col bg-slate-900">
    <!-- Current Slide Display -->
    <div class="flex-1 flex items-center justify-center p-4 relative min-h-0">
      <!-- Loading state -->
      <div v-if="loading" class="flex items-center justify-center text-slate-400">
        <Icon name="heroicons:arrow-path" class="w-6 h-6 animate-spin mr-2" />
        <span>Loading slides...</span>
      </div>

      <!-- Error state -->
      <div v-else-if="error" class="flex flex-col items-center justify-center text-slate-400">
        <Icon name="heroicons:exclamation-triangle" class="w-8 h-8 mb-2 text-amber-500" />
        <span>{{ error }}</span>
      </div>

      <!-- No previews state -->
      <div v-else-if="previewUrls.length === 0" class="flex flex-col items-center justify-center text-slate-400">
        <Icon name="heroicons:photo" class="w-12 h-12 mb-3 opacity-50" />
        <span>No preview images available</span>
      </div>

      <!-- Slide image -->
      <img
        v-else
        :src="currentSlideUrl"
        :alt="`Slide ${currentSlide + 1}`"
        class="max-h-full max-w-full object-contain rounded-lg shadow-2xl"
        @load="onImageLoad"
        @error="onImageError"
      />

      <!-- Navigation arrows (overlay) -->
      <button
        v-if="previewUrls.length > 1"
        @click="prevSlide"
        :disabled="currentSlide === 0"
        class="absolute left-4 top-1/2 -translate-y-1/2 p-2 rounded-full bg-black/30 hover:bg-black/50 text-white disabled:opacity-30 disabled:cursor-not-allowed transition-all"
      >
        <Icon name="heroicons:chevron-left" class="w-6 h-6" />
      </button>
      <button
        v-if="previewUrls.length > 1"
        @click="nextSlide"
        :disabled="currentSlide >= slideCount - 1"
        class="absolute right-4 top-1/2 -translate-y-1/2 p-2 rounded-full bg-black/30 hover:bg-black/50 text-white disabled:opacity-30 disabled:cursor-not-allowed transition-all"
      >
        <Icon name="heroicons:chevron-right" class="w-6 h-6" />
      </button>
    </div>

    <!-- Bottom bar: thumbnails + navigation -->
    <div v-if="previewUrls.length > 0" class="flex-shrink-0 bg-slate-800 border-t border-slate-700">
      <!-- Slide counter -->
      <div class="flex items-center justify-center py-2">
        <span class="text-white text-sm font-medium">
          {{ currentSlide + 1 }} / {{ slideCount }}
        </span>
      </div>

      <!-- Thumbnail strip -->
      <div class="flex gap-2 px-4 pb-3 overflow-x-auto justify-center">
        <button
          v-for="(url, index) in previewUrls"
          :key="index"
          @click="goToSlide(index)"
          :class="[
            'flex-shrink-0 w-16 h-10 rounded overflow-hidden border-2 transition-all hover:scale-105',
            currentSlide === index ? 'border-blue-500 ring-2 ring-blue-500/50' : 'border-transparent opacity-60 hover:opacity-100'
          ]"
        >
          <img
            :src="url"
            :alt="`Thumbnail ${index + 1}`"
            class="w-full h-full object-cover"
          />
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
const config = useRuntimeConfig();
const { token } = useAuth();
const { organization, ensureOrganization } = useOrganization();

const props = defineProps<{
  artifactId: string;
}>();

const currentSlide = ref(0);
const previewUrls = ref<string[]>([]);  // These will be blob URLs
const loading = ref(true);
const error = ref<string | null>(null);

const slideCount = computed(() => previewUrls.value.length);
const currentSlideUrl = computed(() => previewUrls.value[currentSlide.value] || '');

// Fetch preview URLs on mount
onMounted(async () => {
  await fetchPreviews();
});

// Watch for artifact changes
watch(() => props.artifactId, async () => {
  currentSlide.value = 0;
  // Revoke old blob URLs to free memory
  previewUrls.value.forEach(url => {
    if (url.startsWith('blob:')) URL.revokeObjectURL(url);
  });
  await fetchPreviews();
});

// Cleanup blob URLs on unmount
onUnmounted(() => {
  previewUrls.value.forEach(url => {
    if (url.startsWith('blob:')) URL.revokeObjectURL(url);
  });
});

async function fetchPreviews() {
  loading.value = true;
  error.value = null;

  try {
    // First get the list of preview endpoints
    const { data, error: fetchError } = await useMyFetch(`/api/artifacts/${props.artifactId}/previews`);

    if (fetchError.value) {
      throw new Error(fetchError.value.message || 'Failed to fetch previews');
    }

    if (data.value && data.value.previews && data.value.previews.length > 0) {
      // Ensure organization is loaded before fetching images
      const org = await ensureOrganization();

      // Fetch each image with auth headers and convert to blob URLs
      const blobUrls: string[] = [];

      for (const previewPath of data.value.previews) {
        try {
          const headers: Record<string, string> = {
            Authorization: `${token.value}`,
          };
          if (org?.id) {
            headers['X-Organization-Id'] = org.id;
          }

          const response = await fetch(`${config.public.baseURL}${previewPath}`, { headers });

          if (!response.ok) {
            console.error(`Failed to fetch preview: ${response.status}`);
            continue;
          }

          const blob = await response.blob();
          const blobUrl = URL.createObjectURL(blob);
          blobUrls.push(blobUrl);
        } catch (e) {
          console.error('Error fetching preview image:', e);
        }
      }

      previewUrls.value = blobUrls;
    } else {
      previewUrls.value = [];
    }
  } catch (e: any) {
    console.error('Failed to fetch slide previews:', e);
    error.value = e.message || 'Failed to load previews';
    previewUrls.value = [];
  } finally {
    loading.value = false;
  }
}

function prevSlide() {
  if (currentSlide.value > 0) {
    currentSlide.value--;
  }
}

function nextSlide() {
  if (currentSlide.value < slideCount.value - 1) {
    currentSlide.value++;
  }
}

function goToSlide(index: number) {
  currentSlide.value = index;
}

function onImageLoad() {
  // Image loaded successfully
}

function onImageError() {
  error.value = 'Failed to load slide image';
}

// Keyboard navigation
onMounted(() => {
  const handleKeydown = (e: KeyboardEvent) => {
    // Don't handle if user is typing in an input, textarea, or contenteditable element
    const target = e.target as HTMLElement;
    if (
      target.tagName === 'INPUT' ||
      target.tagName === 'TEXTAREA' ||
      target.isContentEditable
    ) {
      return;
    }

    if (e.key === 'ArrowRight' || e.key === ' ') {
      e.preventDefault();
      nextSlide();
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      prevSlide();
    }
  };

  window.addEventListener('keydown', handleKeydown);

  onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
  });
});
</script>
