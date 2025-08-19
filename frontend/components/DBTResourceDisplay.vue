<template>
  <div class="space-y-2">
    <!-- Header with type and name -->
    <div class="flex items-center space-x-2">
      <UIcon :name="getResourceIcon(resource.resource_type)" class="w-4 h-4 text-blue-600" />
      <span class="font-medium text-gray-900">{{ resource.name }}</span>
      <span class="px-2 py-1 text-xs bg-blue-100 text-blue-700 rounded">{{ resource.resource_type }}</span>
    </div>

    <!-- Description -->
    <div v-if="resource.description" class="text-gray-600 text-sm">
      {{ resource.description }}
    </div>

    <!-- Key Information based on resource type -->
    <div class="grid grid-cols-1 gap-2 text-xs">
      <!-- Model specific -->
      <template v-if="resource.resource_type === 'model' || resource.resource_type === 'model_config' || resource.resource_type === 'dbt_model'">
        <div v-if="resource.sql_content" class="bg-gray-100 p-2 rounded font-mono text-xs overflow-x-auto">
          <div class="text-gray-500 mb-1">SQL:</div>
          <pre class="whitespace-pre-wrap">{{ truncateText(resource.sql_content, 200) }}</pre>
        </div>
        <div v-if="columnCount > 0" class="text-gray-500">
          {{ columnCount }} columns
        </div>
        <!-- Columns Table -->
        <div v-if="resource.columns && resource.columns.length > 0" class="mt-2">
          <div class="text-xs font-medium text-gray-700 mb-1">Columns:</div>
          <div class="overflow-x-auto">
            <table class="min-w-full text-xs border border-gray-200 rounded">
              <thead class="bg-gray-50">
                <tr>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Name</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Type</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Description</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(column, index) in resource.columns" :key="index" class="border-b border-gray-100">
                  <td class="px-2 py-1 font-mono text-gray-900">{{ column.name || column.field_name || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">{{ column.data_type || column.type || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">{{ truncateText(column.description || '', 50) || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>

      <!-- Source specific -->
      <template v-if="resource.resource_type === 'source' || resource.resource_type === 'dbt_source'">
        <div class="text-gray-500">
          <span v-if="rawData?.database">Database: {{ rawData.database }}</span>
          <span v-if="rawData?.schema" class="ml-2">Schema: {{ rawData.schema }}</span>
        </div>
        <!-- Source Columns Table -->
        <div v-if="resource.columns && resource.columns.length > 0" class="mt-2">
          <div class="text-xs font-medium text-gray-700 mb-1">Columns:</div>
          <div class="overflow-x-auto">
            <table class="min-w-full text-xs border border-gray-200 rounded">
              <thead class="bg-gray-50">
                <tr>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Name</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Type</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Description</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(column, index) in resource.columns" :key="index" class="border-b border-gray-100">
                  <td class="px-2 py-1 font-mono text-gray-900">{{ column.name || column.field_name || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">{{ column.data_type || column.type || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">{{ truncateText(column.description || '', 50) || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </template>

      <!-- Metric specific -->
      <template v-if="resource.resource_type === 'metric' || resource.resource_type === 'dbt_metric'">
        <div class="text-gray-500">
          <span v-if="rawData?.calculation_method">Method: {{ rawData.calculation_method }}</span>
          <span v-if="rawData?.expression" class="ml-2">Expression: {{ rawData.expression }}</span>
        </div>
      </template>

      <!-- Exposure specific -->
      <template v-if="resource.resource_type === 'exposure' || resource.resource_type === 'dbt_exposure'">
        <div class="text-gray-500">
          <span v-if="rawData?.type">Type: {{ rawData.type }}</span>
          <span v-if="rawData?.maturity" class="ml-2">Maturity: {{ rawData.maturity }}</span>
          <a v-if="rawData?.url" :href="rawData.url" target="_blank" class="ml-2 text-blue-600 hover:underline">
            View Exposure
          </a>
        </div>
      </template>

      <!-- Dependencies -->
      <div v-if="resource.depends_on && resource.depends_on.length > 0" class="text-gray-500">
        <span class="font-medium">Depends on:</span>
        <span class="ml-1">{{ resource.depends_on.slice(0, 3).join(', ') }}</span>
        <span v-if="resource.depends_on.length > 3" class="ml-1">+{{ resource.depends_on.length - 3 }} more</span>
      </div>

      <!-- Generic Columns Table (for resource types not handled above) -->
      <div v-if="!['model', 'model_config', 'dbt_model', 'source', 'dbt_source'].includes(resource.resource_type) && resource.columns && resource.columns.length > 0" class="mt-2">
        <div class="text-xs font-medium text-gray-700 mb-1">Columns:</div>
        <div class="overflow-x-auto">
          <table class="min-w-full text-xs border border-gray-200 rounded">
            <thead class="bg-gray-50">
              <tr>
                <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Name</th>
                <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Type</th>
                <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Description</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(column, index) in resource.columns" :key="index" class="border-b border-gray-100">
                <td class="px-2 py-1 font-mono text-gray-900">{{ column.name || column.field_name || '-' }}</td>
                <td class="px-2 py-1 text-gray-600">{{ column.data_type || column.type || '-' }}</td>
                <td class="px-2 py-1 text-gray-600">{{ truncateText(column.description || '', 50) || '-' }}</td>
              </tr>
            </tbody>
          </table>
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

function getResourceIcon(resourceType: string): string {
  const iconMap: Record<string, string> = {
    'model': 'heroicons:cube',
    'dbt_model': 'heroicons:cube',
    'model_config': 'heroicons:cube',
    'metric': 'heroicons:hashtag',
    'dbt_metric': 'heroicons:hashtag',
    'source': 'heroicons:database',
    'dbt_source': 'heroicons:database',
    'seed': 'heroicons:document-text',
    'dbt_seed': 'heroicons:document-text',
    'macro': 'heroicons:code-bracket',
    'dbt_macro': 'heroicons:code-bracket',
    'test': 'heroicons:shield-check',
    'dbt_test': 'heroicons:shield-check',
    'singular_test': 'heroicons:shield-check',
    'dbt_singular_test': 'heroicons:shield-check',
    'exposure': 'heroicons:presentation-chart-line',
    'dbt_exposure': 'heroicons:presentation-chart-line'
  };
  return iconMap[resourceType] || 'heroicons:document';
}

function truncateText(text: string, maxLength: number): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}
</script>