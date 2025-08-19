<template>
  <div class="space-y-2">
    <!-- Header with type and name -->
    <div class="flex items-center space-x-2">
      <UIcon :name="getResourceIcon(resource.resource_type)" class="w-4 h-4 text-purple-600" />
      <span class="font-medium text-gray-900">{{ resource.name }}</span>
      <span class="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded">{{ formatResourceType(resource.resource_type) }}</span>
    </div>

    <!-- Description -->
    <div v-if="resource.description" class="text-gray-600 text-sm">
      {{ resource.description }}
    </div>

    <!-- Key Information based on resource type -->
    <div class="grid grid-cols-1 gap-2 text-xs">
      <!-- LookML Model specific -->
      <template v-if="resource.resource_type === 'lookml_model'">
        <div class="text-gray-500">
          <span v-if="rawData?.connection">Connection: {{ rawData.connection }}</span>
          <span v-if="rawData?.label" class="ml-2">Label: {{ rawData.label }}</span>
        </div>
      </template>

      <!-- LookML View specific -->
      <template v-if="resource.resource_type === 'lookml_view'">
        <div class="text-gray-500">
          <span v-if="rawData?.sql_table_name">Table: {{ rawData.sql_table_name }}</span>
          <span v-if="rawData?.label" class="ml-2">Label: {{ rawData.label }}</span>
        </div>
        <div v-if="fieldCount > 0" class="text-gray-500">
          {{ fieldCount }} fields (dimensions/measures)
        </div>
        <!-- Fields Table -->
        <div v-if="resource.columns && resource.columns.length > 0" class="mt-2">
          <div class="text-xs font-medium text-gray-700 mb-1">Fields:</div>
          <div class="overflow-x-auto">
            <table class="min-w-full text-xs border border-gray-200 rounded">
              <thead class="bg-gray-50">
                <tr>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Name</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Type</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">LookML Type</th>
                  <th class="px-2 py-1 text-left font-medium text-gray-700 border-b">Description</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(field, index) in resource.columns" :key="index" class="border-b border-gray-100">
                  <td class="px-2 py-1 font-mono text-gray-900">{{ field.field_name || field.name || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">{{ field.type || '-' }}</td>
                  <td class="px-2 py-1 text-gray-600">
                    <span class="px-1 py-0.5 text-xs rounded" :class="getFieldTypeClass(field.resource_type)">
                      {{ formatFieldType(field.resource_type) }}
                    </span>
                  </td>
                  <td class="px-2 py-1 text-gray-600">{{ truncateText(field.description || '', 40) || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div v-if="rawData?.derived_table" class="bg-gray-100 p-2 rounded font-mono text-xs">
          <div class="text-gray-500 mb-1">Derived Table SQL:</div>
          <pre class="whitespace-pre-wrap">{{ truncateText(rawData.derived_table.sql || '', 150) }}</pre>
        </div>
      </template>

      <!-- LookML Explore specific -->
      <template v-if="resource.resource_type === 'lookml_explore'">
        <div class="text-gray-500">
          <span v-if="rawData?.model_name">Model: {{ rawData.model_name }}</span>
          <span v-if="rawData?.view_name" class="ml-2">Base View: {{ rawData.view_name }}</span>
        </div>
      </template>

      <!-- Dependencies -->
      <div v-if="resource.depends_on && resource.depends_on.length > 0" class="text-gray-500">
        <span class="font-medium">Depends on:</span>
        <span class="ml-1">{{ resource.depends_on.slice(0, 3).join(', ') }}</span>
        <span v-if="resource.depends_on.length > 3" class="ml-1">+{{ resource.depends_on.length - 3 }} more</span>
      </div>

      <!-- Generic Fields Table (for other LookML resources with columns) -->
      <div v-if="!['lookml_view'].includes(resource.resource_type) && resource.columns && resource.columns.length > 0" class="mt-2">
        <div class="text-xs font-medium text-gray-700 mb-1">Fields:</div>
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
              <tr v-for="(field, index) in resource.columns" :key="index" class="border-b border-gray-100">
                <td class="px-2 py-1 font-mono text-gray-900">{{ field.field_name || field.name || '-' }}</td>
                <td class="px-2 py-1 text-gray-600">{{ field.type || '-' }}</td>
                <td class="px-2 py-1 text-gray-600">{{ truncateText(field.description || '', 50) || '-' }}</td>
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

const fieldCount = computed(() => {
  return props.resource.columns?.length || 0;
});

function getResourceIcon(resourceType: string): string {
  const iconMap: Record<string, string> = {
    'lookml_model': 'heroicons:cube-transparent',
    'lookml_view': 'heroicons:table-cells',
    'lookml_explore': 'heroicons:magnifying-glass',
    'lookml_dashboard': 'heroicons:chart-bar'
  };
  return iconMap[resourceType] || 'heroicons:document';
}

function formatResourceType(resourceType: string): string {
  return resourceType.replace('lookml_', '').replace('_', ' ').toUpperCase();
}

function truncateText(text: string, maxLength: number): string {
  if (!text) return '';
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + '...';
}

function formatFieldType(resourceType: string): string {
  if (!resourceType) return '';
  return resourceType.replace('lookml_', '').replace('_', ' ').toUpperCase();
}

function getFieldTypeClass(resourceType: string): string {
  const type = resourceType?.toLowerCase() || '';
  if (type.includes('dimension')) {
    return 'bg-blue-100 text-blue-700';
  } else if (type.includes('measure')) {
    return 'bg-green-100 text-green-700';
  } else if (type.includes('parameter')) {
    return 'bg-yellow-100 text-yellow-700';
  } else if (type.includes('filter')) {
    return 'bg-purple-100 text-purple-700';
  }
  return 'bg-gray-100 text-gray-700';
}
</script>