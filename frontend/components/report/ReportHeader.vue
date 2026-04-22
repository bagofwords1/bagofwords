<template>

    <header class="sticky top-0 bg-white z-10 flex flex-col border-gray-200">
        <!-- Top row: back, title, share, dashboard toggle -->
        <div class="flex flex-row pt-1 h-[40px] pb-1 pe-2 items-center">
            <GoBackChevron />
            <h1 class="text-sm md:text-start text-center w-[500px]">
                <span class="font-semibold text-sm">
                    <input
                        type="text"
                        class="inline hover:bg-gray-100 p-1 pt-1 outline-none active:bg-gray-100 hover:cursor-pointer text-start w-full transition-all duration-300 ease-in-out transform motion-safe:hover:scale-[1.01]"
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
            <div class="ms-auto flex items-center gap-2">
                <ShareModal v-if="report" :report="report" share-type="conversation" title="Share Conversation" />
                <UTooltip :text="isSplitScreen ? t('reportView.closeSidebar') : t('reportView.openSidebar')">
                    <button @click="$emit('toggleSplitScreen')" class="hidden md:flex p-1.5 rounded hover:bg-gray-100 items-center gap-1.5">
                        <Icon name="heroicons:view-columns" class="w-5 h-5 text-gray-500" />
                        <span v-if="!isSplitScreen" class="text-xs text-gray-500">{{ t('reportView.sidebar') }}</span>
                    </button>
                </UTooltip>
            </div>
        </div>
        <!-- Mobile tabs -->
        <div v-if="isMobile" class="flex items-center gap-1 px-2 pb-1.5 border-b border-gray-100">
            <button
                v-for="tab in mobileTabs"
                :key="tab.value"
                @click="$emit('update:mobileView', tab.value)"
                class="flex items-center gap-1 px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors"
                :class="mobileView === tab.value
                    ? 'text-gray-900 bg-gray-100'
                    : 'text-gray-400 hover:text-gray-600'"
            >
                <Icon :name="tab.icon" class="w-3 h-3" />
                {{ tab.label }}
            </button>
            <button
                v-if="mobileView !== 'chat'"
                @click="$emit('update:mobileView', 'chat')"
                class="ms-auto p-1 rounded hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition-colors"
            >
                <Icon name="heroicons:x-mark" class="w-4 h-4" />
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
    isMobile?: boolean,
    mobileView?: string,
}>()

defineEmits(['toggleSplitScreen', 'stop', 'update:mobileView'])

const mobileTabs = computed(() => [
    { value: 'chat', label: t('reportView.tabChat'), icon: 'heroicons:chat-bubble-left-right' },
    { value: 'summary', label: t('reportView.tabSummary'), icon: 'heroicons:queue-list' },
    { value: 'dashboard', label: t('reportView.tabDashboard'), icon: 'heroicons:chart-bar-square' },
    { value: 'agent', label: t('reportView.tabAgent'), icon: 'heroicons:cog-6-tooth' },
])

const { t } = useI18n()
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


