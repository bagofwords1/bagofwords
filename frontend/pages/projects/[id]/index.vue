<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-5xl px-4 ps-0 py-2">
            <!-- Header -->
            <div v-if="project" class="mt-2">
                <div class="flex items-start justify-between gap-4">
                    <div class="flex items-center gap-3 min-w-0">
                        <div class="flex items-center justify-center w-10 h-10 rounded-xl shrink-0"
                             :style="project.color ? { backgroundColor: project.color + '1a' } : undefined"
                             :class="!project.color ? 'bg-gray-100 dark:bg-gray-800' : ''">
                            <UIcon name="i-heroicons-folder" class="w-5 h-5" :style="project.color ? { color: project.color } : undefined" :class="!project.color ? 'text-gray-400' : ''" />
                        </div>
                        <div class="min-w-0">
                            <h1 class="text-lg font-semibold text-gray-900 dark:text-white truncate">{{ project.name }}</h1>
                            <p class="text-[13px] text-gray-500 dark:text-gray-400 truncate">
                                <template v-if="project.description">{{ project.description }}</template>
                                <template v-else>{{ $t('projects.reportCount', { count: project.report_count }, project.report_count) }}</template>
                                <span v-if="isShared" class="inline-flex items-center gap-1 ms-2 text-gray-400">
                                    <UIcon name="i-heroicons-user-group" class="w-3.5 h-3.5" />{{ $t('projects.sharedBadge') }}
                                </span>
                            </p>
                        </div>
                    </div>
                    <button
                        name="new-report-in-project"
                        @click="createReportInProject"
                        :disabled="creating"
                        class="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                    >
                        <Spinner v-if="creating" class="animate-spin w-4 h-4" />
                        <UIcon v-else name="i-heroicons-plus" class="w-4 h-4" />
                        {{ $t('nav.newReport') }}
                    </button>
                </div>

                <!-- Tabs -->
                <div class="border-b border-gray-200 dark:border-gray-700 mt-6 mb-4">
                    <nav class="-mb-px flex space-x-6">
                        <button
                            v-for="tab in tabs"
                            :key="tab.key"
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm"
                            :class="activeTab === tab.key
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                            @click="activeTab = tab.key"
                        >
                            {{ tab.label }}
                        </button>
                    </nav>
                </div>

                <!-- Reports tab -->
                <div v-if="activeTab === 'reports'">
                    <div v-if="loadingReports" class="space-y-2 mt-2">
                        <div v-for="i in 4" :key="i" class="h-11 rounded-md bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                    </div>
                    <ul v-else-if="reports.length" class="divide-y divide-gray-100 dark:divide-gray-800">
                        <li v-for="report in reports" :key="report.id">
                            <component
                                :is="canOpen(report) ? NuxtLinkComp : 'div'"
                                :to="canOpen(report) ? `/reports/${report.id}` : undefined"
                                class="flex items-center gap-3 px-2 py-2.5 rounded-md"
                                :class="canOpen(report) ? 'hover:bg-gray-50 dark:hover:bg-gray-800/60 cursor-pointer' : 'opacity-70'"
                            >
                                <UIcon :name="reportTypeIcon(report)" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                                <span class="flex-1 truncate text-[13px] text-gray-800 dark:text-gray-200">{{ report.title || $t('reports.untitled') }}</span>
                                <UIcon v-if="report.is_starred" name="i-heroicons-star-solid" class="w-3.5 h-3.5 shrink-0 text-amber-400" />
                                <UTooltip v-if="!canOpen(report)" :text="$t('projects.readOnlySoon')">
                                    <UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 shrink-0 text-gray-300 dark:text-gray-600" />
                                </UTooltip>
                                <span class="hidden sm:block text-[11px] text-gray-400 dark:text-gray-500 w-28 truncate text-end">{{ formatDate(report.last_activity_at || report.created_at) }}</span>
                                <UTooltip :text="report.user?.name || ''">
                                    <div class="flex items-center justify-center w-5 h-5 rounded-full bg-gray-200 dark:bg-gray-700 text-[9px] font-semibold text-gray-600 dark:text-gray-300 shrink-0">
                                        {{ (report.user?.name || '?').charAt(0).toUpperCase() }}
                                    </div>
                                </UTooltip>
                            </component>
                        </li>
                    </ul>
                    <div v-else class="mt-8 flex flex-col items-center text-center py-10 border border-dashed border-gray-200 dark:border-gray-700 rounded-xl">
                        <UIcon name="i-heroicons-folder-open" class="w-8 h-8 text-gray-300 dark:text-gray-600" />
                        <p class="mt-3 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('projects.emptyProject') }}</p>
                        <button
                            @click="createReportInProject"
                            :disabled="creating"
                            class="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                        >
                            <UIcon name="i-heroicons-plus" class="w-4 h-4" />{{ $t('nav.newReport') }}
                        </button>
                    </div>
                </div>

                <!-- Dashboards tab -->
                <div v-else-if="activeTab === 'dashboards'">
                    <div v-if="loadingDashboards" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-6 mt-2">
                        <div v-for="i in 4" :key="i" class="bg-gray-100 dark:bg-gray-800 rounded-xl overflow-hidden">
                            <div class="aspect-[4/3] bg-gray-200 dark:bg-gray-700 animate-pulse"></div>
                        </div>
                    </div>
                    <div v-else-if="dashboards.length" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-6">
                        <RecentReportCard
                            v-for="report in dashboards"
                            :key="report.id"
                            :report="report"
                            view-mode="org"
                            :is-owner="report.user?.id === (currentUser as any)?.id"
                        />
                    </div>
                    <div v-else class="mt-8 flex flex-col items-center text-center py-10 border border-dashed border-gray-200 dark:border-gray-700 rounded-xl">
                        <UIcon name="i-heroicons-chart-bar-square" class="w-8 h-8 text-gray-300 dark:text-gray-600" />
                        <p class="mt-3 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('projects.noDashboards') }}</p>
                    </div>
                </div>

                <!-- Settings tab -->
                <div v-else-if="activeTab === 'settings'" class="max-w-xl">
                    <div class="space-y-4">
                        <div>
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.name') }}</label>
                            <input v-model="editName" type="text" :disabled="!project.can_manage"
                                class="w-full h-9 px-3 text-[13px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 disabled:opacity-60" />
                        </div>
                        <div>
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.description') }}</label>
                            <textarea v-model="editDescription" rows="2" :disabled="!project.can_manage"
                                class="w-full px-3 py-2 text-[13px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 disabled:opacity-60"></textarea>
                        </div>
                        <div>
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.color') }}</label>
                            <div class="flex items-center gap-1.5">
                                <button v-for="c in colorSwatches" :key="c" type="button" :disabled="!project.can_manage"
                                    class="w-6 h-6 rounded-full border-2 transition-transform disabled:opacity-60"
                                    :class="editColor === c ? 'border-gray-900 dark:border-white scale-110' : 'border-transparent'"
                                    :style="{ backgroundColor: c }"
                                    @click="editColor = editColor === c ? null : c" />
                            </div>
                        </div>
                        <div v-if="project.can_manage" class="pt-1">
                            <button @click="saveSettings" :disabled="savingSettings || !editName.trim()"
                                class="px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50">
                                {{ savingSettings ? $t('common.loading') : $t('common.save') }}
                            </button>
                        </div>
                    </div>

                    <!-- Defaults placeholder: lands with the sharing/defaults phase -->
                    <div class="mt-8 p-4 rounded-xl border border-dashed border-gray-200 dark:border-gray-700">
                        <div class="flex items-center gap-2 text-[13px] font-medium text-gray-600 dark:text-gray-300">
                            <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-400" />{{ $t('projects.settings.defaultsTitle') }}
                        </div>
                        <p class="mt-1 text-[12px] text-gray-400 dark:text-gray-500">{{ $t('projects.settings.defaultsSoon') }}</p>
                    </div>

                    <!-- Danger zone -->
                    <div v-if="project.is_owner" class="mt-8 p-4 rounded-xl border border-red-100 dark:border-red-900/40">
                        <div class="flex items-center justify-between gap-4">
                            <div>
                                <div class="text-[13px] font-medium text-gray-800 dark:text-gray-200">{{ $t('projects.deleteTitle') }}</div>
                                <p class="mt-0.5 text-[12px] text-gray-400 dark:text-gray-500">{{ $t('projects.deleteBody') }}</p>
                            </div>
                            <button @click="confirmDeleteOpen = true"
                                class="shrink-0 px-3 py-1.5 text-[13px] rounded-md border border-red-200 dark:border-red-900 text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950/40">
                                {{ $t('common.delete') }}
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Loading / not found -->
            <div v-else-if="loadingProject" class="mt-6 space-y-3">
                <div class="h-10 w-64 rounded-md bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                <div class="h-40 rounded-xl bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
            </div>
            <div v-else class="mt-16 text-center text-gray-400 dark:text-gray-500 text-[13px]">
                {{ $t('projects.notFound') }}
            </div>
        </div>

        <UModal v-model="confirmDeleteOpen">
            <div class="p-4">
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('projects.deleteTitle') }}</h3>
                <p class="mt-2 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('projects.deleteBody') }}</p>
                <div class="flex justify-end gap-2 mt-4">
                    <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="confirmDeleteOpen = false">{{ $t('common.cancel') }}</button>
                    <button class="px-3 py-1.5 text-[13px] rounded-md bg-red-600 text-white hover:bg-red-700 disabled:opacity-50" :disabled="deleting" @click="doDelete">{{ $t('common.delete') }}</button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { NuxtLink } from '#components'
import Spinner from '~/components/Spinner.vue'
import RecentReportCard from '~/components/home/RecentReportCard.vue'

const NuxtLinkComp = NuxtLink

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const toast = useToast()
const { data: currentUser } = useAuth()
const { fetchProjects, updateProject, deleteProject } = useProjects()
const { selectedAgentObjects } = useAgent()

const projectId = computed(() => String(route.params.id))

const project = ref<any>(null)
const loadingProject = ref(true)
const reports = ref<any[]>([])
const loadingReports = ref(true)
const dashboards = ref<any[]>([])
const loadingDashboards = ref(false)
const dashboardsLoaded = ref(false)
const creating = ref(false)
const savingSettings = ref(false)
const confirmDeleteOpen = ref(false)
const deleting = ref(false)

const activeTab = ref<'reports' | 'dashboards' | 'settings'>('reports')
const tabs = computed(() => [
    { key: 'reports' as const, label: t('projects.tabs.reports') },
    { key: 'dashboards' as const, label: t('projects.tabs.dashboards') },
    { key: 'settings' as const, label: t('projects.tabs.settings') },
])

const isShared = computed(() =>
    project.value && (project.value.access === 'org' || (project.value.member_count || 0) > 0))

// Settings form state
const editName = ref('')
const editDescription = ref('')
const editColor = ref<string | null>(null)
const colorSwatches = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2', '#64748b']

const fetchProject = async () => {
    loadingProject.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}`, { method: 'GET' })
        if (resp?.status?.value === 'success' && resp.data?.value) {
            project.value = resp.data.value
            editName.value = project.value.name || ''
            editDescription.value = project.value.description || ''
            editColor.value = project.value.color || null
        } else {
            project.value = null
        }
    } catch {
        project.value = null
    } finally {
        loadingProject.value = false
    }
}

const fetchReports = async () => {
    loadingReports.value = true
    try {
        const resp: any = await useMyFetch('/reports', {
            method: 'GET',
            query: { filter: 'all', project_id: projectId.value, limit: 100, view: 'minimal' },
        })
        if (resp?.status?.value === 'success' && resp.data?.value?.reports) {
            reports.value = resp.data.value.reports
        }
    } catch {} finally {
        loadingReports.value = false
    }
}

const fetchDashboards = async () => {
    loadingDashboards.value = true
    try {
        const resp: any = await useMyFetch('/reports', {
            method: 'GET',
            query: { filter: 'all', project_id: projectId.value, has_artifacts: 'yes', limit: 50 },
        })
        if (resp?.status?.value === 'success' && resp.data?.value?.reports) {
            dashboards.value = resp.data.value.reports
            dashboardsLoaded.value = true
        }
    } catch {} finally {
        loadingDashboards.value = false
    }
}
watch(activeTab, (tab) => {
    if (tab === 'dashboards' && !dashboardsLoaded.value) fetchDashboards()
})

// Until per-report read-only viewing ships (sharing phase), only the report
// owner can open the full report page — other members see a locked row.
const canOpen = (report: any) => report.user?.id === (currentUser.value as any)?.id

const reportTypeIcon = (report: any): string => {
    const modes = report?.artifact_modes || []
    if (modes.includes('page')) return 'i-heroicons-chart-bar-square'
    if (modes.includes('slides')) return 'i-heroicons-presentation-chart-bar'
    return 'i-heroicons-chat-bubble-left-right'
}

const formatDate = (value: string | null) => {
    if (!value) return ''
    try {
        return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    } catch { return '' }
}

const createReportInProject = async () => {
    if (creating.value) return
    creating.value = true
    try {
        const dataSourceIds = selectedAgentObjects.value.map((a: any) => a.id)
        const resp: any = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: [],
                data_sources: dataSourceIds,
                project_id: projectId.value,
            }),
        })
        if (resp?.error?.value) throw resp.error.value
        const data = resp.data?.value as any
        await router.push(`/reports/${data.id}`)
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        creating.value = false
    }
}

const saveSettings = async () => {
    if (savingSettings.value) return
    savingSettings.value = true
    try {
        await updateProject(projectId.value, {
            name: editName.value.trim(),
            description: editDescription.value,
            color: editColor.value || '',
        })
        await fetchProject()
        toast.add({ title: t('projects.settings.saved'), color: 'green' })
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        savingSettings.value = false
    }
}

const doDelete = async () => {
    if (deleting.value) return
    deleting.value = true
    try {
        await deleteProject(projectId.value)
        confirmDeleteOpen.value = false
        await router.push('/')
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        deleting.value = false
    }
}

onMounted(async () => {
    await Promise.all([fetchProject(), fetchReports(), fetchProjects()])
})
watch(projectId, async () => {
    dashboardsLoaded.value = false
    activeTab.value = 'reports'
    await Promise.all([fetchProject(), fetchReports()])
})
</script>
