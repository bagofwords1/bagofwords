<template>
    <div class="flex pl-2 md:pl-4 text-sm">
        <div class="w-full md:w-3/4 px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    Files
                </h1>
                <p class="mt-2 text-gray-500">Manage your organization files</p>

            </div>

            <div class="bg-white rounded-lg shadow mt-8">
<table class="min-w-full divide-y divide-gray-200">
  <thead class="bg-gray-50">
    <tr>
      <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">File</th>
      <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Metadata</th>
      <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Created At</th>
      <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                    </tr>
                    </thead>

                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="file in files" :key="file.id">
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="flex items-center">
                                    <UIcon name="heroicons-document-text" class="w-5 h-5 text-gray-500 mr-2" />
                                    {{ file.filename }}.
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div v-if="file.schemas.length > 0">
                                    <div v-for="schema in file.schemas" :key="schema.id">
                                        <UTooltip :text="Object.keys(schema.schema.fields).join(', ')">
                                            <div class="flex items-center">
                                                <Icon name="heroicons-view-columns" class="w-5 h-5 text-gray-500 mr-2" />
                                                {{ Object.keys(schema.schema.fields).length }} metadata fields
                                            </div>
                                        </UTooltip>
                                    </div>
                                </div>
                                <div v-else-if="file.tags.length > 0">
                                     <UTooltip :text="file.tags.map(tag => tag.key).join(', ')">
                                         <div class="flex items-center">
                                            <Icon name="heroicons-view-columns" class="w-5 h-5 text-gray-500 mr-2" />
                                            {{ file.tags.length }} metadata tags
                                        </div>
                                     </UTooltip>
                                </div>
                                <div v-else>
                                    No metadata
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">{{ file.created_at }}</td>
                            <td class="px-6 py-4 whitespace-nowrap flex items-center gap-2">
                                <button
                                    v-if="isQueryable(file) && useCan('create_data_source')"
                                    @click="createDataSource(file)"
                                    :disabled="loadingFileIds.has(file.id)"
                                    class="inline-flex items-center gap-1 text-xs font-medium text-white bg-primary-500 hover:bg-primary-600 disabled:opacity-50 rounded-md px-2.5 py-1.5 transition-colors"
                                >
                                    <Spinner v-if="loadingFileIds.has(file.id)" class="w-3 h-3" />
                                    <Icon v-else name="heroicons-magnifying-glass-circle" class="w-4 h-4" />
                                    Query this data
                                </button>
                                <button @click="downloadFile(file)" class="text-primary-500 hover:text-primary-700">
                                    <Icon name="heroicons-arrow-down-tray" class="w-5 h-5 text-gray-500" />
                                </button>
                            </td>
                        </tr>
                    </tbody>

                </table>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';
import { useExcel } from '~/composables/useExcel'
import { useCan } from '~/composables/usePermissions'

const { isExcel } = useExcel()
const files = ref([]);
const router = useRouter();
const toast = useToast();
const loadingFileIds = ref<Set<string>>(new Set())

definePageMeta({ auth: true })

const QUERYABLE_EXTENSIONS = ['.csv', '.xlsx', '.xls'];

function isQueryable(file: any): boolean {
  const name = (file.filename || '').toLowerCase();
  return QUERYABLE_EXTENSIONS.some(ext => name.endsWith(ext));
}

const getFiles = async () => {
  const response = await useMyFetch('/api/files', {
    method: 'GET',
    headers: {
        'Content-Type': 'application/json',
    },
  })
  files.value = response.data.value
}

const createDataSource = async (file: any) => {
  loadingFileIds.value = new Set([...loadingFileIds.value, file.id])
  try {
    const { data, error } = await useMyFetch(`/api/files/${file.id}/create_data_source`, {
      method: 'POST',
    });
    if (error.value || !data.value) {
      toast.add({ title: 'Error', description: error.value?.data?.detail || 'Failed to create data source', color: 'red' });
      return;
    }
    toast.add({ title: 'Data source created', description: `"${data.value.data_source_name}" is ready to query`, color: 'green' });
    router.push('/');
  } catch (e: any) {
    toast.add({ title: 'Error', description: e.message || 'Failed to create data source', color: 'red' });
  } finally {
    const s = new Set(loadingFileIds.value)
    s.delete(file.id)
    loadingFileIds.value = s
  }
}

const downloadFile = async (file: any) => {
  const response = await useMyFetch(`/api/files/${file.path}`, {
    method: 'GET',
  })
}

onMounted(() => {
  getFiles();
})
</script>
