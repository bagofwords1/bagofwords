<template>
  <div class="space-y-2">
    <!-- Header with type and name -->
    <div class="flex items-center space-x-2">
      <UIcon name="heroicons:document" class="w-4 h-4 text-gray-600" />
      <span class="font-medium text-gray-900">{{ resource.name }}</span>
      <span class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded">{{ resource.resource_type }}</span>
    </div>

    <!-- Description -->
    <div v-if="resource.description" class="text-gray-600 text-sm">
      {{ resource.description }}
    </div>

    <!-- Basic Information -->
    <div class="grid grid-cols-1 gap-2 text-xs">
      <!-- SQL Content if available -->
      <div v-if="resource.sql_content" class="bg-gray-100 p-2 rounded font-mono text-xs overflow-x-auto">
        <div class="text-gray-500 mb-1">Content:</div>
        <pre class="whitespace-pre-wrap">{{ truncateText(resource.sql_content, 200) }}</pre>
      </div>

      <!-- Columns if available -->
      <div v-if="columnCount > 0" class="text-gray-500">
        {{ columnCount }} columns/fields
      </div>

      <!-- Dependencies -->
      <div v-if="resource.depends_on && resource.depends_on.length > 0" class="text-gray-500">
        <span class="font-medium">Depends on:</span>
        <span class="ml-1">{{ resource.depends_on.slice(0, 3).join(', ') }}</span>
        <span v-if="resource.depends_on.length > 3" class="ml-1">+{{ resource.depends_on.length - 3 }} more</span>
      </div>

      <!-- Key raw data fields -->
      <div v-if="keyRawDataFields.length > 0" class="text-gray-500">
        <div v-for="field in keyRawDataFields" :key="field.key" class="mb-1">
          <span class="font-medium">{{ field.key }}:</span>
          <span class="ml-1">{{ field.value }}</span>
        </div>
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

const columnCount = computed(() => {
  return props.resource.columns?.length || 0;
});

const keyRawDataFields = computed(() => {
  const data = rawData.value;
  if (!data || typeof data !== 'object') return [];

  // Extract key fields that might be interesting
  const interestingFields = ['type', 'label', 'connection', 'database', 'schema', 'table', 'view_name', 'model_name'];
  const fields = [];

  for (const field of interestingFields) {
    if (data[field] && typeof data[field] === 'string') {
      fields.push({
        key: field.replace('_', ' ').toUpperCase(),
        value: truncateText(data[field], 50)
      });
    }
  }

  return fields.slice(0, 3); // Limit to 3 fields to keep it minimal
});

function truncateText(text: string, maxLength: number): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}
</script>