<template>

    <header class="sticky top-0 bg-white dark:bg-gray-900 z-10 flex flex-col border-gray-200 dark:border-gray-700">
        <!-- Top row: back, title, share, dashboard toggle -->
        <div class="flex flex-row pt-1 h-[40px] pb-1 pe-2 items-center">
            <GoBackChevron />
            <UTooltip v-if="report" :text="report.is_starred ? t('reports.tooltips.unstar') : t('reports.tooltips.star')">
                <button @click="toggleStar" class="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 focus:outline-none">
                    <UIcon
                        :name="report.is_starred ? 'heroicons-star-solid' : 'heroicons-star'"
                        class="w-5 h-5 transition-colors"
                        :class="report.is_starred ? 'text-yellow-400 hover:text-yellow-500' : 'text-gray-400 hover:text-gray-500'"
                    />
                </button>
            </UTooltip>
            <h1 class="text-sm md:text-start text-center w-[500px]">
                <span class="font-semibold text-sm">
                    <!-- While the generated title types itself in, the input is
                         swapped for a matching span so the caret can trail the
                         text (an input's own caret sits wherever the cursor is,
                         and focusing it would fight the user). Identical
                         typography, so there is no reflow on the swap. -->
                    <span
                        v-if="report && isTypingTitle"
                        class="report-title-typing inline-block p-1 pt-1 text-start w-full text-gray-900 dark:text-gray-100 truncate cursor-text"
                        aria-live="polite"
                        @click="onTitleFocus"
                    >{{ localTitle }}<span class="report-title-caret" aria-hidden="true"></span></span>
                    <input
                        type="text"
                        class="inline bg-transparent hover:bg-gray-100 dark:hover:bg-gray-800 focus:bg-gray-100 dark:focus:bg-gray-800 p-1 pt-1 rounded outline-none active:bg-gray-100 dark:active:bg-gray-800 hover:cursor-pointer text-start w-full text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 transition-colors duration-150"
                        v-else-if="report"
                        v-model="localTitle"
                        :class="{ 'report-title-settled': justTyped }"
                        :disabled="isSaving"
                        @focus="onTitleFocus"
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
                    : 'px-2.5 text-gray-400 hover:text-gray-600'"
                :aria-label="tab.label"
                :title="tab.label"
            >
                <Icon :name="tab.icon" class="w-4 h-4 flex-shrink-0" />
                <span v-if="mobileView === tab.value">{{ tab.label }}</span>
            </button>
            <button
                v-if="mobileView !== 'chat'"
                @click="$emit('update:mobileView', 'chat')"
                class="ms-auto p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-400 hover:text-gray-600 transition-colors"
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

// The server generates the title from the first prompt and streams it in
// mid-run (report.title.updated → pages/reports/[id] patches `report`), so the
// placeholder is replaced under the user's eyes. Type it in instead of swapping
// the text: it reads as the assistant naming the report, and it makes clear the
// field is still editable. Plain assignment for every other case (first load, a
// manual rename, a title that arrived before this mounted).
const PLACEHOLDER_TITLES = ['', 'untitled report']
const isTypingTitle = ref(false)
// Brief afterglow on the real input once typing hands back, so the transition
// from the animated span to the editable field isn't a hard cut.
const justTyped = ref(false)
let typeTimer: ReturnType<typeof setTimeout> | null = null
let settleTimer: ReturnType<typeof setTimeout> | null = null

function stopTyping(finalTitle?: string) {
    if (typeTimer) { clearTimeout(typeTimer); typeTimer = null }
    isTypingTitle.value = false
    if (typeof finalTitle === 'string') localTitle.value = finalTitle
}

function prefersReducedMotion() {
    return typeof window !== 'undefined'
        && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

function typeTitle(title: string) {
    stopTyping()
    // Typing is motion too — CSS can't opt this one out, so honor the setting
    // here and just show the finished title.
    if (prefersReducedMotion()) { localTitle.value = title; return }
    isTypingTitle.value = true
    localTitle.value = ''
    // ~28ms/char lands a five-word title in well under a second; the floor
    // keeps a long title from dragging.
    const step = Math.max(12, Math.min(28, Math.round(700 / Math.max(title.length, 1))))
    const tick = (i: number) => {
        localTitle.value = title.slice(0, i)
        if (i >= title.length) {
            stopTyping(title)
            justTyped.value = true
            if (settleTimer) clearTimeout(settleTimer)
            settleTimer = setTimeout(() => { justTyped.value = false }, 900)
            return
        }
        typeTimer = setTimeout(() => tick(i + 1), step)
    }
    tick(1)
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

// A click into the field mid-animation hands control back immediately.
function onTitleFocus() {
    if (isTypingTitle.value) stopTyping(props.report?.title || localTitle.value)
}

onBeforeUnmount(() => {
    stopTyping()
    if (settleTimer) clearTimeout(settleTimer)
})

async function saveReportTitle() {
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
/* Title reveal — the generated title types itself into the header while the
   run is still streaming. Kept deliberately quiet: a trailing caret, a short
   fade-up per swap, and one settle glow when the editable input comes back. */
.report-title-typing {
    animation: report-title-rise 0.28s ease-out;
}

.report-title-caret {
    display: inline-block;
    width: 1.5px;
    height: 0.95em;
    margin-inline-start: 2px;
    vertical-align: -0.12em;
    background: currentColor;
    opacity: 0.75;
    animation: report-title-blink 0.9s steps(1, end) infinite;
}

.report-title-settled {
    animation: report-title-settle 0.9s ease-out;
}

@keyframes report-title-rise {
    from { opacity: 0.35; transform: translateY(2px); }
    to { opacity: 1; transform: none; }
}

@keyframes report-title-blink {
    0%, 55% { opacity: 0.75; }
    56%, 100% { opacity: 0; }
}

/* A soft wash that fades out — reads as "this was just written", without
   moving the text the user may be about to click into. */
@keyframes report-title-settle {
    0% { background-color: rgba(59, 130, 246, 0.14); }
    100% { background-color: transparent; }
}

@media (prefers-reduced-motion: reduce) {
    .report-title-typing,
    .report-title-settled { animation: none; }
    .report-title-caret { animation: none; opacity: 0; }
}
</style>
