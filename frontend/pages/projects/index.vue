<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-4xl px-4 ps-0 py-8">
            <!-- Header -->
            <div class="flex items-center justify-between gap-4 mb-6">
                <h1 class="text-xl font-semibold text-gray-900 dark:text-white">{{ $t('projects.title') }}</h1>
                <button
                    type="button"
                    name="new-project"
                    class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-3 py-1.5 transition-colors"
                    @click="createOpen = true"
                >
                    <UIcon name="i-heroicons-plus" class="w-4 h-4" />
                    {{ $t('projects.newProject') }}
                </button>
            </div>

            <!-- Toolbar: scope tabs, search, filters — same rule as /reports.
                 Everything here filters the one /projects payload client-side:
                 the endpoint returns every project the caller can see in a
                 single response, so there is nothing to re-fetch per keystroke. -->
            <div class="border-b border-gray-200 dark:border-gray-800 mb-3">
                <nav class="-mb-px flex items-center gap-6">
                    <button
                        type="button"
                        class="whitespace-nowrap border-b-2 py-2 px-1 text-sm transition-colors"
                        :class="activeFilter === 'my'
                            ? 'border-blue-500 text-blue-500'
                            : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                        @click="setActiveFilter('my')"
                    >
                        {{ $t('projects.myProjects') }}
                    </button>
                    <button
                        type="button"
                        class="whitespace-nowrap border-b-2 py-2 px-1 text-sm transition-colors"
                        :class="activeFilter === 'shared'
                            ? 'border-blue-500 text-blue-500'
                            : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                        @click="setActiveFilter('shared')"
                    >
                        {{ $t('projects.sharedWithMe') }}
                    </button>

                    <div class="ms-auto flex items-center gap-2 pb-1.5">
                        <!-- Search -->
                        <div class="relative w-40 sm:w-64">
                            <input
                                v-model="searchTerm"
                                type="text"
                                :placeholder="$t('projects.searchPlaceholder')"
                                data-testid="project-search"
                                class="w-full ps-8 pe-3 py-1.5 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent"
                            />
                            <UIcon
                                name="i-heroicons-magnifying-glass"
                                class="absolute start-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400"
                            />
                        </div>

                        <!-- Filters (My projects only, like /reports).
                             /reports offers type/status/schedule/agent/dashboard;
                             only the two that a project row actually carries in
                             this payload survive — access and report count.
                             The rest have no project equivalent to filter on. -->
                        <div v-if="activeFilter === 'my'" class="relative shrink-0" ref="filtersRef">
                            <UTooltip :text="$t('projects.filtersButton')">
                                <button
                                    type="button"
                                    @click="showFilters = !showFilters"
                                    class="relative inline-flex items-center justify-center w-8 h-8 rounded-lg border transition-colors"
                                    :class="showFilters || activeFilterCount > 0
                                        ? 'border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300'
                                        : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
                                >
                                    <UIcon name="i-heroicons-funnel" class="h-4 w-4" />
                                    <span
                                        v-if="activeFilterCount > 0"
                                        class="absolute -top-1 -end-1 inline-flex items-center justify-center min-w-[15px] h-[15px] px-1 text-[10px] font-semibold rounded-full bg-blue-600 text-white"
                                    >
                                        {{ activeFilterCount }}
                                    </span>
                                </button>
                            </UTooltip>

                            <!-- Filters popover -->
                            <div
                                v-if="showFilters"
                                class="absolute end-0 z-20 mt-2 w-[360px] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl shadow-lg p-4"
                            >
                                <div class="space-y-3">
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('projects.filters.accessLabel') }}</span>
                                        <USelectMenu
                                            :model-value="accessFilter"
                                            @update:model-value="setAccessFilter"
                                            :options="accessFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedAccessLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                    <div class="flex items-center justify-between gap-3">
                                        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('projects.filters.reportsLabel') }}</span>
                                        <USelectMenu
                                            :model-value="contentFilter"
                                            @update:model-value="setContentFilter"
                                            :options="contentFilterOptions"
                                            value-attribute="value"
                                            option-attribute="label"
                                            class="w-48"
                                        >
                                            <template #label>
                                                <span class="text-xs whitespace-nowrap">{{ selectedContentLabel }}</span>
                                            </template>
                                        </USelectMenu>
                                    </div>
                                </div>
                                <div v-if="activeFilterCount > 0" class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-800 flex justify-end">
                                    <button
                                        type="button"
                                        @click="clearFilters"
                                        class="text-xs font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300"
                                    >
                                        {{ $t('projects.filters.clear') }}
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </nav>
            </div>

            <!-- Directory list -->
            <div
                v-if="isLoading || pagedProjects.length"
                class="divide-y divide-gray-100 dark:divide-gray-800 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden"
            >
                <!-- Loading skeleton -->
                <template v-if="isLoading">
                    <div v-for="i in 5" :key="i" class="flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900">
                        <div class="w-9 h-9 rounded-lg bg-gray-100 dark:bg-gray-800 animate-pulse shrink-0"></div>
                        <div class="flex-1 space-y-2">
                            <div class="h-3 w-1/3 rounded bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                            <div class="h-2.5 w-1/5 rounded bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                        </div>
                    </div>
                </template>

                <!-- Project rows -->
                <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
                    <li
                        v-for="project in pagedProjects"
                        :key="project.id"
                        @click="goToProject(project)"
                        :data-testid="`project-row-${project.id}`"
                        class="group flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-colors cursor-pointer"
                    >
                        <!-- Folder avatar, tinted with the project accent -->
                        <span class="flex items-center justify-center w-9 h-9 rounded-lg bg-gray-50 dark:bg-gray-800 shrink-0">
                            <UIcon
                                name="i-heroicons-folder"
                                class="w-5 h-5"
                                :class="project.color ? '' : 'text-gray-400 dark:text-gray-500'"
                                :style="project.color ? { color: project.color } : undefined"
                            />
                        </span>

                        <!-- Title block -->
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5 flex-wrap">
                                <NuxtLink
                                    :to="`/projects/${project.id}`"
                                    @click.stop
                                    class="text-sm font-medium text-gray-900 dark:text-white hover:text-blue-600 truncate"
                                >
                                    {{ project.name }}
                                </NuxtLink>
                                <span
                                    v-if="isShared(project)"
                                    class="inline-flex items-center gap-1 rounded-full border border-gray-200 dark:border-gray-700 px-2 py-0.5 text-[11px] text-gray-400 dark:text-gray-500"
                                >
                                    <UIcon name="i-heroicons-user-group" class="w-3 h-3" />
                                    {{ $t('projects.sharedBadge') }}
                                </span>
                            </div>
                            <!-- Sub-label: reports, members, owner, description -->
                            <div class="mt-0.5 flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500 flex-wrap">
                                <span class="inline-flex items-center gap-1">
                                    <UIcon name="i-heroicons-document-text" class="h-3.5 w-3.5" />
                                    {{ $t('projects.reportCount', { count: project.report_count }, project.report_count) }}
                                </span>
                                <template v-if="project.member_count > 0">
                                    <span class="text-gray-300 dark:text-gray-600">·</span>
                                    <span class="inline-flex items-center gap-1">
                                        <UIcon name="i-heroicons-user-group" class="h-3.5 w-3.5" />
                                        {{ project.member_count }}
                                    </span>
                                </template>
                                <template v-if="!project.is_owner && (project as any).user?.name">
                                    <span class="text-gray-300 dark:text-gray-600">·</span>
                                    <span>{{ $t('projects.ownedBy', { name: (project as any).user.name }) }}</span>
                                </template>
                                <template v-if="project.description">
                                    <span class="text-gray-300 dark:text-gray-600">·</span>
                                    <span class="truncate max-w-[240px]">{{ project.description }}</span>
                                </template>
                            </div>
                        </div>

                        <!-- Right metadata: date -->
                        <div class="shrink-0 hidden sm:block text-xs text-gray-400 dark:text-gray-500">
                            {{ formatDate((project as any).created_at) }}
                        </div>

                        <!-- Navigation affordance (no row actions on this page) -->
                        <div class="shrink-0 w-6 flex justify-end">
                            <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 rtl-flip text-gray-300 dark:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity" />
                        </div>
                    </li>
                </ul>
            </div>

            <!-- Empty state -->
            <div v-else-if="loaded" class="text-center py-16 border border-dashed border-gray-200 dark:border-gray-800 rounded-xl">
                <UIcon name="i-heroicons-folder-plus" class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
                <p class="text-sm font-medium text-gray-900 dark:text-white">
                    {{ emptyTitle }}
                </p>
                <p class="text-sm text-gray-400 dark:text-gray-500" :class="canCreateFromEmpty ? 'mb-4' : ''">
                    {{ emptyHint }}
                </p>
                <!-- A shared project belongs to someone else and a filtered-out
                     list is not empty, so "New project" only shows where a new
                     project would actually land in the list being looked at. -->
                <button
                    v-if="canCreateFromEmpty"
                    type="button"
                    class="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                    @click="createOpen = true"
                >
                    <UIcon name="i-heroicons-plus" class="w-4 h-4" />
                    {{ $t('projects.newProject') }}
                </button>
            </div>

            <!-- Pagination. Mirrors /reports: the row shows up as soon as there
                 are more projects than the smallest page size, so raising the
                 size to 50 never hides the only way back to 10. -->
            <div
                v-if="!isLoading && pagedProjects.length && filteredProjects.length > rowsPerPageOptions[0]"
                class="mt-3 flex items-center justify-between gap-3 text-xs text-gray-400 dark:text-gray-500"
            >
                <div class="flex items-center gap-1.5">
                    <span class="whitespace-nowrap">{{ $t('projects.pagination.rowsPerPage') }}</span>
                    <USelectMenu
                        :model-value="rowsPerPage"
                        @update:model-value="setRowsPerPage"
                        :options="rowsPerPageOptions"
                        size="xs"
                        class="w-[72px]"
                    />
                </div>

                <div v-if="totalPages > 1" class="flex items-center gap-1">
                    <button
                        type="button"
                        class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-transparent"
                        :disabled="currentPage <= 1"
                        @click="changePage(currentPage - 1)"
                        :aria-label="$t('common.previous')"
                    >
                        <UIcon name="i-heroicons-chevron-left" class="w-3.5 h-3.5 rtl-flip" />
                    </button>
                    <span>{{ currentPage }} / {{ totalPages }}</span>
                    <button
                        type="button"
                        class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-transparent"
                        :disabled="currentPage >= totalPages"
                        @click="changePage(currentPage + 1)"
                        :aria-label="$t('common.next')"
                    >
                        <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 rtl-flip" />
                    </button>
                </div>
            </div>

            <!-- Create dialog (name only — everything else lives in settings) -->
            <UModal v-model="createOpen" :ui="{ width: 'sm:max-w-sm' }">
                <div class="p-5">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white mb-1">{{ $t('projects.createTitle') }}</h3>
                    <p class="text-xs text-gray-400 dark:text-gray-500 mb-3">{{ $t('projects.createHint') }}</p>
                    <input
                        v-model="createName"
                        type="text"
                        :placeholder="$t('projects.namePlaceholder')"
                        class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                        @keyup.enter="submitCreate"
                    />
                    <div class="flex justify-end gap-2 mt-4">
                        <button type="button" class="text-sm text-gray-500 dark:text-gray-400 px-3 py-1.5 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800" @click="createOpen = false">
                            {{ $t('common.cancel') }}
                        </button>
                        <button
                            type="button"
                            class="text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg disabled:opacity-50"
                            :disabled="!createName.trim() || createBusy"
                            @click="submitCreate"
                        >
                            {{ $t('projects.create') }}
                        </button>
                    </div>
                </div>
            </UModal>
        </div>
    </div>
</template>

<script setup lang="ts">
import { useProjects, type Project } from '~/composables/useProjects'

definePageMeta({ auth: true })

const { t } = useI18n()
useHead({ title: () => t('projects.title') })

const router = useRouter()
const toast = useToast()
const { projects, loaded, loading, fetchProjects, createProject } = useProjects()

const createOpen = ref(false)
const createName = ref('')
const createBusy = ref(false)

const activeFilter = ref<'my' | 'shared'>('my')
const searchTerm = ref('')
const accessFilter = ref<'all' | 'private' | 'org'>('all')
const contentFilter = ref<'all' | 'with' | 'empty'>('all')
const showFilters = ref(false)
const filtersRef = ref<HTMLElement | null>(null)
const currentPage = ref(1)
const rowsPerPageOptions = [10, 25, 50, 100]
const rowsPerPage = ref(rowsPerPageOptions[0])

// The list is only ever fetched once, so "loading" alone would flash the
// skeleton on every background refresh; gate it on never-loaded instead.
const isLoading = computed(() => loading.value && !loaded.value)

// A project is "shared" when it is not the caller's own: either someone
// granted it to them or it is open to the whole org. The same predicate
// splits the two tabs, so a project appears in exactly one of them.
const isShared = (project: Project) => !project.is_owner || project.member_count > 0 || project.access === 'org'

const scopedProjects = computed(() =>
    projects.value.filter(p => (activeFilter.value === 'shared' ? !p.is_owner : p.is_owner))
)

const filteredProjects = computed(() => {
    const term = searchTerm.value.trim().toLowerCase()
    return scopedProjects.value.filter(p => {
        if (term) {
            const haystack = `${p.name} ${p.description || ''}`.toLowerCase()
            if (!haystack.includes(term)) return false
        }
        // Filters are hidden on the shared tab (as on /reports), so they only
        // ever narrow the caller's own projects.
        if (activeFilter.value === 'my') {
            if (accessFilter.value === 'private' && p.access !== 'private') return false
            if (accessFilter.value === 'org' && p.access !== 'org') return false
            if (contentFilter.value === 'with' && !p.report_count) return false
            if (contentFilter.value === 'empty' && p.report_count > 0) return false
        }
        return true
    })
})

const totalPages = computed(() => Math.max(1, Math.ceil(filteredProjects.value.length / rowsPerPage.value)))

const pagedProjects = computed(() => {
    const start = (currentPage.value - 1) * rowsPerPage.value
    return filteredProjects.value.slice(start, start + rowsPerPage.value)
})

// Deleting or filtering can shrink the list under the current page, which
// would leave the user on an empty page with the pager hidden.
watch(totalPages, (pages) => {
    if (currentPage.value > pages) currentPage.value = pages
})

const hasNarrowedList = computed(() =>
    Boolean(searchTerm.value.trim()) || accessFilter.value !== 'all' || contentFilter.value !== 'all'
)

const canCreateFromEmpty = computed(() => activeFilter.value === 'my' && !hasNarrowedList.value)

const emptyTitle = computed(() => {
    if (hasNarrowedList.value) return t('projects.noResults')
    return activeFilter.value === 'shared' ? t('projects.sharedEmpty') : t('projects.emptyTitle')
})

const emptyHint = computed(() => {
    if (hasNarrowedList.value) return t('projects.noResultsHint')
    return activeFilter.value === 'shared' ? t('projects.sharedEmptyHint') : t('projects.moveNoProjects')
})

const accessFilterOptions = computed(() => [
    { value: 'all', label: t('projects.filters.allAccess') },
    { value: 'private', label: t('projects.filters.private') },
    { value: 'org', label: t('projects.filters.orgShared') },
])

const contentFilterOptions = computed(() => [
    { value: 'all', label: t('projects.filters.allReports') },
    { value: 'with', label: t('projects.filters.withReports') },
    { value: 'empty', label: t('projects.filters.emptyReports') },
])

const selectedAccessLabel = computed(
    () => accessFilterOptions.value.find(o => o.value === accessFilter.value)?.label || t('projects.filters.accessLabel')
)

const selectedContentLabel = computed(
    () => contentFilterOptions.value.find(o => o.value === contentFilter.value)?.label || t('projects.filters.reportsLabel')
)

const activeFilterCount = computed(() => {
    let count = 0
    if (accessFilter.value !== 'all') count++
    if (contentFilter.value !== 'all') count++
    return count
})

const _df = useFormatDate()
const formatDate = (iso?: string) => {
    if (!iso) return ''
    return _df.format(iso, { month: 'short', day: 'numeric', year: 'numeric' }) || ''
}

const goToProject = (project: Project) => {
    router.push(`/projects/${project.id}`)
}

const setActiveFilter = (filter: 'my' | 'shared') => {
    if (activeFilter.value === filter) return
    activeFilter.value = filter
    accessFilter.value = 'all'
    contentFilter.value = 'all'
    showFilters.value = false
    currentPage.value = 1
}

const setAccessFilter = (value: 'all' | 'private' | 'org') => {
    accessFilter.value = value
    currentPage.value = 1
}

const setContentFilter = (value: 'all' | 'with' | 'empty') => {
    contentFilter.value = value
    currentPage.value = 1
}

const clearFilters = () => {
    accessFilter.value = 'all'
    contentFilter.value = 'all'
    currentPage.value = 1
}

const changePage = (page: number) => {
    if (page < 1 || page > totalPages.value) return
    currentPage.value = page
}

// Same storage convention as /reports (`bow.` prefix); anything outside the
// offered set is ignored so a tampered value can't produce an empty page.
const ROWS_PER_PAGE_KEY = 'bow.projects.rowsPerPage'

const setRowsPerPage = (limit: number) => {
    if (rowsPerPage.value === limit) return
    rowsPerPage.value = limit
    currentPage.value = 1
    if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ROWS_PER_PAGE_KEY, String(limit))
    }
}

watch(searchTerm, () => { currentPage.value = 1 })

const submitCreate = async () => {
    const name = createName.value.trim()
    if (!name || createBusy.value) return
    createBusy.value = true
    try {
        const project: any = await createProject({ name })
        createOpen.value = false
        createName.value = ''
        if (project?.id) await router.push(`/projects/${project.id}`)
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        createBusy.value = false
    }
}

const onClickOutside = (e: MouseEvent) => {
    if (showFilters.value && filtersRef.value && !filtersRef.value.contains(e.target as Node)) {
        showFilters.value = false
    }
}

onMounted(() => {
    document.addEventListener('click', onClickOutside)
    if (typeof localStorage !== 'undefined') {
        const stored = Number(localStorage.getItem(ROWS_PER_PAGE_KEY))
        if (rowsPerPageOptions.includes(stored)) rowsPerPage.value = stored
    }
    fetchProjects()
})

onUnmounted(() => {
    document.removeEventListener('click', onClickOutside)
})
</script>
