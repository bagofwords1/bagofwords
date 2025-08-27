<template>

    <header class="sticky top-0 bg-white z-10 flex flex-row pt-1 h-[40px] border-gray-200 pb-1 pr-2 items-center">
        <GoBackChevron />
        <h1 class="text-sm md:text-left text-center w-[500px]">
            <span class="font-semibold text-sm">
                <input 
                    type="text" 
                    class="inline hover:bg-gray-100 p-1 pt-1 outline-none active:bg-gray-100 hover:cursor-pointer text-left w-full transition-all duration-300 ease-in-out transform motion-safe:hover:scale-[1.01]" 
                    v-if="report"
                    v-model="localTitle" 
                    :disabled="isSaving"
                    @keyup.enter="saveReportTitle" 
                    @blur="saveReportTitle"
                    ref="reportTitleInput" 
                />
                <span v-else></span>
            </span>
        </h1>
        <div class="ml-auto flex items-center gap-1">
            <button @click="$emit('toggleSplitScreen')" class="p-1.5 rounded text-xl hover:bg-gray-100 flex items-center">
                <span class="inline-flex items-center">
                    <Icon name="heroicons:chart-pie" class="inline-block mr-2" />
                </span>
                <span class="text-sm" :class="isSplitScreen ? 'hidden' : 'inline'">Dashboard</span>
            </button>
        </div>
    </header>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import ShareModal from '@/components/ShareModal.vue'

const props = defineProps<{ 
    report: any | null,
    isSplitScreen: boolean,
    isStreaming: boolean,
}>()

defineEmits(['toggleSplitScreen', 'rerun', 'stop'])

const route = useRoute()
const report_id = route.params.id
const reportTitleInput = ref<HTMLInputElement | null>(null)
const localTitle = ref('')
const isSaving = ref(false)
const toast = useToast()

// Watch for changes in report prop to update local title
watch(() => props.report?.title, (newTitle) => {
    if (newTitle) {
        localTitle.value = newTitle
    }
}, { immediate: true })

async function saveReportTitle() {
    // disable submit button
    isSaving.value = true

    if (!props.report || !localTitle.value.trim()) {
        isSaving.value = false
        toast.add({
            title: 'Title is required',
            color: 'red',
        })
        return
    }
    
    const requestBody = {
        title: localTitle.value.trim()
    }

    try {
        await useMyFetch(`/api/reports/${report_id}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(requestBody),
        })
        
        // Update the report object
        if (props.report) {
            props.report.title = localTitle.value.trim()


        }
        
        // Blur the input
        if (reportTitleInput.value) {
            reportTitleInput.value.blur()
            toast.add({
                title: 'Report title updated',
                color: 'green',
            })
        }
        


    } catch (error) {
        console.error('Failed to save report title:', error)
        // Revert to original title on error
        if (props.report?.title) {
            localTitle.value = props.report.title
        }
        toast.add({
            title: 'Failed to update report title',
            color: 'red',
        })
    }
    isSaving.value = false
}
</script>


