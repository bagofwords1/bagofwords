<template>
    <div class="flex pl-2 md:pl-4 text-sm">
        <div class="w-full md:w-3/4 px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    {{ memory.title }}
                </h1>
                <p class="mt-2 text-gray-500">
                    {{ memory.description }}
                </p>
                <p class="mt-2 text-gray-500 mb-8">
                    Last updated: {{ new Date(widget?.last_step?.created_at).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) }}
                </p>
                <p>
                    <button class="bg-blue-500 text-white px-2 py-1 rounded-md mb-4" @click="refreshMemory">Refresh Data</button>
                </p>
            </div>
            <div>
                <h2 class="text-sm font-semibold cursor-pointer hover:text-gray-600 hover:bg-gray-50 rounded-md px-1 py-0.5 mb-2" @click="toggleDataModel">
                    <Icon :name="showDataModel ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" />
                    Data Model
                </h2>
                <table v-if="showDataModel" class="border-collapse w-full">
                    <tr v-for="column in widget?.last_step?.data_model?.columns">
                        <th class="border border-gray-200 px-2 py-1">
                            {{ column.generated_column_name }}
                        </th>
                        <td class="border border-gray-200 px-2 py-1">
                            {{ column.description }}
                        </td>
                    </tr>
                </table>
            </div>

            <div>
                <h2 class="text-sm font-semibold cursor-pointer hover:text-gray-600 hover:bg-gray-50 rounded-md px-1 py-0.5 mt-4 mb-2" @click="toggleData">
                    <Icon :name="showData ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" />
                    Data Explorer
                </h2>
                <div v-if="showData" class="h-[500px]">
                    <RenderTable :widget="widget" :step="widget.last_step" />
                </div>
            </div>

            <div>
                <h2 class="text-sm font-semibold cursor-pointer hover:text-gray-600 hover:bg-gray-50 rounded-md px-1 py-0.5 mt-4 mb-2">
                    Options
                </h2>
                <button class="bg-white text-red-500 hover:underline" @click="deleteMemory">Delete</button>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import AgGridComponent from '~/components/AgGridComponent.vue'
import RenderTable from '~/components/RenderTable.vue'

definePageMeta({ auth: true })

const route = useRoute()
const id = route.params.id

const memory = ref({})
const widget = ref({})
const isLoading = ref(true)

// Add reactive properties for toggling
const showDataModel = ref(true)
const showData = ref(true)

async function deleteMemory() {
    if (confirm('Are you sure you want to delete this memory?')) {
        const response = await useMyFetch(`/api/memories/${id}`, {
            method: 'DELETE',
        })
        if (response.data.value) {
            navigateTo('/memory')
        }
    }
}

async function getMemory() {
    const response = await useMyFetch(`/api/memories/${id}`, {
        method: 'GET',
    })
    memory.value = response.data.value
}

async function getWidgetByMemory() {
    const response = await useMyFetch(`/api/memories/${id}/widget`, {
        method: 'GET',
    })
    widget.value = response.data.value
}

async function refreshMemory() {
    const response = await useMyFetch(`/api/memories/${id}/refresh`, {
        method: 'POST',
    })
    await getMemory()
    await getWidgetByMemory()
}

// Add toggle functions
function toggleDataModel() {
    showDataModel.value = !showDataModel.value
}

function toggleData() {
    showData.value = !showData.value
}

onMounted(async () => {
    await nextTick(async () => {  
        await getMemory()
        await getWidgetByMemory()
        isLoading.value = false
    })
})

</script>
