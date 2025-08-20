<template>
    <div class="inline">
        <USelectMenu v-model="internalSelectedDataSources" :options="dataSources" option-attribute="name"
            trailingIcon="none" multiple @change="handleSelectionChange"
            class=" mr-2 inline-block border border-gray-200 rounded hover:bg-gray-200 text-gray-500 text-xs font-medium cursor-pointer rounded dark:bg-gray-700 dark:text-gray-300"
            :ui="{
                variant: 'none',
                color: 'gray',
                base: 'bg-white hover:bg-gray-50 text-gray-500 text-xs font-medium rounded px-1 py-1 cursor-pointer',
                leading: {
                    padding: {
                        sm: 'ps-1',
                    }
                },
                trailing: {
                    padding: {
                        sm: 'pe-2',
                    }
                }

            }" :uiMenu="{
                base: 'w-[230px]',
            }">
            <template #label>
                <span class="flex text-[11px] items-center h-1">
                    Data:
                </span>
                    <span v-for="ds in internalSelectedDataSources" :key="ds.id" class="flex items-center h-3">
                        <DataSourceIcon :type="ds.type" class="h-2.5" />
                </span>

            </template>
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
    const response = await useMyFetch('/data_sources/active', {
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
    nextTick(async () => {
        const { organization, ensureOrganization } = useOrganization()
        
        try {
            // Wait for organization to be available before making API calls
            await ensureOrganization()
            
            if (organization.value?.id) {
                getDataSources()
            } else {
                console.warn('DataSourceSelectorComponentExcel: Organization not available, skipping API calls')
            }
        } catch (error) {
            console.error('DataSourceSelectorComponentExcel: Error during initialization:', error)
        }
    })
})


</script>