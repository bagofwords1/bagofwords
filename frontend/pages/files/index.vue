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
                            <td class="px-6 py-4 whitespace-nowrap">
                                <button @click="downloadFile(file)" class="text-blue-500 hover:text-blue-700">
                                    <Icon name="heroicons-arrow-down-tray" class="w-5 h-5 text-gray-500 mr-2" />
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

const files = ref([]);

definePageMeta({ auth: true })

const getFiles = async () => {
  const response = await useMyFetch('/api/files', {
    method: 'GET',
    headers: {
        'Content-Type': 'application/json',
    },
  })
  files.value = response.data.value
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
