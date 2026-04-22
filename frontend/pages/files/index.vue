<template>
    <div class="flex ps-2 md:ps-4 text-sm">
        <div class="w-full md:w-3/4 px-4 ps-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    {{ $t('files.title') }}
                </h1>
                <p class="mt-2 text-gray-500">{{ $t('files.subtitle') }}</p>

            </div>

            <div class="bg-white rounded-lg shadow mt-8">
<table class="min-w-full divide-y divide-gray-200">
  <thead class="bg-gray-50">
    <tr>
      <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('files.file') }}</th>
      <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('files.metadata') }}</th>
      <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('files.createdAt') }}</th>
      <th class="px-6 py-3 text-start text-xs font-medium text-gray-500 uppercase tracking-wider">{{ $t('files.actions') }}</th>
                    </tr>
                    </thead>

                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="file in files" :key="file.id">
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="flex items-center">
                                    <UIcon name="heroicons-document-text" class="w-5 h-5 text-gray-500 me-2" />
                                    {{ file.filename }}.
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div v-if="file.schemas.length > 0">
                                    <div v-for="schema in file.schemas" :key="schema.id">
                                        <UTooltip :text="Object.keys(schema.schema.fields).join(', ')">
                                            <div class="flex items-center">
                                                <Icon name="heroicons-view-columns" class="w-5 h-5 text-gray-500 me-2" />
                                                {{ $t(Object.keys(schema.schema.fields).length === 1 ? 'files.metadataFieldsOne' : 'files.metadataFieldsMany', { count: Object.keys(schema.schema.fields).length }) }}
                                            </div>
                                        </UTooltip>
                                    </div>
                                </div>
                                <div v-else-if="file.tags.length > 0">
                                     <UTooltip :text="file.tags.map(tag => tag.key).join(', ')">
                                         <div class="flex items-center">
                                            <Icon name="heroicons-view-columns" class="w-5 h-5 text-gray-500 me-2" />
                                            {{ $t(file.tags.length === 1 ? 'files.metadataTagsOne' : 'files.metadataTagsMany', { count: file.tags.length }) }}
                                        </div>
                                     </UTooltip>
                                </div>
                                <div v-else>
                                    {{ $t('files.noMetadata') }}
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">{{ file.created_at }}</td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <button @click="downloadFile(file)" class="text-blue-500 hover:text-blue-700">
                                    <Icon name="heroicons-arrow-down-tray" class="w-5 h-5 text-gray-500 me-2" />
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
