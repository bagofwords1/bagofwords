<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="flex flex-col max-h-[calc(100dvh-4rem)]">
            <div class="px-6 pt-5 pb-4 border-b border-gray-100 dark:border-gray-800 flex items-start justify-between gap-4">
                <div>
                    <h3 class="text-lg font-semibold text-gray-900 dark:text-white">{{ $t('agentCatalogs.title') }}</h3>
                    <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ $t('agentCatalogs.subtitle') }}</p>
                </div>
                <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" @click="isOpen = false" />
            </div>

            <div class="flex flex-1 min-h-0">
                <!-- Catalog list -->
                <div class="w-60 shrink-0 border-e border-gray-100 dark:border-gray-800 flex flex-col min-h-0">
                    <form class="p-3 flex items-center gap-1.5" @submit.prevent="createCatalog">
                        <UInput v-model="newName" :placeholder="$t('agentCatalogs.newPlaceholder')" size="xs" class="flex-1 min-w-0" />
                        <UButton type="submit" size="xs" color="blue" icon="i-heroicons-plus" :loading="creating" :disabled="!newName.trim()" />
                    </form>
                    <div class="flex-1 overflow-y-auto px-2 pb-3 space-y-0.5">
                        <div v-if="loading" class="px-2 py-3 text-xs text-gray-400">{{ $t('common.loading') }}</div>
                        <div v-else-if="!catalogs.length" class="px-2 py-3 text-xs text-gray-400">{{ $t('agentCatalogs.empty') }}</div>
                        <button
                            v-for="c in catalogs"
                            :key="c.id"
                            type="button"
                            class="w-full flex items-center gap-2 h-8 px-2 rounded-md text-sm text-start"
                            :class="selectedId === c.id ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/60'"
                            @click="selectCatalog(c.id)"
                        >
                            <UIcon name="i-heroicons-tag" class="w-4 h-4 text-gray-400 shrink-0" />
                            <span class="flex-1 min-w-0 truncate">{{ c.name }}</span>
                            <span class="text-xs tabular-nums text-gray-400">{{ c.agent_count }}</span>
                        </button>
                    </div>
                </div>

                <!-- Selected catalog -->
                <div class="flex-1 min-w-0 flex flex-col min-h-0">
                    <div v-if="!selected" class="flex-1 flex items-center justify-center p-8 text-sm text-gray-400 text-center">
                        {{ catalogs.length ? $t('agentCatalogs.pickOne') : $t('agentCatalogs.createFirst') }}
                    </div>
                    <template v-else>
                        <div class="p-4 space-y-3 border-b border-gray-100 dark:border-gray-800">
                            <div class="grid grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('agentCatalogs.name') }}</label>
                                    <UInput v-model="draft.name" size="sm" />
                                </div>
                                <div>
                                    <label class="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">{{ $t('agentCatalogs.description') }}</label>
                                    <UInput v-model="draft.description" :placeholder="$t('agentCatalogs.optional')" size="sm" />
                                </div>
                            </div>
                        </div>

                        <div class="px-4 pt-3 pb-2 flex items-center justify-between gap-2">
                            <span class="text-sm font-medium text-gray-900 dark:text-white">
                                {{ $t('agentCatalogs.agents') }}
                                <span class="ms-1 text-xs tabular-nums text-gray-400">{{ draft.agentIds.size }}</span>
                            </span>
                            <UInput
                                v-model="search"
                                :placeholder="$t('agentCatalogs.searchAgents')"
                                icon="i-heroicons-magnifying-glass"
                                size="xs"
                                class="w-56"
                            />
                        </div>

                        <div class="mx-4 mb-3 border rounded-md overflow-hidden flex flex-col min-h-0">
                            <label
                                class="flex items-center gap-2 px-3 h-9 border-b text-sm bg-gray-50 dark:bg-gray-900"
                                :class="visibleAgents.length ? 'cursor-pointer' : 'opacity-50'"
                            >
                                <UCheckbox
                                    :model-value="allVisibleSelected"
                                    :disabled="!visibleAgents.length"
                                    size="xs"
                                    @update:model-value="toggleAllVisible($event)"
                                />
                                <span class="font-medium">{{ $t('agentCatalogs.selectAllShown', { n: visibleAgents.length }) }}</span>
                            </label>
                            <div class="overflow-y-auto max-h-72">
                                <div v-if="!visibleAgents.length" class="px-3 py-4 text-sm text-center text-gray-400">
                                    {{ $t('agentCatalogs.noAgentsMatch') }}
                                </div>
                                <label
                                    v-for="a in visibleAgents"
                                    :key="a.id"
                                    class="flex items-center gap-2 px-3 h-9 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50"
                                >
                                    <UCheckbox
                                        :model-value="draft.agentIds.has(a.id)"
                                        size="xs"
                                        @update:model-value="toggleAgent(a.id, $event)"
                                    />
                                    <span class="min-w-0 truncate text-gray-800 dark:text-gray-200">{{ a.name }}</span>
                                    <span class="flex-1" />
                                    <!-- One catalog per agent: checking an agent that lives
                                         elsewhere moves it here on save. -->
                                    <span
                                        v-if="otherCatalogName(a)"
                                        class="text-xs shrink-0"
                                        :class="draft.agentIds.has(a.id) ? 'text-amber-600 dark:text-amber-400' : 'text-gray-400'"
                                    >
                                        {{ draft.agentIds.has(a.id) ? $t('agentCatalogs.movesFrom', { name: otherCatalogName(a) }) : $t('agentCatalogs.inCatalog', { name: otherCatalogName(a) }) }}
                                    </span>
                                </label>
                            </div>
                        </div>

                        <div class="mt-auto px-4 py-3 border-t border-gray-100 dark:border-gray-800 flex items-center justify-between gap-2">
                            <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash" :loading="deleting" @click="deleteCatalog">
                                {{ $t('agentCatalogs.delete') }}
                            </UButton>
                            <div class="flex items-center gap-2">
                                <UButton color="gray" variant="ghost" size="sm" :disabled="!dirty" @click="resetDraft">{{ $t('agentCatalogs.discard') }}</UButton>
                                <UButton color="blue" size="sm" :loading="saving" :disabled="!dirty || !draft.name.trim()" @click="saveCatalog">
                                    {{ $t('agentCatalogs.save') }}
                                </UButton>
                            </div>
                        </div>
                    </template>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
// Full-admin tool for agent catalogs: create / rename / delete catalogs and
// choose which agents belong to each. Catalogs are organizational only — they
// label the agents-list filter and carry no access semantics.

interface Catalog {
    id: string
    name: string
    description?: string | null
    color?: string | null
    agent_count: number
}

interface AgentItem {
    id: string
    name: string
    catalog_id?: string | null
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void
    (e: 'changed'): void
}>()

const { t } = useI18n()
const toast = useToast()

const isOpen = computed({
    get: () => props.modelValue,
    set: (v: boolean) => emit('update:modelValue', v),
})

const catalogs = ref<Catalog[]>([])
const agents = ref<AgentItem[]>([])
const loading = ref(false)
const selectedId = ref<string | null>(null)
const newName = ref('')
const search = ref('')
const creating = ref(false)
const saving = ref(false)
const deleting = ref(false)

const draft = reactive({ name: '', description: '', agentIds: new Set<string>() })

const selected = computed(() => catalogs.value.find((c) => c.id === selectedId.value) || null)
const memberIds = (catalogId: string) => new Set(agents.value.filter((a) => a.catalog_id === catalogId).map((a) => a.id))

const sameSet = (a: Set<string>, b: Set<string>) => a.size === b.size && [...a].every((x) => b.has(x))

const dirty = computed(() => {
    const c = selected.value
    if (!c) return false
    return draft.name.trim() !== c.name
        || (draft.description || '') !== (c.description || '')
        || !sameSet(draft.agentIds, memberIds(c.id))
})

const visibleAgents = computed(() => {
    const q = search.value.trim().toLowerCase()
    return q ? agents.value.filter((a) => a.name.toLowerCase().includes(q)) : agents.value
})
const allVisibleSelected = computed(() =>
    visibleAgents.value.length > 0 && visibleAgents.value.every((a) => draft.agentIds.has(a.id))
)

function otherCatalogName(a: AgentItem) {
    if (!a.catalog_id || a.catalog_id === selectedId.value) return ''
    return catalogs.value.find((c) => c.id === a.catalog_id)?.name || ''
}

function errorDetail(error: any, fallback: string) {
    return error?.value?.data?.detail || fallback
}

async function load() {
    loading.value = true
    try {
        // show_all: an admin assigns catalogs across every agent in the org,
        // including private ones they are not a member of.
        const [cat, ds] = await Promise.all([
            useMyFetch<Catalog[]>('/agent_catalogs'),
            useMyFetch<any[]>('/data_sources/active', { query: { show_all: true, include_unconnected: true } }),
        ])
        catalogs.value = (cat.data.value || []) as Catalog[]
        agents.value = ((ds.data.value || []) as any[])
            .map((d) => ({ id: d.id, name: d.name, catalog_id: d.catalog_id }))
            .sort((a, b) => a.name.localeCompare(b.name))
        if (!selectedId.value || !catalogs.value.some((c) => c.id === selectedId.value)) {
            selectedId.value = catalogs.value[0]?.id || null
        }
        resetDraft()
    } finally {
        loading.value = false
    }
}

function resetDraft() {
    const c = selected.value
    draft.name = c?.name || ''
    draft.description = c?.description || ''
    draft.agentIds = c ? memberIds(c.id) : new Set()
}

function confirmDiscard() {
    return !dirty.value || confirm(t('agentCatalogs.confirmDiscard'))
}

function selectCatalog(id: string) {
    if (id === selectedId.value || !confirmDiscard()) return
    selectedId.value = id
    search.value = ''
    resetDraft()
}

function toggleAgent(id: string, checked: boolean) {
    const next = new Set(draft.agentIds)
    if (checked) next.add(id)
    else next.delete(id)
    draft.agentIds = next
}

function toggleAllVisible(checked: boolean) {
    const next = new Set(draft.agentIds)
    for (const a of visibleAgents.value) {
        if (checked) next.add(a.id)
        else next.delete(a.id)
    }
    draft.agentIds = next
}

async function createCatalog() {
    const name = newName.value.trim()
    if (!name || !confirmDiscard()) return
    creating.value = true
    try {
        const { data, error } = await useMyFetch<Catalog>('/agent_catalogs', { method: 'POST', body: { name } })
        if (error.value) {
            toast.add({ title: errorDetail(error, t('agentCatalogs.failed')), color: 'red' })
            return
        }
        newName.value = ''
        selectedId.value = (data.value as Catalog).id
        await load()
        emit('changed')
    } finally {
        creating.value = false
    }
}

async function saveCatalog() {
    const c = selected.value
    if (!c) return
    saving.value = true
    try {
        const name = draft.name.trim()
        if (name !== c.name || (draft.description || '') !== (c.description || '')) {
            const { error } = await useMyFetch(`/agent_catalogs/${c.id}`, {
                method: 'PUT',
                body: { name, description: draft.description || null },
            })
            if (error.value) {
                toast.add({ title: errorDetail(error, t('agentCatalogs.failed')), color: 'red' })
                return
            }
        }
        if (!sameSet(draft.agentIds, memberIds(c.id))) {
            const { error } = await useMyFetch(`/agent_catalogs/${c.id}/agents`, {
                method: 'PUT',
                body: { data_source_ids: [...draft.agentIds] },
            })
            if (error.value) {
                toast.add({ title: errorDetail(error, t('agentCatalogs.failed')), color: 'red' })
                return
            }
        }
        toast.add({ title: t('agentCatalogs.saved') })
        await load()
        emit('changed')
    } finally {
        saving.value = false
    }
}

async function deleteCatalog() {
    const c = selected.value
    if (!c || !confirm(t('agentCatalogs.confirmDelete', { name: c.name }))) return
    deleting.value = true
    try {
        const { error } = await useMyFetch(`/agent_catalogs/${c.id}`, { method: 'DELETE' })
        if (error.value) {
            toast.add({ title: errorDetail(error, t('agentCatalogs.failed')), color: 'red' })
            return
        }
        selectedId.value = null
        await load()
        emit('changed')
    } finally {
        deleting.value = false
    }
}

watch(isOpen, (open) => {
    if (open) {
        search.value = ''
        newName.value = ''
        load()
    }
})
</script>
