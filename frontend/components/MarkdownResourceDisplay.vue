<template>
  <div class="space-y-2">
    <!-- Header with type and name -->
    <div class="flex items-center space-x-2">
      <UIcon name="heroicons:document-text" class="w-4 h-4 text-green-600" />
      <span class="font-medium text-gray-900">{{ resource.name }}</span>
      <span class="px-2 py-1 text-xs bg-green-100 text-green-700 rounded">Markdown</span>
    </div>

    <!-- Description -->
    <div v-if="resource.description" class="text-gray-600 text-sm">
      {{ resource.description }}
    </div>

    <!-- Content preview -->
    <div class="space-y-2 text-xs">
      <div v-if="markdownContent" class="bg-gray-100 p-2 rounded">
        <div class="text-gray-500 mb-1">Content Preview:</div>
        <div class="prose prose-sm max-w-none text-gray-700">
          <pre class="whitespace-pre-wrap text-xs">{{ truncateText(markdownContent, 300) }}</pre>
        </div>
      </div>

      <!-- File info -->
      <div class="text-gray-500">
        <span v-if="fileSize">Size: {{ fileSize }}</span>
        <span v-if="lastModified" class="ml-2">Modified: {{ lastModified }}</span>
      </div>

      <!-- Path -->
      <div v-if="resource.path" class="text-gray-400 text-xs">
        {{ resource.path.split('/').pop() }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
interface Resource {
  id: string;
  name: string;
  resource_type: string;
  description?: string;
  path?: string;
  sql_content?: string;
  raw_data?: any;
  columns?: any[];
  depends_on?: string[];
}

interface Props {
  resource: Resource;
}

const props = defineProps<Props>();

const rawData = computed(() => {
  if (!props.resource.raw_data) return {};
  if (typeof props.resource.raw_data === 'string') {
    try {
      return JSON.parse(props.resource.raw_data);
    } catch {
      return {};
    }
  }
  return props.resource.raw_data;
});

const markdownContent = computed(() => {
  // Try to get content from various possible locations
  return rawData.value?.content || 
         rawData.value?.text || 
         props.resource.sql_content || 
         '';
});

const fileSize = computed(() => {
  const size = rawData.value?.size;
  if (!size) return null;
  
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
});

const lastModified = computed(() => {
  const modified = rawData.value?.last_modified || rawData.value?.modified_at;
  if (!modified) return null;
  
  try {
    return new Date(modified).toLocaleDateString();
  } catch {
    return null;
  }
});

function truncateText(text: string, maxLength: number): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}
</script>