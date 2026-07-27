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

                <!-- ── Overview: reports + dashboards + inline context rail ── -->
                <div v-if="view === 'overview'" class="mt-6 flex items-start gap-8">
                    <div class="flex-1 min-w-0 space-y-7">
                        <!-- Automations (scheduled tasks + dashboard refreshes, derived via reports) -->
                        <div>
                            <div class="flex items-center justify-between mb-1">
                                <button type="button" class="flex items-center gap-1 text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-600 dark:hover:text-gray-300" @click="sectionOpen.automations = !sectionOpen.automations">
                                    <UIcon :name="sectionOpen.automations ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
                                    {{ $t('projects.overview.automations') }}
                                    <span v-if="automations.length" class="normal-case font-normal text-gray-300 dark:text-gray-600">{{ automations.length }}</span>
                                </button>
                            </div>
                            <template v-if="sectionOpen.automations">
                                <ul v-if="automations.length" class="divide-y divide-gray-100 dark:divide-gray-800">
                                    <li v-for="auto in automations" :key="auto.id">
                                        <NuxtLink :to="`/reports/${auto.report_id}`" class="flex items-center gap-3 px-2 py-2 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800/60 cursor-pointer">
                                            <UIcon :name="auto.kind === 'task' ? 'i-heroicons-clock' : 'i-heroicons-arrow-path'" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                                            <span class="flex-1 truncate text-[13px] text-gray-800 dark:text-gray-200">{{ auto.label }}</span>
                                            <span class="hidden sm:block text-[11px] font-mono text-gray-400 dark:text-gray-500">{{ auto.cron_schedule }}</span>
                                            <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="auto.is_active ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'"></span>
                                        </NuxtLink>
                                    </li>
                                </ul>
                                <p v-else class="text-[12px] text-gray-400 dark:text-gray-500 py-1.5">{{ $t('projects.overview.noAutomations') }}</p>
                            </template>
                        </div>

                        <!-- Reports (paginated) -->
                        <div>
                            <div class="flex items-center justify-between mb-1">
                                <button type="button" class="flex items-center gap-1 text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-600 dark:hover:text-gray-300" @click="sectionOpen.reports = !sectionOpen.reports">
                                    <UIcon :name="sectionOpen.reports ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
                                    {{ $t('projects.tabs.reports') }}
                                    <span v-if="reportsMeta.total" class="normal-case font-normal text-gray-300 dark:text-gray-600">{{ reportsMeta.total }}</span>
                                </button>
                                <div v-if="sectionOpen.reports && reportsMeta.total_pages > 1" class="flex items-center gap-1 text-[11px] text-gray-400">
                                    <button class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40" :disabled="reportsPage <= 1" @click="changeReportsPage(reportsPage - 1)" :aria-label="$t('common.previous')">
                                        <UIcon name="i-heroicons-chevron-left" class="w-3.5 h-3.5 rtl-flip" />
                                    </button>
                                    <span>{{ reportsPage }} / {{ reportsMeta.total_pages }}</span>
                                    <button class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40" :disabled="reportsPage >= reportsMeta.total_pages" @click="changeReportsPage(reportsPage + 1)" :aria-label="$t('common.next')">
                                        <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 rtl-flip" />
                                    </button>
                                </div>
                            </div>
                            <template v-if="sectionOpen.reports">
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
                            </template>
                        </div>

                        <!-- Dashboards (paginated, thumbnails) -->
                        <div>
                            <div class="flex items-center justify-between mb-2">
                                <button type="button" class="flex items-center gap-1 text-[11px] font-semibold text-gray-400 uppercase tracking-wider hover:text-gray-600 dark:hover:text-gray-300" @click="sectionOpen.dashboards = !sectionOpen.dashboards">
                                    <UIcon :name="sectionOpen.dashboards ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl-flip" />
                                    {{ $t('projects.tabs.dashboards') }}
                                    <span v-if="dashboardsMeta.total" class="normal-case font-normal text-gray-300 dark:text-gray-600">{{ dashboardsMeta.total }}</span>
                                </button>
                                <div v-if="sectionOpen.dashboards && dashboardsMeta.total_pages > 1" class="flex items-center gap-1 text-[11px] text-gray-400">
                                    <button class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40" :disabled="dashboardsPage <= 1" @click="changeDashboardsPage(dashboardsPage - 1)" :aria-label="$t('common.previous')">
                                        <UIcon name="i-heroicons-chevron-left" class="w-3.5 h-3.5 rtl-flip" />
                                    </button>
                                    <span>{{ dashboardsPage }} / {{ dashboardsMeta.total_pages }}</span>
                                    <button class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40" :disabled="dashboardsPage >= dashboardsMeta.total_pages" @click="changeDashboardsPage(dashboardsPage + 1)" :aria-label="$t('common.next')">
                                        <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 rtl-flip" />
                                    </button>
                                </div>
                            </div>
                            <template v-if="sectionOpen.dashboards">
                                <div v-if="loadingDashboards" class="grid grid-cols-3 md:grid-cols-4 gap-3">
                                    <div v-for="i in 4" :key="i" class="bg-gray-100 dark:bg-gray-800 rounded-xl overflow-hidden">
                                        <div class="aspect-[4/3] bg-gray-200 dark:bg-gray-700 animate-pulse"></div>
                                    </div>
                                </div>
                                <div v-else-if="dashboards.length" class="grid grid-cols-3 md:grid-cols-4 gap-3">
                                    <RecentReportCard
                                        v-for="report in dashboards"
                                        :key="report.id"
                                        :report="report"
                                        view-mode="org"
                                        :is-owner="report.user?.id === (currentUser as any)?.id"
                                    />
                                </div>
                                <p v-else class="text-[12px] text-gray-400 dark:text-gray-500 py-1.5">{{ $t('projects.noDashboards') }}</p>
                            </template>
                        </div>
                    </div>

                    <!-- Context rail: members / agents / files / instructions — all inline -->
                    <aside class="hidden lg:block w-64 shrink-0 space-y-3">
                        <!-- Members -->
                        <div class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.members') }}</span>
                                <UPopover v-if="project.can_manage" v-model:open="memberPickerOpen" :popper="{ placement: 'bottom-end' }">
                                    <button name="add-member" class="flex items-center justify-center w-5 h-5 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" @click="loadOrgMembers">
                                        <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
                                    </button>
                                    <template #panel>
                                        <div class="w-56 p-1.5 text-xs max-h-64 overflow-y-auto" data-testid="member-picker">
                                            <button
                                                v-for="m in shareCandidates"
                                                :key="m.user.id"
                                                type="button"
                                                class="flex items-center gap-2 w-full px-2 py-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 text-gray-700 dark:text-gray-200 disabled:opacity-60"
                                                :disabled="shareBusy"
                                                @click="addMember(m.user.id)"
                                            >
                                                <div class="flex items-center justify-center w-5 h-5 rounded-full bg-gray-200 dark:bg-gray-700 text-[9px] font-semibold text-gray-600 dark:text-gray-300 shrink-0">
                                                    {{ (m.user.name || m.user.email || '?').charAt(0).toUpperCase() }}
                                                </div>
                                                <span class="flex-1 truncate text-start">{{ m.user.name || m.user.email }}</span>
                                            </button>
                                            <div v-if="!shareCandidates.length" class="px-2 py-1.5 text-gray-400">{{ $t('projects.overview.everyoneAdded') }}</div>
                                        </div>
                                    </template>
                                </UPopover>
                            </div>
                            <div class="space-y-1">
                                <div v-for="member in projectMembers" :key="member.user_id" class="group/member flex items-center gap-2">
                                    <div class="flex items-center justify-center w-5 h-5 rounded-full bg-gray-200 dark:bg-gray-700 text-[9px] font-semibold text-gray-600 dark:text-gray-300 shrink-0">
                                        {{ (member.user_name || member.user_email || '?').charAt(0).toUpperCase() }}
                                    </div>
                                    <span class="flex-1 truncate text-[12px] text-gray-600 dark:text-gray-300">{{ member.user_name || member.user_email }}</span>
                                    <span v-if="member.permissions.includes('owner')" class="text-[10px] text-gray-300 dark:text-gray-600">{{ $t('projects.share.roleOwner') }}</span>
                                    <button
                                        v-else-if="project.can_manage"
                                        class="flex items-center justify-center w-4 h-4 rounded text-gray-300 hover:text-red-500 opacity-0 group-hover/member:opacity-100 transition-opacity"
                                        :disabled="shareBusy"
                                        @click="removeMember(member.user_id)"
                                        :aria-label="$t('common.delete')"
                                    >
                                        <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
                                    </button>
                                </div>
                                <p v-if="!projectMembers.length" class="text-[12px] text-gray-400">{{ $t('projects.overview.onlyYou') }}</p>
                            </div>
                            <p v-if="project.can_manage" class="mt-2 text-[10px] text-gray-300 dark:text-gray-600">{{ $t('projects.share.hint') }}</p>
                        </div>

                        <!-- Agents -->
                        <div class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.agents') }}</span>
                                <UPopover v-if="project.can_manage" v-model:open="agentPickerOpen" :popper="{ placement: 'bottom-end' }">
                                    <button name="add-agent" class="flex items-center justify-center w-5 h-5 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" @click="loadOrgAgents">
                                        <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
                                    </button>
                                    <template #panel>
                                        <div class="w-56 p-1.5 text-xs" data-testid="agent-picker">
                                            <input
                                                v-model="agentSearch"
                                                type="text"
                                                :placeholder="$t('projects.overview.searchAgents')"
                                                class="w-full h-7 px-2 mb-1 text-[12px] bg-gray-50 dark:bg-gray-800 border border-gray-100 dark:border-gray-700 rounded outline-none focus:border-gray-300"
                                            />
                                            <div class="max-h-56 overflow-y-auto">
                                                <button
                                                    v-for="agent in agentCandidates"
                                                    :key="agent.id"
                                                    type="button"
                                                    class="flex items-center gap-2 w-full px-2 py-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 text-gray-700 dark:text-gray-200 disabled:opacity-60"
                                                    :disabled="savingAgents"
                                                    @click="addAgent(agent.id)"
                                                >
                                                    <DataSourceIcon :type="agent.type" :connector-key="agent.connector_key" :icon="agent.icon" class="h-3.5 shrink-0" />
                                                    <span class="flex-1 truncate text-start">{{ agent.name }}</span>
                                                </button>
                                                <div v-if="!agentCandidates.length" class="px-2 py-1.5 text-gray-400">{{ $t('projects.settings.noAgents') }}</div>
                                            </div>
                                        </div>
                                    </template>
                                </UPopover>
                            </div>
                            <div v-if="project.data_sources?.length" class="space-y-1">
                                <div v-for="agent in project.data_sources" :key="agent.id" class="group/agent flex items-center gap-1.5 text-[12px] text-gray-600 dark:text-gray-300">
                                    <DataSourceIcon :type="agent.type" :connector-key="agent.connector_key" :icon="agent.icon" class="h-3.5 shrink-0" />
                                    <span class="flex-1 truncate">{{ agent.name }}</span>
                                    <button
                                        v-if="project.can_manage"
                                        class="flex items-center justify-center w-4 h-4 rounded text-gray-300 hover:text-red-500 opacity-0 group-hover/agent:opacity-100 transition-opacity"
                                        :disabled="savingAgents"
                                        @click="removeAgent(agent.id)"
                                        :aria-label="$t('common.delete')"
                                    >
                                        <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            </div>
                            <p v-else class="text-[12px] text-gray-400">{{ $t('projects.overview.noAgents') }}</p>
                            <p class="mt-2 text-[10px] text-gray-300 dark:text-gray-600">{{ $t('projects.settings.defaultsHint') }}</p>
                        </div>

                        <!-- Files (drag & drop) -->
                        <div
                            class="p-3 rounded-xl border transition-colors"
                            :class="isDraggingFiles ? 'border-blue-300 bg-blue-50/40 dark:bg-blue-950/20' : 'border-gray-100 dark:border-gray-800'"
                            @dragover.prevent="project.can_manage ? isDraggingFiles = true : null"
                            @dragleave.prevent="isDraggingFiles = false"
                            @drop.prevent="onFilesDropped"
                        >
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.files') }}</span>
                                <button v-if="project.can_manage" name="add-file" class="flex items-center justify-center w-5 h-5 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800" @click="fileInputRef?.click()">
                                    <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
                                </button>
                                <input ref="fileInputRef" type="file" multiple class="hidden" @change="onFilesPicked" />
                            </div>
                            <div v-if="project.files?.length" class="space-y-1">
                                <div v-for="file in project.files" :key="file.id" class="group/file flex items-center gap-1.5 text-[12px] text-gray-600 dark:text-gray-300">
                                    <UIcon name="i-heroicons-document" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
                                    <span class="flex-1 truncate">{{ file.filename }}</span>
                                    <button
                                        v-if="project.can_manage"
                                        class="flex items-center justify-center w-4 h-4 rounded text-gray-300 hover:text-red-500 opacity-0 group-hover/file:opacity-100 transition-opacity"
                                        :disabled="uploadingFiles"
                                        @click="removeFile(file.id)"
                                        :aria-label="$t('common.delete')"
                                    >
                                        <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
                                    </button>
                                </div>
                            </div>
                            <button
                                v-if="project.can_manage"
                                type="button"
                                class="mt-2 w-full py-3 rounded-md border border-dashed text-[11px] transition-colors"
                                :class="isDraggingFiles ? 'border-blue-400 text-blue-500' : 'border-gray-200 dark:border-gray-700 text-gray-400 hover:text-gray-600 hover:border-gray-300'"
                                @click="fileInputRef?.click()"
                            >
                                <template v-if="uploadingFiles">{{ $t('projects.overview.uploading') }}</template>
                                <template v-else>{{ $t('projects.overview.dropFiles') }}</template>
                            </button>
                            <p v-else-if="!project.files?.length" class="text-[12px] text-gray-400">{{ $t('projects.overview.noFiles') }}</p>
                        </div>

                        <!-- Instructions (inline editor) -->
                        <div v-if="project.can_manage || project.instructions" class="p-3 rounded-xl border border-gray-100 dark:border-gray-800">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">{{ $t('projects.overview.instructions') }}</span>
                            </div>
                            <template v-if="project.can_manage">
                                <textarea
                                    v-model="instructionsDraft"
                                    rows="5"
                                    :placeholder="$t('projects.settings.instructionsPlaceholder')"
                                    class="w-full px-2 py-1.5 text-[12px] bg-gray-50/60 dark:bg-gray-800/60 border border-gray-100 dark:border-gray-800 rounded-md outline-none focus:border-gray-300 dark:focus:border-gray-600 text-gray-700 dark:text-gray-200 resize-y"
                                    data-testid="instructions-editor"
                                ></textarea>
                                <div v-if="instructionsDirty" class="mt-1.5 flex items-center justify-end gap-1.5">
                                    <button class="px-2 py-1 text-[11px] rounded border border-gray-200 dark:border-gray-700 text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800" @click="instructionsDraft = project.instructions || ''">{{ $t('common.cancel') }}</button>
                                    <button class="px-2 py-1 text-[11px] rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50" :disabled="savingInstructions" @click="saveInstructions">
                                        {{ savingInstructions ? $t('common.loading') : $t('common.save') }}
                                    </button>
                                </div>
                                <p v-else class="mt-1 text-[10px] text-gray-300 dark:text-gray-600">{{ $t('projects.settings.instructionsHint') }}</p>
                            </template>
                            <p v-else class="text-[12px] text-gray-500 dark:text-gray-400 whitespace-pre-line line-clamp-6">{{ project.instructions }}</p>
                        </div>
                    </aside>
                </div>

                <!-- ── Settings (gear): name / description / color / danger zone ── -->
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
                        <div v-if="project.can_manage" class="pt-1">
                            <button @click="saveSettings" :disabled="savingSettings || !editName.trim()"
                                class="px-3 py-1.5 text-[13px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50">
                                {{ savingSettings ? $t('common.loading') : $t('common.save') }}
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

        <UModal v-model="confirmDeleteOpen">
            <div class="p-4">
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('projects.deleteTitle') }}</h3>
                <!-- Honest impact: archive (not destroy) reports/dashboards,
                     stop automations, unlink files. -->
                <p class="mt-2 text-[13px] text-gray-500 dark:text-gray-400">{{ $t('projects.deleteImpact', {
                    reports: project?.report_count ?? 0,
                    dashboards: project?.dashboard_count ?? 0,
                    automations: project?.automation_count ?? 0,
                    files: project?.files?.length ?? 0,
                }) }}</p>
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
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const toast = useToast()
const { data: currentUser } = useAuth()
const { fetchProjects, updateProject, deleteProject } = useProjects()
const { selectedAgentObjects } = useAgent()
const { organization } = useOrganization()

const projectId = computed(() => String(route.params.id))

const project = ref<any>(null)
const loadingProject = ref(true)
const creating = ref(false)
const savingSettings = ref(false)
const confirmDeleteOpen = ref(false)
const deleting = ref(false)

// One workspace screen: overview; settings lives behind the gear button.
const view = ref<'overview' | 'settings'>('overview')

const isShared = computed(() =>
    project.value && (project.value.access === 'org' || (project.value.member_count || 0) > 0))

// ── Reports & dashboards (both server-paginated) ─────────────────────────
const REPORTS_PAGE_SIZE = 10
const DASHBOARDS_PAGE_SIZE = 8
const reports = ref<any[]>([])
const loadingReports = ref(true)
const reportsPage = ref(1)
const reportsMeta = ref<any>({ total: 0, total_pages: 1 })
const dashboards = ref<any[]>([])
const loadingDashboards = ref(false)
const dashboardsPage = ref(1)
const dashboardsMeta = ref<any>({ total: 0, total_pages: 1 })
const automations = ref<any[]>([])
// Sections collapse when empty (set after first fetch); reports stay open.
const sectionOpen = ref({ automations: true, reports: true, dashboards: true })

const fetchAutomations = async () => {
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/automations`, { method: 'GET' })
        if (resp?.status?.value === 'success' && Array.isArray(resp.data?.value)) {
            automations.value = resp.data.value
        }
    } catch {}
    sectionOpen.value.automations = automations.value.length > 0
}

const fetchReports = async () => {
    loadingReports.value = true
    try {
        const resp: any = await useMyFetch('/reports', {
            method: 'GET',
            query: { filter: 'all', project_id: projectId.value, page: reportsPage.value, limit: REPORTS_PAGE_SIZE, view: 'minimal' },
        })
        if (resp?.status?.value === 'success' && resp.data?.value?.reports) {
            reports.value = resp.data.value.reports
            reportsMeta.value = resp.data.value.meta || { total: 0, total_pages: 1 }
        }
    } catch {} finally {
        loadingReports.value = false
    }
}
const changeReportsPage = (page: number) => {
    reportsPage.value = Math.max(1, Math.min(page, reportsMeta.value.total_pages || 1))
    fetchReports()
}

const fetchDashboards = async () => {
    loadingDashboards.value = true
    try {
        const resp: any = await useMyFetch('/reports', {
            method: 'GET',
            query: { filter: 'all', project_id: projectId.value, has_artifacts: 'yes', page: dashboardsPage.value, limit: DASHBOARDS_PAGE_SIZE },
        })
        if (resp?.status?.value === 'success' && resp.data?.value?.reports) {
            dashboards.value = resp.data.value.reports
            dashboardsMeta.value = resp.data.value.meta || { total: 0, total_pages: 1 }
        }
    } catch {} finally {
        loadingDashboards.value = false
        sectionOpen.value.dashboards = (dashboardsMeta.value.total || 0) > 0
    }
}
const changeDashboardsPage = (page: number) => {
    dashboardsPage.value = Math.max(1, Math.min(page, dashboardsMeta.value.total_pages || 1))
    fetchDashboards()
}

// ── Members (inline add/remove; single collaborator role) ────────────────
const projectMembers = ref<any[]>([])
const orgMembers = ref<any[]>([])
const memberPickerOpen = ref(false)
const shareBusy = ref(false)

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
const loadOrgMembers = async () => {
    try {
        const orgId = (organization.value as any)?.id
        if (!orgId) return
        const resp: any = await useMyFetch(`/organizations/${orgId}/members`, { method: 'GET' })
        if (resp?.status?.value === 'success' && Array.isArray(resp.data?.value)) orgMembers.value = resp.data.value
    } catch {}
}
const addMember = async (userId: string) => {
    if (!userId || shareBusy.value) return
    shareBusy.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/members`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, permissions: ['view'] }),
        })
        if (resp?.error?.value) throw resp.error.value
        projectMembers.value = resp.data?.value || []
        memberPickerOpen.value = false
        await fetchProject({ silent: true })
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
        await fetchProject({ silent: true })
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        shareBusy.value = false
    }
}

// ── Agents (inline selector; saves immediately) ──────────────────────────
const orgAgents = ref<any[]>([])
const agentPickerOpen = ref(false)
const agentSearch = ref('')
const savingAgents = ref(false)

const agentCandidates = computed(() => {
    const taken = new Set((project.value?.data_sources || []).map((d: any) => d.id))
    const q = agentSearch.value.trim().toLowerCase()
    return orgAgents.value.filter((a: any) =>
        !taken.has(a.id) && (!q || String(a.name || '').toLowerCase().includes(q)))
})
const loadOrgAgents = async () => {
    try {
        // Only agents every current member can resolve may become defaults —
        // the server filters (and PUT still validates against stale lists).
        const resp: any = await useMyFetch(`/projects/${projectId.value}/data_sources/selectable`, {
            method: 'GET',
        })
        if (resp?.status?.value === 'success' && Array.isArray(resp.data?.value)) orgAgents.value = resp.data.value
    } catch {}
}
const saveAgentIds = async (ids: string[]) => {
    savingAgents.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}/data_sources`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data_source_ids: ids }),
        })
        if (resp?.error?.value) throw resp.error.value
        if (resp.data?.value) project.value = resp.data.value
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        savingAgents.value = false
    }
}
const addAgent = async (id: string) => {
    agentPickerOpen.value = false
    await saveAgentIds([...(project.value?.data_sources || []).map((d: any) => d.id), id])
}
const removeAgent = async (id: string) => {
    await saveAgentIds((project.value?.data_sources || []).map((d: any) => d.id).filter((x: string) => x !== id))
}

// ── Files (drag & drop upload + inline remove) ───────────────────────────
const fileInputRef = ref<HTMLInputElement | null>(null)
const isDraggingFiles = ref(false)
const uploadingFiles = ref(false)

const saveFileIds = async (ids: string[]) => {
    const resp: any = await useMyFetch(`/projects/${projectId.value}/files`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_ids: ids }),
    })
    if (resp?.error?.value) throw resp.error.value
    if (resp.data?.value) project.value = resp.data.value
}
const uploadFilesToProject = async (files: File[]) => {
    if (!files.length || uploadingFiles.value || !project.value?.can_manage) return
    uploadingFiles.value = true
    try {
        const newIds: string[] = []
        for (const file of files) {
            const formData = new FormData()
            formData.append('file', file)
            const resp: any = await useMyFetch('/files', { method: 'POST', body: formData })
            if (resp?.error?.value) throw resp.error.value
            const uploaded = resp.data?.value as any
            if (uploaded?.id) newIds.push(uploaded.id)
        }
        const current = (project.value?.files || []).map((f: any) => f.id)
        await saveFileIds([...current, ...newIds])
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        uploadingFiles.value = false
    }
}
const onFilesDropped = (e: DragEvent) => {
    isDraggingFiles.value = false
    uploadFilesToProject(Array.from(e.dataTransfer?.files || []))
}
const onFilesPicked = (e: Event) => {
    const input = e.target as HTMLInputElement
    uploadFilesToProject(Array.from(input.files || []))
    input.value = ''
}
const removeFile = async (id: string) => {
    try {
        await saveFileIds((project.value?.files || []).map((f: any) => f.id).filter((x: string) => x !== id))
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    }
}

// ── Instructions (inline editor) ─────────────────────────────────────────
const instructionsDraft = ref('')
const savingInstructions = ref(false)
const instructionsDirty = computed(() =>
    (instructionsDraft.value || '') !== (project.value?.instructions || ''))
const saveInstructions = async () => {
    if (savingInstructions.value) return
    savingInstructions.value = true
    try {
        await updateProject(projectId.value, { instructions: instructionsDraft.value } as any)
        await fetchProject({ silent: true })
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        savingInstructions.value = false
    }
}

// ── Settings (name / description / color only) ───────────────────────────
const editName = ref('')
const editDescription = ref('')
const editColor = ref<string | null>(null)
const colorSwatches = ['#2563eb', '#16a34a', '#d97706', '#dc2626', '#9333ea', '#0891b2', '#64748b']

const fetchProject = async (opts: { silent?: boolean } = {}) => {
    if (!opts.silent) loadingProject.value = true
    try {
        const resp: any = await useMyFetch(`/projects/${projectId.value}`, { method: 'GET' })
        if (resp?.status?.value === 'success' && resp.data?.value) {
            project.value = resp.data.value
            editName.value = project.value.name || ''
            editDescription.value = project.value.description || ''
            editColor.value = project.value.color || null
            instructionsDraft.value = project.value.instructions || ''
        } else if (!opts.silent) {
            project.value = null
        }
    } catch {
        if (!opts.silent) project.value = null
    } finally {
        if (!opts.silent) loadingProject.value = false
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
        })
        await fetchProject({ silent: true })
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
    await Promise.all([fetchProject(), fetchReports(), fetchDashboards(), fetchAutomations(), fetchMembers(), fetchProjects()])
})
watch(projectId, async () => {
    view.value = 'overview'
    reportsPage.value = 1
    dashboardsPage.value = 1
    await Promise.all([fetchProject(), fetchReports(), fetchDashboards(), fetchAutomations(), fetchMembers()])
})
</script>
