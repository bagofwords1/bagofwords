<template>
    <!-- The Dashboard tab of a conversation with no dashboard yet: make a new
         one here, or edit one you own or may edit. Editing opens your own
         conversation for that dashboard (one per person per dashboard), not
         a copy in this one. -->
    <div class="w-full max-w-2xl px-4 sm:px-6" data-testid="dashboard-start">
        <div class="text-center mb-6">
            <div class="w-10 h-10 mx-auto mb-3 rounded-full bg-gray-100 dark:bg-gray-800 flex items-center justify-center">
                <Icon name="heroicons:squares-2x2" class="w-5 h-5 text-gray-500 dark:text-gray-400" />
            </div>
            <h3 class="text-base font-medium text-gray-900 dark:text-white">{{ $t('dashboardStart.title') }}</h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ $t('dashboardStart.subtitle') }}</p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            <!-- New -->
            <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-4 flex flex-col" data-testid="dashboard-start-new">
                <div class="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-white">
                    <Icon name="heroicons:sparkles" class="w-4 h-4 text-blue-600" />
                    {{ $t('dashboardStart.newTitle') }}
                </div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-4 flex-1">
                    {{ chartCount > 0 ? $t('dashboardStart.newDescCharts', chartCount) : $t('dashboardStart.newDescEmpty') }}
                </p>
                <button v-if="chartCount > 0" type="button" data-testid="dashboard-start-generate"
                    class="self-start inline-flex items-center gap-1.5 h-8 px-3 text-xs font-medium rounded-full text-white bg-blue-600 hover:bg-blue-700"
                    @click="emit('generate')">
                    <Icon name="heroicons:bolt" class="w-3.5 h-3.5" />
                    {{ $t('dashboardStart.generate') }}
                </button>
                <button v-else type="button" data-testid="dashboard-start-chat"
                    class="self-start inline-flex items-center gap-1.5 h-8 px-3 text-xs font-medium rounded-full border border-gray-300 dark:border-gray-600 text-blue-700 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950"
                    @click="startInChat">
                    <Icon name="heroicons:chat-bubble-left-ellipsis" class="w-3.5 h-3.5" />
                    {{ $t('dashboardStart.startInChat') }}
                </button>
            </div>

            <!-- Existing -->
            <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-4 flex flex-col min-h-0" data-testid="dashboard-start-existing">
                <div class="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-white">
                    <Icon name="heroicons:pencil-square" class="w-4 h-4 text-gray-500" />
                    {{ $t('dashboardStart.existingTitle') }}
                </div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 mb-3">{{ $t('dashboardStart.existingDesc') }}</p>
                <UInput v-model="search" size="sm" icon="i-heroicons-magnifying-glass" :placeholder="$t('dashboardStart.search')"
                    data-testid="dashboard-start-search" class="mb-2" />
                <div class="max-h-56 overflow-y-auto -mx-1">
                    <div v-if="loading" class="flex items-center justify-center py-6"><Spinner class="w-4 h-4 text-gray-400" /></div>
                    <p v-else-if="!shown.length" class="text-xs text-gray-400 text-center py-6">{{ $t('dashboardStart.none') }}</p>
                    <button v-for="d in shown" v-else :key="d.id" type="button" :disabled="!!opening"
                        class="w-full flex items-center gap-2.5 px-1.5 py-1.5 rounded-lg text-start hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-60"
                        data-testid="dashboard-start-item" @click="open(d)">
                        <div class="w-12 h-8 shrink-0 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 overflow-hidden flex items-center justify-center">
                            <img v-if="d.thumbnail_url" :src="d.thumbnail_url" alt="" class="w-full h-full object-cover" />
                            <Icon v-else name="heroicons:squares-2x2" class="w-3.5 h-3.5 text-gray-400" />
                        </div>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ d.title || $t('dashboardStart.untitled') }}</div>
                            <div class="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                                {{ roleOf(d) === 'owner' ? $t('dashboardStart.owner') : $t('dashboardCollab.list.canEdit') }}<template v-if="roleOf(d) !== 'owner' && d.owner?.name"> · {{ $t('dashboardStart.byOwner', { name: d.owner.name }) }}</template>
                            </div>
                        </div>
                        <Spinner v-if="opening === d.id" class="w-3.5 h-3.5 text-gray-400 shrink-0" />
                        <Icon v-else name="heroicons:arrow-top-right-on-square" class="w-3.5 h-3.5 text-gray-300 shrink-0 rtl-flip" />
                    </button>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

defineProps<{ chartCount: number }>()
const emit = defineEmits<{ (e: 'generate'): void }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const items = ref<any[]>([])
const loading = ref(true)
const search = ref('')
const opening = ref<string | null>(null)

const roleOf = (d: any): 'owner' | 'edit' | 'view' => d.role || (d.is_owner ? 'owner' : 'view')

// Mine and shared with me, editable only, newest first, one row each.
async function load() {
    loading.value = true
    try {
        const pages = await Promise.all(['my', 'shared'].map(filter =>
            useMyFetch('/dashboards', { method: 'GET', query: { page: 1, limit: 50, filter } })))
        const seen = new Set<string>()
        items.value = pages.flatMap(p => ((p.data.value as any)?.dashboards || []) as any[])
            .filter(d => roleOf(d) !== 'view' && !seen.has(d.id) && seen.add(d.id))
            .sort((a, b) => new Date(b.updated_at || 0).getTime() - new Date(a.updated_at || 0).getTime())
    } catch {
        items.value = []
    } finally {
        loading.value = false
    }
}
onMounted(load)

const shown = computed(() => {
    const q = search.value.trim().toLowerCase()
    return q ? items.value.filter(d => (d.title || '').toLowerCase().includes(q)) : items.value
})

// Your conversation for it if you have one; else Edit makes one (as the
// Dashboards page's pencil does).
async function open(d: any) {
    if (opening.value) return
    if (d.report_id) return navigateTo(`/reports/${d.report_id}`)
    opening.value = d.id
    try {
        const { data, error } = await useMyFetch(`/dashboards/${encodeURIComponent(d.id)}/edit`, { method: 'POST' })
        const reportId = (data.value as any)?.report_id
        if (error.value || !reportId) {
            toast.add({ title: t('dashboardCollab.list.editFailed'), description: getErrorMessage(error.value), color: 'red' })
            return
        }
        await navigateTo(`/reports/${reportId}`)
    } finally {
        opening.value = null
    }
}

function startInChat() {
    window.dispatchEvent(new CustomEvent('prompt:prefill', { detail: { text: t('dashboardStart.promptSeed'), autoSubmit: false } }))
}
</script>
