<template>

    <header class="sticky top-0 bg-white dark:bg-gray-900 z-10 flex flex-col border-gray-200 dark:border-gray-700">
        <!-- Top row: back, title, share, dashboard toggle -->
        <div class="flex h-10 flex-row items-center py-1 pe-2">
            <GoBackChevron class="!flex h-8 w-8 shrink-0 items-center justify-center !p-0 leading-none" />
            <div v-if="report" class="report-star-slot h-8 shrink-0" :class="{ 'report-star-enter': isNewReport }">
            <UTooltip class="flex h-8 shrink-0 items-center" :text="report.is_starred ? t('reports.tooltips.unstar') : t('reports.tooltips.star')">
                <button @click="toggleStar" class="flex h-8 w-8 items-center justify-center rounded hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none">
                    <UIcon
                        :name="report.is_starred ? 'heroicons-star-solid' : 'heroicons-star'"
                        class="block w-5 h-5 transition-colors"
                        :class="report.is_starred ? 'text-yellow-400 hover:text-yellow-500' : 'text-ink-muted hover:text-gray-500'"
                    />
                </button>
            </UTooltip>
            </div>
            <h1 class="grid h-8 min-w-0 max-w-lg items-center text-start text-sm font-semibold">
                <!-- Size the field from the complete title, including while it types in. -->
                <span aria-hidden="true" class="invisible col-start-1 row-start-1 h-8 truncate px-1 py-0 text-sm font-semibold leading-8">{{ localTitle || t('reports.newReport') }}</span>
                <input
                    v-if="report"
                    type="text"
                    class="report-title col-start-1 row-start-1 block h-8 min-w-0 w-full cursor-pointer focus:cursor-text rounded bg-transparent px-1 py-0 text-sm font-semibold leading-8 text-ink outline-none hover:bg-surface-muted focus:bg-surface-muted placeholder:text-ink transition-colors"
                    :dir="dominantTextDir(localTitle) || 'ltr'"
                    :placeholder="t('reports.newReport')"
                    :class="{ 'header-control-enter': revealTitle }"
                    v-model="localTitle"
                    :disabled="isSaving"
                    @focus="onTitleFocus"
                    @keyup.enter="saveReportTitle"
                    @blur="saveReportTitle"
                    ref="reportTitleInput"
                />
                <span v-if="!report" class="col-start-1 row-start-1 h-8 truncate px-1 text-left text-sm font-semibold leading-8 text-ink">{{ t('reports.newReport') }}</span>
            </h1>
            <div v-if="report" class="header-control-enter ms-auto flex items-center gap-2">
                <ShareModal v-if="report" :report="report" share-type="conversation" :title="$t('share.shareConversation')" />
                <!-- Text size / width menu hidden for now; set v-if back to true to restore. -->
                <ChatDisplayMenu v-if="false" />
                <UTooltip :text="isSplitScreen ? t('reportView.closeSidebar') : t('reportView.openSidebar')">
                    <button @click="$emit('toggleSplitScreen')" class="hidden md:flex p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 items-center gap-1.5">
                        <Icon name="heroicons:view-columns" class="w-5 h-5 text-gray-500 dark:text-gray-400" />
                        <span v-if="!isSplitScreen" class="text-xs text-gray-500 dark:text-gray-400">{{ t('reportView.sidebar') }}</span>
                    </button>
                </UTooltip>
            </div>
        </div>
        <!-- Mobile tabs -->
        <div v-if="isMobile" class="flex items-center gap-1 px-2 pb-1.5 border-b border-gray-100 dark:border-gray-800">
            <button
                v-for="tab in mobileTabs"
                :key="tab.value"
                @click="$emit('update:mobileView', tab.value)"
                class="flex items-center gap-1.5 py-1.5 text-xs font-medium rounded-md transition-colors"
                :class="mobileView === tab.value
                    ? 'px-3 text-gray-900 dark:text-white bg-gray-100 dark:bg-gray-800'
                    : 'px-2.5 text-ink-muted hover:text-gray-600'"
                :aria-label="tab.label"
                :title="tab.label"
            >
                <Icon :name="tab.icon" class="w-4 h-4 flex-shrink-0" />
                <span v-if="mobileView === tab.value">{{ tab.label }}</span>
            </button>
            <button
                v-if="mobileView !== 'chat'"
                @click="$emit('update:mobileView', 'chat')"
                class="ms-auto p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-ink-muted hover:text-gray-600 transition-colors"
            >
                <Icon name="heroicons:x-mark" class="w-4 h-4" />
            </button>
        </div>
    </header>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { dominantTextDir } from '~/utils/textDirection'
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import ShareModal from '@/components/ShareModal.vue'
import ChatDisplayMenu from '@/components/report/ChatDisplayMenu.vue'

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
// Capture the creation handoff before the report consumes its prompt query.
const isNewReport = typeof route.query.new_message === 'string' 
const report_id = route.params.id
const reportTitleInput = ref<HTMLInputElement | null>(null)
const localTitle = ref('')
const isSaving = ref(false)
const toast = useToast()

// Keep the draft label until the generated title is ready. Reveal the full
// title with opacity only, so neither its direction nor its baseline moves.
const PLACEHOLDER_TITLES = ['', 'untitled report']
const revealTitle = ref(false)
let revealTimer: ReturnType<typeof setTimeout> | null = null

function stopTyping(title: string) {
    localTitle.value = PLACEHOLDER_TITLES.includes(title.trim().toLowerCase()) ? '' : title
}

function typeTitle(title: string) {
    stopTyping(title)
    revealTitle.value = true
    if (revealTimer) clearTimeout(revealTimer)
    revealTimer = setTimeout(() => { revealTitle.value = false }, 180)
}

watch(() => props.report?.title, (newTitle, oldTitle) => {
    if (!newTitle) return
    const wasPlaceholder = PLACEHOLDER_TITLES.includes((oldTitle || '').trim().toLowerCase())
    const isGenerated = wasPlaceholder && newTitle !== oldTitle && oldTitle !== undefined
    // Never hijack the field while it is being edited, and never re-animate a
    // title the user just saved themselves.
    const isFocused = typeof document !== 'undefined'
        && document.activeElement === reportTitleInput.value
    if (isGenerated && !isFocused && !isSaving.value) {
        typeTitle(newTitle)
    } else {
        stopTyping(newTitle)
    }
}, { immediate: true })

function onTitleFocus() {
    revealTitle.value = false
}

onBeforeUnmount(() => {
    if (revealTimer) clearTimeout(revealTimer)
})

async function saveReportTitle() {
    if (!localTitle.value.trim() && PLACEHOLDER_TITLES.includes((props.report?.title || '').trim().toLowerCase())) return
    // Enter saves and then blurs, and the blur handler saves again — the two
    // fire close enough together that the second one starts before the first
    // PUT returns, so one edit meant two requests and two toasts. Drop any
    // save that arrives while one is in flight.
    if (isSaving.value) return
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

    // Nothing to save when the field still holds the stored title — e.g. the
    // blur that follows a completed save, or a click through the field.
    if (localTitle.value.trim() === (props.report.title || '')) {
        isSaving.value = false
        reportTitleInput.value?.blur()
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
            // Keep the sidebar's recent-reports list in step — it listens for
            // this (layouts/default.vue). No `generated` flag: a rename is the
            // user's own edit, so it swaps in without the reveal animation the
            // streamed title plays.
            try {
                window.dispatchEvent(new CustomEvent('report:updated', {
                    detail: { id: String(props.report.id ?? report_id), title: props.report.title }
                }))
            } catch {}
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

async function toggleStar() {
    if (!props.report) return
    const next = !props.report.is_starred
    // Optimistic update
    props.report.is_starred = next
    try {
        const response: any = await useMyFetch(`/reports/${props.report.id}/star`, {
            method: next ? 'POST' : 'DELETE',
        })
        if (response?.error?.value) {
            throw response.error.value
        }
    } catch (error: any) {
        // Revert on failure
        props.report.is_starred = !next
        console.error('Error toggling star', error)
        toast.add({
            title: t('reports.toasts.starFailed'),
            description: String(error?.data?.detail || error?.message || ''),
            color: 'red',
        })
    }
}
</script>



<style scoped>
/* Keep the title beside the header controls while shaping its content in
   its own direction. The same input remains mounted during title generation. */
.report-title {
    text-align: left;
}
.report-star-slot {
    inline-size: 2rem;
}
.report-star-enter {
    animation: report-star-enter 200ms ease-out both;
}
@keyframes report-star-enter {
    from { inline-size: 0; opacity: 0; overflow: hidden; }
    to { inline-size: 2rem; opacity: 1; overflow: hidden; }
}
.header-control-enter {
    animation: header-control-enter 180ms ease-out both;
}
@keyframes header-control-enter {
    from { opacity: 0; }
    to { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
    .header-control-enter, .report-star-enter { animation: none; }
}
</style>
