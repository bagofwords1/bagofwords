<template>
    <div class="py-6">
        <div v-if="fetchError" />
        <div v-else>
            <!-- Filter tabs + search -->
            <div class="flex items-center justify-between gap-3 mb-4">
                <div class="flex items-center gap-1 border-b border-gray-200">
                    <button
                        @click="filterType = 'published'"
                        :class="[
                            'px-3 py-2 text-xs font-medium border-b-2 transition-colors',
                            filterType === 'published'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:text-gray-700'
                        ]"
                    >
                        {{ $t('queries.published') }}
                    </button>
                    <button
                        @click="filterType = 'suggested'"
                        :class="[
                            'px-3 py-2 text-xs font-medium border-b-2 transition-colors',
                            filterType === 'suggested'
                                ? 'border-amber-500 text-amber-600'
                                : 'border-transparent text-gray-500 hover:text-gray-700'
                        ]"
                    >
                        {{ isAdmin ? $t('queries.draftSuggested') : $t('queries.myDrafts') }}
                        <span v-if="suggestedCount > 0" class="ms-1.5 px-1.5 py-0.5 rounded-full text-[10px] bg-amber-100 text-amber-700">{{ suggestedCount }}</span>
                    </button>
                </div>
                <input
                    v-model="q"
                    type="text"
                    :placeholder="$t('queries.searchPlaceholder')"
                    class="border border-gray-200 rounded-md px-3 py-1.5 text-xs w-52 focus:outline-none focus:ring-1 focus:ring-blue-200"
                />
            </div>

            <!-- Loading -->
            <div v-if="loading" class="text-xs text-gray-400 flex items-center gap-1.5 py-4">
                <Spinner class="w-3.5 h-3.5" />
                {{ $t('queries.loading') }}
            </div>

            <!-- Empty state -->
            <div v-else-if="filteredItems.length === 0" class="flex flex-col items-center justify-center py-16">
                <div class="w-14 h-14 rounded-full bg-gray-50 flex items-center justify-center mb-3">
                    <Icon
                        :name="filterType === 'suggested' ? 'heroicons:light-bulb' : 'heroicons:cube'"
                        class="w-7 h-7 text-gray-300"
                    />
                </div>
                <p class="text-sm text-gray-500">
                    {{ filterType === 'suggested' ? $t('queries.noDrafts') : $t('queries.noPublished') }}
                </p>
            </div>

            <!-- List -->
            <div v-else class="space-y-2">
                <div
                    v-for="item in filteredItems"
                    :key="item.id"
                    class="border border-gray-100 bg-white rounded-lg p-4 hover:shadow-sm hover:border-gray-200 transition-all cursor-pointer"
                    @click="navigateToEntity(item.id)"
                >
                    <div class="flex items-start gap-3">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-2 mb-1">
                                <span
                                    class="text-[10px] px-1.5 py-0.5 rounded border"
                                    :class="item.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                                >{{ (item.type || '').toUpperCase() }}</span>
                                <Icon v-if="getEntityType(item) === 'global'" name="heroicons:check-badge" class="w-4 h-4 text-green-600" title="Approved" />
                                <span v-if="getEntityType(item) === 'archived'" class="text-[10px] px-1.5 py-0.5 rounded border text-red-700 border-red-200 bg-red-50">{{ $t('queries.archivedBadge') }}</span>
                                <span v-else-if="getEntityType(item) === 'draft'" class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50">{{ $t('queries.draftBadge') }}</span>
                                <span v-else-if="getEntityType(item) === 'private'" class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50">{{ $t('queries.draftBadge') }}</span>
                                <span v-else-if="getEntityType(item) === 'suggested'" class="text-[10px] px-1.5 py-0.5 rounded border text-amber-700 border-amber-200 bg-amber-50">{{ $t('queries.suggestedBadge') }}</span>
                                <span class="text-[11px] text-gray-400">{{ timeAgo(item.updated_at) }}</span>
                            </div>
                            <div class="text-sm font-medium text-gray-900 mb-0.5">{{ item.title || item.slug }}</div>
                            <div class="text-xs text-gray-500 line-clamp-2">{{ item.description || $t('queries.noDescription') }}</div>

                            <div class="flex items-center gap-3 mt-2">
                                <div v-if="item.data?.info?.total_rows !== undefined || item.data?.info?.total_columns !== undefined" class="flex items-center gap-3 text-[11px] text-gray-400">
                                    <span v-if="item.data?.info?.total_rows !== undefined" class="flex items-center gap-1">
                                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
                                        {{ formatCount(item.data.info.total_rows) }}
                                    </span>
                                    <span v-if="item.data?.info?.total_columns !== undefined" class="flex items-center gap-1">
                                        <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 4v16M15 4v16" /></svg>
                                        {{ formatCount(item.data.info.total_columns) }}
                                    </span>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Summary -->
            <div v-if="!loading && filteredItems.length > 0" class="mt-4 text-center text-[11px] text-gray-400">
                {{ summaryLabel }}
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import { useCan } from '~/composables/usePermissions'
import { useAuth } from '#imports'
import type { Ref } from 'vue'

definePageMeta({ auth: true, layout: 'data' })

const { t } = useI18n()
const router = useRouter()
const { data: authData } = useAuth()

const integration = inject<Ref<any>>('integration', ref(null))
const fetchError = inject<Ref<number | null>>('fetchError', ref(null))
const agentId = computed(() => integration.value?.id || '')

type MinimalDS = { id: string; name?: string; type?: string }
type EntityItem = {
    id: string
    type: string
    title: string
    slug: string
    description?: string | null
    updated_at: string
    data_sources?: MinimalDS[]
    data?: { info?: { total_rows?: number; total_columns?: number } }
    status?: string
    private_status?: string | null
    global_status?: string | null
    owner_id?: string
}

const allItems = ref<EntityItem[]>([])
const loading = ref(false)
const q = ref('')
const filterType = ref<'published' | 'suggested'>('published')
const isAdmin = computed(() => useCan('update_entities'))
const currentUserId = computed(() => (authData.value as any)?.user?.id)

const suggestedCount = computed(() =>
    allItems.value.filter(item => {
        const type = getEntityType(item)
        return (type === 'private' || type === 'suggested' || type === 'draft') && !isArchived(item)
    }).length
)

const filteredItems = computed(() => {
    let filtered = allItems.value.filter(item => !isArchived(item))
    if (filterType.value === 'published') {
        filtered = filtered.filter(item => getEntityType(item) === 'global')
    } else {
        filtered = filtered.filter(item => {
            const type = getEntityType(item)
            return type === 'private' || type === 'suggested' || type === 'draft'
        })
        if (!isAdmin.value) {
            filtered = filtered.filter(item => item.owner_id === currentUserId.value)
        }
    }
    if (q.value) {
        const s = q.value.toLowerCase()
        filtered = filtered.filter(item =>
            item.title?.toLowerCase().includes(s) ||
            item.slug?.toLowerCase().includes(s) ||
            item.description?.toLowerCase().includes(s)
        )
    }
    return filtered
})

const summaryLabel = computed(() => {
    const count = filteredItems.value.length
    if (filterType.value === 'suggested') {
        return t(count === 1 ? 'queries.showingDraftsOne' : 'queries.showingDraftsMany', { count })
    }
    return t(count === 1 ? 'queries.showingPublishedOne' : 'queries.showingPublishedMany', { count })
})

function isArchived(item: EntityItem) {
    return item.status === 'archived' || item.private_status === 'archived'
}

function getEntityType(item: EntityItem): string {
    if (isArchived(item)) return 'archived'
    if (item.private_status && !item.global_status) return 'private'
    if (item.private_status && item.global_status === 'suggested') return 'suggested'
    if (!item.private_status && item.global_status === 'approved') {
        if (item.status === 'published') return 'global'
        if (item.status === 'draft') return 'draft'
    }
    return 'unknown'
}

function navigateToEntity(id: string) {
    router.push(`/queries/${id}`)
}

function timeAgo(iso: string | Date | null | undefined) {
    if (!iso) return '—'
    const d = typeof iso === 'string' ? new Date(iso) : iso
    const diff = Math.max(0, Date.now() - (d?.getTime?.() || 0))
    const mins = Math.floor(diff / 60000)
    if (mins < 60) return t('queries.timeMinutesAgo', { n: mins })
    const hrs = Math.floor(mins / 60)
    if (hrs < 24) return t('queries.timeHoursAgo', { n: hrs })
    return t('queries.timeDaysAgo', { n: Math.floor(hrs / 24) })
}

function formatCount(num?: number): string {
    if (num == null) return '—'
    if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`
    if (num >= 1_000) return `${(num / 1_000).toFixed(1)}K`
    return String(num)
}

async function loadEntities() {
    if (!agentId.value) return
    loading.value = true
    try {
        const { data, error } = await useMyFetch(`/api/entities?data_source_ids=${agentId.value}`, { method: 'GET' })
        if (error.value) throw error.value
        allItems.value = (data.value as any) || []
    } catch {
        allItems.value = []
    } finally {
        loading.value = false
    }
}

watch(agentId, (id) => { if (id) loadEntities() }, { immediate: true })
</script>

<style scoped>
.line-clamp-2 {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
</style>
