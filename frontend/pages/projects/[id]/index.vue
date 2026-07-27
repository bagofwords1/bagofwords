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
                    <div class="shrink-0 flex items-center gap-2">
                        <button
                            v-if="project.can_manage"
                            name="share-project"
                            @click="openShareModal"
                            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium rounded-md border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
                        >
                            <UIcon name="i-heroicons-user-plus" class="w-4 h-4" />
                            {{ $t('projects.share.button') }}
                            <span v-if="project.member_count" class="text-[11px] text-gray-400">{{ project.member_count }}</span>
                        </button>
                        <button
                            name="new-report-in-project"
                            @click="createReportInProject"
                            :disabled="creating"
                            class="inline-flex items-center gap-1.5 px-3 py-1.5 text-[13px] font-medium rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                        >
                            <Spinner v-if="creating" class="animate-spin w-4 h-4" />
                            <UIcon v-else name="i-heroicons-plus" class="w-4 h-4" />
                            {{ $t('nav.newReport') }}
                        </button>
                        <UTooltip :text="$t('projects.tabs.settings')">
                            <button
                                name="project-settings"
                                @click="view = view === 'settings' ? 'overview' : 'settings'"
                                class="inline-flex items-center justify-center w-8 h-8 rounded-md border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800"
                                :class="view === 'settings' ? 'text-gray-900 dark:text-white bg-gray-100 dark:bg-gray-800' : 'text-gray-500 dark:text-gray-400'"
                            >
                                <UIcon name="i-heroicons-cog-6-tooth" class="w-4 h-4" />
                            </button>
                        </UTooltip>
                    </div>
                </div>

                <!-- ── Overview: dashboards + reports + (agents / files / members) rail ── -->
                <div v-if="view === 'overview'" class="mt-6 flex items-start gap-8">
                    <div class="flex-1 min-w-0">
                        <!-- Dashboards strip (only when the project has any) -->
                        <div v-if="loadingDashboards || dashboards.length" class="mb-7">
                            <div class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-2">{{ $t('projects.tabs.dashboards') }}</div>
                            <div v-if="loadingDashboards" class="grid grid-cols-2 md:grid-cols-3 gap-4">
                                <div v-for="i in 3" :key="i" class="bg-gray-100 dark:bg-gray-800 rounded-xl overflow-hidden">
                                    <div class="aspect-[4/3] bg-gray-200 dark:bg-gray-700 animate-pulse"></div>
                                </div>
                            </div>
                            <div v-else class="grid grid-cols-2 md:grid-cols-3 gap-4">
                                <RecentReportCard
                                    v-for="report in dashboards.slice(0, 6)"
                                    :key="report.id"
                                    :report="report"
                                    view-mode="org"
                                    :is-owner="report.user?.id === (currentUser as any)?.id"
                                />
                            </div>
                        </div>

                        <!-- Reports list -->
                        <div class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-1">{{ $t('projects.tabs.reports') }}</div>
                        <div v-if="loadingReports" class="space-y-2 mt-2">
                            <div v-for="i in 4" :key="i" class="h-11 rounded-md bg-gray-100 dark:bg-gray-800 animate-pulse"></div>
                        </div>
                        <ul v-else-if="reports.length" class="divide-y divide-gray-100 dark:divide-gray-800">
                            <li v-for="report in reports" :key="report.id">
                                <NuxtLink
                                    :to="`/reports/${report.id}`"
                                    class="flex items-center gap-3 px-2 py-2.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800/60 cursor-pointer"
                                >
                                    <UIcon :name="reportTypeIcon(report)" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                                    <span class="flex-1 truncate text-[13px] text-gray-800 dark:text-gray-200">{{ report.title || $t('reports.untitled') }}</span>
                                    <UIcon v-if="report.is_starred" name="i-heroicons-star-solid" class="w-3.5 h-3.5 shrink-0 text-amber-400" />
                                    <UTooltip v-if="!isOwn(report)" :text="$t('projects.readOnlyBadge')">
                                        <UIcon name="i-heroicons-eye" class="w-3.5 h-3.5 shrink-0 text-gray-300 dark:text-gray-600" />
                                    </UTooltip>
                                    <span class="hidden sm:block text-[11px] text-gray-400 dark:text-gray-500 w-28 truncate text-end">{{ formatDate(report.last_activity_at || report.created_at) }}</span>
                                    <UTooltip :text="report.user?.name || ''">
                                        <div class="flex items-center justify-center w-5 h-5 rounded-full bg-gray-200 dark:bg-gray-700 text-[9px] font-semibold text-gray-600 dark:text-gray-300 shrink-0">
                                            {{ (report.user?.name || '?').charAt(0).toUpperCase() }}
                                        </div>
                                    </UTooltip>
                                </NuxtLink>
                            </li>
                        </ul>
                        <div v-else class="mt-4 flex flex-col items-center text-center py-10 border border-dashed border-gray-200 dark:border-gray-700 rounded-xl">
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

                    <!-- Context rail: members / agents / files / instructions -->
                    <aside class="hidden lg:block w-60 shrink-0 space-y-3">
                        <div class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.members') }}</span>
                                <button v-if="project.can_manage" class="text-[11px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" @click="openShareModal">{{ $t('projects.overview.manage') }}</button>
                            </div>
                            <div class="flex items-center -space-x-1.5">
                                <UTooltip v-for="member in projectMembers.slice(0, 8)" :key="member.user_id" :text="member.user_name || member.user_email || ''">
                                    <div class="flex items-center justify-center w-6 h-6 rounded-full bg-gray-200 dark:bg-gray-700 ring-2 ring-white dark:ring-gray-900 text-[10px] font-semibold text-gray-600 dark:text-gray-300">
                                        {{ (member.user_name || member.user_email || '?').charAt(0).toUpperCase() }}
                                    </div>
                                </UTooltip>
                                <span v-if="projectMembers.length > 8" class="ps-3 text-[11px] text-gray-400">+{{ projectMembers.length - 8 }}</span>
                                <span v-if="!projectMembers.length" class="text-[12px] text-gray-400">{{ $t('projects.overview.onlyYou') }}</span>
                            </div>
                        </div>

                        <div class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.agents') }}</span>
                                <button v-if="project.can_manage" class="text-[11px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" @click="view = 'settings'">{{ $t('projects.overview.manage') }}</button>
                            </div>
                            <div v-if="project.data_sources?.length" class="space-y-1">
                                <div v-for="agent in project.data_sources" :key="agent.id" class="flex items-center gap-1.5 text-[12px] text-gray-600 dark:text-gray-300">
                                    <UIcon name="i-heroicons-cube" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
                                    <span class="truncate">{{ agent.name }}</span>
                                </div>
                            </div>
                            <p v-else class="text-[12px] text-gray-400">{{ $t('projects.overview.noAgents') }}</p>
                        </div>

                        <div class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.files') }}</span>
                                <button v-if="project.can_manage" class="text-[11px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" @click="view = 'settings'">{{ $t('projects.overview.manage') }}</button>
                            </div>
                            <div v-if="project.files?.length" class="space-y-1">
                                <div v-for="file in project.files" :key="file.id" class="flex items-center gap-1.5 text-[12px] text-gray-600 dark:text-gray-300">
                                    <UIcon name="i-heroicons-document" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
                                    <span class="truncate">{{ file.filename }}</span>
                                </div>
                            </div>
                            <p v-else class="text-[12px] text-gray-400">{{ $t('projects.overview.noFiles') }}</p>
                        </div>

                        <div v-if="project.instructions" class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.instructions') }}</span>
                                <button v-if="project.can_manage" class="text-[11px] text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" @click="view = 'settings'">{{ $t('projects.overview.manage') }}</button>
                            </div>
                            <p class="text-[12px] text-gray-500 dark:text-gray-400 whitespace-pre-line line-clamp-5">{{ project.instructions }}</p>
                        </div>
                    </aside>
                </div>

                <!-- ── Settings (gear) ── -->
                <div v-else-if="view === 'settings'" class="max-w-xl mt-6">
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
                        <div>
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.instructions') }}</label>
                            <textarea v-model="editInstructions" rows="4" :disabled="!project.can_manage"
                                :placeholder="$t('projects.settings.instructionsPlaceholder')"
                                class="w-full px-3 py-2 text-[13px] font-mono bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 disabled:opacity-60"></textarea>
                            <p class="mt-1 text-[11px] text-gray-400 dark:text-gray-500">{{ $t('projects.settings.instructionsHint') }}</p>
                        </div>
                        <div v-if="project.can_manage" class="pt-1">
                            <button @click="saveSettings" :disabled="savingSettings || !editName.trim()"
                                class="px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50">
                                {{ savingSettings ? $t('common.loading') : $t('common.save') }}
                            </button>
                        </div>
                    </div>

                    <!-- Default agents & files: copied onto every new report in the project -->
                    <div class="mt-8 p-4 rounded-xl border border-gray-200 dark:border-gray-700">
                        <div class="flex items-center gap-2 text-[13px] font-medium text-gray-600 dark:text-gray-300">
                            <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-400" />{{ $t('projects.settings.defaultsTitle') }}
                        </div>
                        <p class="mt-1 text-[12px] text-gray-400 dark:text-gray-500">{{ $t('projects.settings.defaultsHint') }}</p>

                        <div class="mt-3">
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.defaultAgents') }}</label>
                            <div class="max-h-40 overflow-y-auto rounded-md border border-gray-100 dark:border-gray-800 divide-y divide-gray-50 dark:divide-gray-800/60">
                                <label v-for="agent in orgAgents" :key="agent.id"
                                    class="flex items-center gap-2 px-3 py-2 text-[13px] text-gray-700 dark:text-gray-200"
                                    :class="project.can_manage ? 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/60' : 'opacity-70'">
                                    <input type="checkbox" class="rounded border-gray-300" :disabled="!project.can_manage"
                                        :checked="selectedAgentIds.includes(agent.id)"
                                        @change="toggleDefaultAgent(agent.id)" />
                                    <span class="truncate">{{ agent.name }}</span>
                                </label>
                                <div v-if="!orgAgents.length" class="px-3 py-2 text-[12px] text-gray-400">{{ $t('projects.settings.noAgents') }}</div>
                            </div>
                        </div>

                        <div class="mt-3">
                            <label class="block text-[12px] font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('projects.settings.defaultFiles') }}</label>
                            <div class="max-h-40 overflow-y-auto rounded-md border border-gray-100 dark:border-gray-800 divide-y divide-gray-50 dark:divide-gray-800/60">
                                <label v-for="file in orgFiles" :key="file.id"
                                    class="flex items-center gap-2 px-3 py-2 text-[13px] text-gray-700 dark:text-gray-200"
                                    :class="project.can_manage ? 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/60' : 'opacity-70'">
                                    <input type="checkbox" class="rounded border-gray-300" :disabled="!project.can_manage"
                                        :checked="selectedFileIds.includes(file.id)"
                                        @change="toggleDefaultFile(file.id)" />
                                    <span class="truncate">{{ file.filename }}</span>
                                </label>
                                <div v-if="!orgFiles.length" class="px-3 py-2 text-[12px] text-gray-400">{{ $t('projects.settings.noFiles') }}</div>
                            </div>
                        </div>

                        <div v-if="project.can_manage" class="mt-3">
                            <button @click="saveDefaults" :disabled="savingDefaults"
                                class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
                                {{ savingDefaults ? $t('common.loading') : $t('projects.settings.saveDefaults') }}
                            </button>
                        </div>
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

        <!-- Share: single collaborator role — members see everything read-only and can fork -->
        <UModal v-model="shareModalOpen">
            <div class="p-4">
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('projects.share.title') }}</h3>
                <p class="mt-1 text-[12px] text-gray-400 dark:text-gray-500">{{ $t('projects.share.hint') }}</p>

                <div class="mt-3 flex items-center gap-2">
                    <select v-model="shareSelectedUserId"
                        class="flex-1 h-9 px-2 text-[13px] bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400">
                        <option value="" disabled>{{ $t('projects.share.pickMember') }}</option>
                        <option v-for="m in shareCandidates" :key="m.user.id" :value="m.user.id">
                            {{ m.user.name || m.user.email }}
                        </option>
                    </select>
                    <button
                        class="shrink-0 px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                        :disabled="!shareSelectedUserId || shareBusy"
                        @click="addMember"
                    >{{ $t('projects.share.add') }}</button>
                </div>

                <ul class="mt-4 divide-y divide-gray-100 dark:divide-gray-800">
                    <li v-for="member in projectMembers" :key="member.user_id" class="flex items-center gap-2 py-2">
                        <div class="flex items-center justify-center w-6 h-6 rounded-full bg-gray-200 dark:bg-gray-700 text-[10px] font-semibold text-gray-600 dark:text-gray-300 shrink-0">
                            {{ (member.user_name || member.user_email || '?').charAt(0).toUpperCase() }}
                        </div>
                        <div class="flex-1 min-w-0">
                            <div class="text-[13px] text-gray-800 dark:text-gray-200 truncate">{{ member.user_name || member.user_email }}</div>
                        </div>
                        <span class="text-[11px] text-gray-400 dark:text-gray-500">
                            {{ member.permissions.includes('owner') ? $t('projects.share.roleOwner') : $t('projects.share.roleCollaborator') }}
                        </span>
                        <button
                            v-if="!member.permissions.includes('owner')"
                            class="shrink-0 flex items-center justify-center w-6 h-6 rounded text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/40"
                            :disabled="shareBusy"
                            @click="removeMember(member.user_id)"
                            :aria-label="$t('common.delete')"
                        >
                            <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
                        </button>
                    </li>
                </ul>

                <div class="flex justify-end mt-3">
                    <button class="px-3 py-1.5 text-[13px] rounded-md border border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800" @click="shareModalOpen = false">{{ $t('common.cancel') }}</button>
                </div>
            </div>
        </UModal>

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
import Spinner from '~/components/Spinner.vue'
import RecentReportCard from '~/components/home/RecentReportCard.vue'

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

// One workspace screen: overview (dashboards + reports + context rail);
// settings lives behind the gear button.
const view = ref<'overview' | 'settings'>('overview')

const isShared = computed(() =>
    project.value && (project.value.access === 'org' || (project.value.member_count || 0) > 0))

// Settings form state
const editName = ref('')
const editDescription = ref('')
const editColor = ref<string | null>(null)
const editInstructions = ref('')
const colorSwatches = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2', '#64748b']

// ── Defaults (agents/files copied onto new reports) ─────────────────────
const orgAgents = ref<any[]>([])
const orgFiles = ref<any[]>([])
const selectedAgentIds = ref<string[]>([])
const selectedFileIds = ref<string[]>([])
const savingDefaults = ref(false)
const defaultsLoaded = ref(false)

const fetchDefaultsOptions = async () => {
    if (defaultsLoaded.value) return
    try {
        const [dsResp, fResp]: any[] = await Promise.all([
            useMyFetch('/data_sources', { method: 'GET' }),
            useMyFetch('/files', { method: 'GET' }),
        ])
        if (dsResp?.status?.value === 'success' && Array.isArray(dsResp.data?.value)) orgAgents.value = dsResp.data.value
        if (fResp?.status?.value === 'success' && Array.isArray(fResp.data?.value)) orgFiles.value = fResp.data.value
        defaultsLoaded.value = true
    } catch {}
}
const toggleDefaultAgent = (id: string) => {
    selectedAgentIds.value = selectedAgentIds.value.includes(id)
        ? selectedAgentIds.value.filter(x => x !== id)
        : [...selectedAgentIds.value, id]
}
const toggleDefaultFile = (id: string) => {
    selectedFileIds.value = selectedFileIds.value.includes(id)
        ? selectedFileIds.value.filter(x => x !== id)
        : [...selectedFileIds.value, id]
}
const saveDefaults = async () => {
    if (savingDefaults.value) return
    savingDefaults.value = true
    try {
        const [dsResp, fResp]: any[] = await Promise.all([
            useMyFetch(`/projects/${projectId.value}/data_sources`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ data_source_ids: selectedAgentIds.value }),
            }),
            useMyFetch(`/projects/${projectId.value}/files`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_ids: selectedFileIds.value }),
            }),
        ])
        if (dsResp?.error?.value) throw dsResp.error.value
        if (fResp?.error?.value) throw fResp.error.value
        await fetchProject()
        toast.add({ title: t('projects.settings.saved'), color: 'green' })
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        savingDefaults.value = false
    }
}
watch(view, (v) => { if (v === 'settings') fetchDefaultsOptions() })

// ── Share (single collaborator role: view + fork) ────────────────────────
const { organization } = useOrganization()
const shareModalOpen = ref(false)
const shareBusy = ref(false)
const shareSelectedUserId = ref('')
const projectMembers = ref<any[]>([])
const orgMembers = ref<any[]>([])

const shareCandidates = computed(() => {
    const taken = new Set(projectMembers.value.map((m: any) => m.user_id))
    return orgMembers.value.filter((m: any) => m.user?.id && !taken.has(m.user.id))
})
const fetchMembers = async () => {
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/members`, { method: 'GET' })
        if (resp?.status?.value === 'success' && Array.isArray(resp.data?.value)) projectMembers.value = resp.data.value
    } catch {}
}
const openShareModal = async () => {
    shareModalOpen.value = true
    shareSelectedUserId.value = ''
    fetchMembers()
    try {
        const orgId = (organization.value as any)?.id
        if (orgId) {
            const resp: any = await useMyFetch(`/organizations/${orgId}/members`, { method: 'GET' })
            if (resp?.status?.value === 'success' && Array.isArray(resp.data?.value)) orgMembers.value = resp.data.value
        }
    } catch {}
}
const addMember = async () => {
    if (!shareSelectedUserId.value || shareBusy.value) return
    shareBusy.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/members`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: shareSelectedUserId.value, permissions: ['view'] }),
        })
        if (resp?.error?.value) throw resp.error.value
        projectMembers.value = resp.data?.value || []
        shareSelectedUserId.value = ''
        await fetchProject()
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        shareBusy.value = false
    }
}
const removeMember = async (userId: string) => {
    if (shareBusy.value) return
    shareBusy.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/members/${userId}`, { method: 'DELETE' })
        if (resp?.error?.value) throw resp.error.value
        projectMembers.value = resp.data?.value || []
        await fetchProject()
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        shareBusy.value = false
    }
}

const fetchProject = async () => {
    loadingProject.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}`, { method: 'GET' })
        if (resp?.status?.value === 'success' && resp.data?.value) {
            project.value = resp.data.value
            editName.value = project.value.name || ''
            editDescription.value = project.value.description || ''
            editColor.value = project.value.color || null
            editInstructions.value = project.value.instructions || ''
            selectedAgentIds.value = (project.value.data_sources || []).map((d: any) => d.id)
            selectedFileIds.value = (project.value.files || []).map((f: any) => f.id)
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

// Collaborators open any project report read-only; the eye badge marks
// reports owned by someone else.
const isOwn = (report: any) => report.user?.id === (currentUser.value as any)?.id

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
            instructions: editInstructions.value,
        } as any)
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
    await Promise.all([fetchProject(), fetchReports(), fetchDashboards(), fetchMembers(), fetchProjects()])
})
watch(projectId, async () => {
    dashboardsLoaded.value = false
    view.value = 'overview'
    await Promise.all([fetchProject(), fetchReports(), fetchDashboards(), fetchMembers()])
})
</script>
