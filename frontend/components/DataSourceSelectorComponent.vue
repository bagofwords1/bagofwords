<template>
    <div class="inline">
        <USelectMenu v-model="internalSelectedDataSources" :options="dataSources" option-attribute="name" trailingIcon="none"
            multiple
            @change="handleSelectionChange"
            class="mr-2 inline-block bg-gray-100 hover:bg-gray-200 text-gray-500 text-xs font-medium cursor-pointer rounded dark:bg-gray-700 dark:text-gray-300"
            :ui="{
                variant: 'none',
                color: 'gray',
                base: 'bg-gray-100 hover:bg-gray-200 text-gray-500 text-xs font-medium rounded px-4 py-4 cursor-pointer',
                leading: {
                    padding: {
                        sm: 'ps-3',
                    }
                },
                trailing: {
                    padding: {
                        sm: 'pe-3',
                    }
                }

            }" :uiMenu="{
                base: 'w-[230px]',
            }">
            <template #label>
                <span v-for="ds in internalSelectedDataSources" :key="ds.id" class="flex items-center h-6">
                    <DataSourceIcon :type="ds.type" class="h-3.5" />
                </span>

            </template>
            <!---
            <template #leading>
                <DataSourceIcon :type="selectDataSource?.type" class="w-4" v-if="selected_ds" />
            </template>
            -->
            <template #option="{ option }">
  <div class="flex items-center w-full">
    <DataSourceIcon :type="option?.type" class="w-4" />
    <span class="ml-2">{{ option.name }}</span>
  </div>
</template>
        </USelectMenu>
    </div>
</template>

<script lang="ts" setup>

const internalSelectedDataSources = ref([])
const dataSources = ref([])

const props = defineProps({
    internalSelectedDataSources: {
    type: Array,
    default: () => [],
  },
  reportId: {
    type: String,
    default: () => '',
  }
});

const emit = defineEmits(['update:selectedDataSources']);

 async function getDataSources() {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    }).then((response) => {
        dataSources.value = response.data.value
    })
    internalSelectedDataSources.value = dataSources.value
    handleSelectionChange()
    
}

function handleSelectionChange() {
  emit('update:selectedDataSources', internalSelectedDataSources.value);
}

onMounted(() => {
    nextTick(() => {
        getDataSources()
    })
})


</script>