<template>
  <div class="space-y-2">
    <!-- Header with type and name -->
    <div class="flex items-center space-x-2">
      <UIcon :name="getResourceIcon(resource.resource_type)" class="w-4 h-4 text-purple-600" />
      <span class="font-medium text-gray-900">{{ resource.name }}</span>
      <span class="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded">{{ resource.resource_type }}</span>
    </div>

    <!-- Description -->
    <div v-if="resource.description" class="text-gray-600 text-sm">
      {{ resource.description }}
    </div>

    <!-- Datasource and Custom SQL content -->
    <div v-if="resource.sql_content" class="bg-gray-100 p-2 rounded font-mono text-xs overflow-x-auto">
      <div class="text-gray-500 mb-1">Content:</div>
      <pre class="whitespace-pre-wrap">{{ truncateText(resource.sql_content, 1200) }}</pre>
    </div>

    <!-- Columns / Fields -->
    <div v-if="resource.columns && resource.columns.length > 0" class="mt-2">
      <div class="text-xs font-medium text-gray-700 mb-1">{{ columnsTitle }}</div>
      <div class="overflow-x-auto">
        <table class="min-w-full text-xs border border-gray-200 rounded">
          <thead class="bg-gray-50">
            <tr>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Name</th>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Type</th>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Role</th>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Aggregation</th>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Calculated</th>
              <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Formula / Description</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(column, index) in resource.columns" :key="index" class="border-b border-gray-100">
              <td class="px-2 py-1 font-mono text-gray-900">{{ column.name || '-' }}</td>
              <td class="px-2 py-1 text-gray-600">{{ column.data_type || '-' }}</td>
              <td class="px-2 py-1 text-gray-600">{{ column.role || '-' }}</td>
              <td class="px-2 py-1 text-gray-600">{{ column.default_aggregation || '-' }}</td>
              <td class="px-2 py-1 text-gray-600">{{ column.is_calculated ? 'yes' : 'no' }}</td>
              <td class="px-2 py-1 text-gray-600">{{ truncateText(column.formula || column.description || '', 120) || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Parameters specific table -->
    <div v-if="resource.resource_type === 'tableau_parameter' && resource.raw_data" class="text-xs text-gray-600">
      <div><span class="font-medium">Type:</span> {{ resource.raw_data.data_type || '-' }}</div>
      <div v-if="resource.raw_data.current_value !== undefined"><span class="font-medium">Current:</span> {{ resource.raw_data.current_value }}</div>
    </div>

    <!-- Dependencies -->
    <div v-if="resource.depends_on && resource.depends_on.length > 0" class="text-gray-500 text-xs">
      <span class="font-medium">Depends on:</span>
      <span class="ml-1">{{ resource.depends_on.slice(0, 3).join(', ') }}</span>
      <span v-if="resource.depends_on.length > 3" class="ml-1">+{{ resource.depends_on.length - 3 }} more</span>
    </div>

    <!-- Path -->
    <div v-if="resource.path" class="text-gray-400 text-xs">
      {{ resource.path.split('/').pop() }}
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

const columnsTitle = computed(() => {
  if (props.resource.resource_type === 'tableau_datasource') return 'Fields';
  return 'Columns';
});

function getResourceIcon(resourceType: string): string {
  const iconMap: Record<string, string> = {
    'tableau_datasource': 'heroicons:database',
    'tableau_custom_sql': 'heroicons:code-bracket',
    'tableau_join': 'heroicons:link',
    'tableau_parameter': 'heroicons:adjustments-horizontal',
  };
  return iconMap[resourceType] || 'heroicons:document';
}

function truncateText(text: string, maxLength: number): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}
</script>


