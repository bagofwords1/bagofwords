<template>
    <!-- One place for a dashboard's settings (behind the toolbar's gear), a
         tab each: when its data refreshes, what viewers get (the Data tab,
         chat, whose credentials their runs use), the name in its link; then
         the queries behind it, its resources and its analytics. Who has
         access stays in the Share dialog. -->
    <UTooltip :text="$t('dashboardSettings.open')">
        <button @click="open()" data-testid="dashboard-settings" :aria-label="$t('dashboardSettings.open')"
            class="p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors flex items-center">
            <Icon name="heroicons:cog-6-tooth" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
        </button>
    </UTooltip>

    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-4xl' }" :prevent-close="dirty" @close-prevented="requestClose">
        <div class="flex flex-col sm:flex-row h-[85vh] sm:h-[min(85vh,720px)]" data-testid="dashboard-settings-modal">
            <!-- Left: the tabs -->
            <nav class="sm:w-56 flex-shrink-0 bg-gray-50 dark:bg-gray-800/40 border-b sm:border-b-0 sm:border-e border-gray-200 dark:border-gray-800 sm:rounded-s-lg flex flex-col">
                <div class="hidden sm:flex items-center gap-2.5 px-5 pt-5 pb-4">
                    <div class="w-8 h-8 rounded-full bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 flex items-center justify-center flex-shrink-0">
                        <Icon name="heroicons:cog-6-tooth" class="w-4 h-4 text-gray-600 dark:text-gray-300" />
                    </div>
                    <span class="text-sm font-medium text-gray-900 dark:text-white leading-tight">{{ $t('dashboardSettings.title') }}</span>
                </div>
                <div class="flex sm:flex-col gap-0.5 overflow-x-auto px-2 sm:px-3 py-2 sm:py-0" role="tablist" aria-orientation="vertical">
                    <template v-for="(item, idx) in tabs" :key="item.key">
                        <div v-if="idx > 0 && item.group !== tabs[idx - 1].group" class="hidden sm:block h-px bg-gray-200 dark:bg-gray-700 my-2 mx-2" />
                        <div v-if="item.groupLabel && (idx === 0 || item.group !== tabs[idx - 1].group)"
                            class="hidden sm:block px-3 pt-1 pb-1 text-[11px] font-medium uppercase tracking-wide text-gray-400">{{ item.groupLabel }}</div>
                        <button type="button" role="tab" :aria-selected="tab === item.key" :data-testid="`settings-tab-${item.key}`"
                            class="flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm whitespace-nowrap text-start transition-colors outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
                            :class="tab === item.key
                                ? 'bg-blue-100/70 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200 font-medium'
                                : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800'"
                            @click="tab = item.key">
                            <Icon :name="item.icon" class="w-4 h-4 flex-shrink-0" />
                            {{ item.label }}
                        </button>
                    </template>
                </div>
            </nav>

            <!-- Right: the tab -->
            <div class="flex-1 min-w-0 min-h-0 flex flex-col">
                <div class="flex items-start justify-between gap-3 px-6 pt-5 pb-4">
                    <div class="min-w-0">
                        <h2 class="text-lg font-normal text-gray-900 dark:text-white truncate" data-testid="settings-tab-title">{{ currentTab.label }}</h2>
                        <p v-if="currentTab.desc" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ currentTab.desc }}</p>
                    </div>
                    <button @click="requestClose" :aria-label="$t('common.close')"
                        class="p-1.5 -me-1.5 rounded-full text-gray-400 hover:text-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800 dark:hover:text-gray-300 outline-none">
                        <Icon name="heroicons:x-mark" class="w-5 h-5" />
                    </button>
                </div>

                <div class="flex-1 min-h-0 overflow-y-auto px-6 pt-1 pb-4">
                    <!-- Settings (General and Viewers save together) -->
                    <div v-if="isSettingsTab && loading" class="flex items-center justify-center py-16">
                        <Spinner class="w-5 h-5 text-gray-400" />
                    </div>
                    <template v-else>
                        <!-- General: its name, its link, when its data refreshes -->
                        <section v-show="tab === 'general'" class="space-y-6" data-testid="settings-general">
                            <div data-testid="settings-name">
                                <label class="block text-sm font-medium text-gray-900 dark:text-white mb-1.5" for="dashboard-settings-name">{{ $t('dashboardSettings.nameTitle') }}</label>
                                <input id="dashboard-settings-name" v-model="nameDraft" type="text" maxlength="255" :disabled="!rename"
                                    :placeholder="$t('artifactFrame.renamePlaceholder')"
                                    class="w-full h-9 px-3 border border-gray-200 dark:border-gray-700 rounded-lg text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent disabled:opacity-60" />
                                <!-- No rename here: the owner renames it from a conversation of their own. -->
                                <p v-if="!rename" class="text-[11px] text-gray-400 mt-1">{{ capabilities?.includes('transfer') ? $t('dashboardSettings.renameInConversation') : $t('dashboardSettings.ownerOnly') }}</p>
                            </div>

                            <div data-testid="settings-link">
                                <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('dashboardSettings.linkTitle') }}</div>
                                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 mb-1.5">{{ $t('dashboardSettings.linkDesc') }}</p>
                                <!-- The fixed part is drawn as a grey add-on; only the name is typed. -->
                                <div class="flex items-stretch h-9 border border-gray-200 dark:border-gray-700 rounded-lg text-sm bg-white dark:bg-gray-900 min-w-0 overflow-hidden focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-transparent"
                                    :class="[!canSettings ? 'opacity-60' : '', !slugValid ? 'border-red-400' : '']" dir="ltr" data-testid="link-name-editor">
                                    <span class="flex items-center px-3 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 border-e border-gray-200 dark:border-gray-700 whitespace-nowrap select-none max-w-[55%] truncate"
                                        :title="linkPrefix">{{ linkPrefixShort }}</span>
                                    <input v-model="slugDraft" type="text" maxlength="80" :disabled="!canSettings" data-testid="link-name-input"
                                        class="flex-1 min-w-0 border-none outline-none bg-transparent px-3 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400"
                                        :placeholder="slugSuggestion || 'my-dashboard'" />
                                </div>
                                <p v-if="!slugValid" class="text-[11px] text-red-500 mt-1">{{ $t("errors['dashboard.slug_invalid']") }}</p>
                                <!-- Empty: offer the name as a link name in one click. -->
                                <button v-else-if="!slugDraft && slugSuggestion && canSettings" type="button" data-testid="link-name-suggest"
                                    class="mt-1.5 text-xs font-medium text-blue-700 dark:text-blue-400 hover:underline" @click="slugDraft = slugSuggestion">
                                    {{ $t('dashboardSettings.linkUseSuggestion', { slug: slugSuggestion }) }}
                                </button>
                                <!-- The link as it will work once saved. -->
                                <div class="mt-2 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 min-w-0" data-testid="link-preview">
                                    <Icon name="heroicons:link" class="w-3.5 h-3.5 flex-shrink-0" />
                                    <span class="flex-shrink-0">{{ slugDraft.trim() === (saved.slug || '') ? $t('dashboardSettings.linkPreview') : $t('dashboardSettings.linkPreviewUnsaved') }}</span>
                                    <span class="font-mono text-gray-700 dark:text-gray-300 truncate" dir="ltr">{{ effectiveLink }}</span>
                                    <!-- Copy only what already works: an unsaved name is not a link yet. -->
                                    <button v-if="slugDraft.trim() === (saved.slug || '')" type="button"
                                        class="flex-shrink-0 inline-flex items-center gap-1 text-blue-700 dark:text-blue-400 hover:underline" @click="copyEffectiveLink">
                                        <Icon :name="linkCopied ? 'heroicons:check' : 'heroicons:clipboard-document'" class="w-3.5 h-3.5" />
                                        {{ linkCopied ? $t('share.copied') : $t('share.copyLink') }}
                                    </button>
                                </div>
                                <p v-if="!slugDraft && saved.slug" class="text-[11px] text-gray-400 mt-1">{{ $t('share.linkNameRemove') }}</p>
                            </div>

                            <div data-testid="settings-refresh">
                                <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('dashboardSettings.refreshTitle') }}</div>
                                <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 mb-2">{{ $t('dashboardSettings.refreshDesc') }}</p>
                            <div class="inline-flex p-0.5 bg-gray-100 dark:bg-gray-800 rounded-lg mb-3" role="radiogroup">
                                <button v-for="opt in refreshModeOptions" :key="opt.value" type="button" role="radio"
                                    :aria-checked="refreshMode === opt.value" :disabled="!canSettings"
                                    :data-testid="`refresh-mode-${opt.value}`"
                                    class="px-3 py-1 text-xs rounded-md transition-colors disabled:cursor-not-allowed"
                                    :class="refreshMode === opt.value
                                        ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium'
                                        : 'text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'"
                                    @click="refreshMode = opt.value">
                                    {{ opt.label }}
                                </button>
                            </div>

                            <!-- Recurring builder (the schedule-task modal's) -->
                            <div v-if="refreshMode === 'recurring'" class="flex items-center gap-1.5 text-sm text-gray-600 dark:text-gray-400 flex-wrap">
                                <span>{{ $t('scheduledPrompt.every') }}</span>
                                <input v-if="recurInterval === 'minutes' || recurInterval === 'hours'" v-model.number="recurEveryN"
                                    type="number" min="1" :max="recurInterval === 'minutes' ? 59 : 23" :disabled="!canSettings"
                                    class="w-14 h-[30px] rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-1.5 text-sm text-center" />
                                <select v-model="recurInterval" :disabled="!canSettings"
                                    class="h-[30px] rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 text-sm">
                                    <option value="minutes">{{ $t('scheduledPrompt.intervalMinutes') }}</option>
                                    <option value="hours">{{ $t('scheduledPrompt.intervalHours') }}</option>
                                    <option value="day">{{ $t('scheduledPrompt.intervalDay') }}</option>
                                    <option value="weekdays">{{ $t('scheduledPrompt.intervalWeekdays') }}</option>
                                    <option value="week">{{ $t('scheduledPrompt.intervalWeek') }}</option>
                                    <option value="month">{{ $t('scheduledPrompt.intervalMonth') }}</option>
                                </select>
                                <template v-if="['day', 'weekdays', 'week', 'month'].includes(recurInterval)">
                                    <span>{{ $t('scheduledPrompt.at') }}</span>
                                    <!-- Typed only (no picker popup); dir=ltr keeps HH:MM readable in RTL. -->
                                    <input v-model="recurTime" type="time" step="60" required dir="ltr" :disabled="!canSettings"
                                        class="h-[30px] rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 text-sm tabular-nums text-gray-700 dark:text-gray-200 [color-scheme:light] dark:[color-scheme:dark] focus:outline-none focus:border-blue-400 focus:ring-1 focus:ring-blue-400/30 [&::-webkit-calendar-picker-indicator]:hidden" />
                                </template>
                                <template v-if="recurInterval === 'week'">
                                    <span>{{ $t('scheduledPrompt.on') }}</span>
                                    <div class="flex items-center gap-1">
                                        <button v-for="d in weekdays" :key="d.value" type="button" :disabled="!canSettings"
                                            @click="toggleRecurDay(d.value)" :title="d.label" :aria-pressed="recurDays.includes(d.value)"
                                            class="flex items-center justify-center w-6 h-6 rounded-full text-[11px] font-medium border transition-colors"
                                            :class="recurDays.includes(d.value)
                                                ? 'bg-blue-600 text-white border-blue-600'
                                                : 'bg-white dark:bg-gray-900 text-gray-500 dark:text-gray-400 border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600'">
                                            {{ d.short }}
                                        </button>
                                    </div>
                                </template>
                                <template v-if="recurInterval === 'month'">
                                    <span>{{ $t('scheduledPrompt.onDay') }}</span>
                                    <select v-model="recurDayOfMonth" :disabled="!canSettings"
                                        class="h-[30px] rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 text-sm">
                                        <option v-for="d in 28" :key="d" :value="d">{{ d }}</option>
                                    </select>
                                </template>
                            </div>
                            <p v-if="refreshMode === 'recurring' && scheduleLabel" class="mt-2 text-xs text-gray-400">{{ scheduleLabel }}</p>
                            <p v-if="refreshMode === 'on_open'" class="text-xs text-gray-400">{{ $t('dashboardSettings.onOpenNote') }}</p>
                            <p v-if="lastRunAt" class="mt-2 text-xs text-gray-400">{{ $t('dashboardSettings.lastRun', { at: formatDateTime(lastRunAt) }) }}</p>

                            <!-- Email after each scheduled run -->
                            <div v-if="smtpEnabled && refreshMode === 'recurring'" class="mt-4" data-testid="settings-subscribers">
                                <label class="text-xs font-medium text-gray-700 dark:text-gray-300 flex items-center gap-1.5 mb-1.5">
                                    <Icon name="heroicons:envelope" class="w-3.5 h-3.5" />
                                    {{ $t('dashboardSettings.notifyTitle') }}
                                </label>
                                <div class="flex flex-wrap items-center gap-1.5 border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-1.5 min-h-[36px] focus-within:ring-2 focus-within:ring-blue-500 focus-within:border-transparent bg-white dark:bg-gray-900">
                                    <span v-for="(sub, idx) in subscribers" :key="idx"
                                        class="inline-flex items-center gap-1 bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 text-xs ps-2 pe-1 py-0.5 rounded-full">
                                        {{ sub.type === 'user' ? memberName(sub.id) : sub.address }}
                                        <button @click="subscribers.splice(idx, 1)" :disabled="!canSettings" class="p-0.5 rounded-full hover:bg-gray-200 dark:hover:bg-gray-700 outline-none">
                                            <Icon name="heroicons:x-mark" class="w-3 h-3" />
                                        </button>
                                    </span>
                                    <div class="relative flex-1 min-w-[140px]">
                                        <input v-model="subInput" type="text" :disabled="!canSettings"
                                            class="w-full border-none outline-none text-sm bg-transparent p-0"
                                            :placeholder="$t('dashboardSettings.notifyPlaceholder')"
                                            @keydown.enter.prevent="subEnter" @keydown.,.prevent="addSubEmail(subInput)"
                                            @keydown.backspace="!subInput && subscribers.pop()"
                                            @focus="subDropdown = true" @input="subDropdown = true" @blur="subBlur" />
                                        <div v-if="subDropdown && subMatches.length > 0"
                                            class="absolute start-0 top-full mt-1 w-64 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg z-50 max-h-40 overflow-y-auto">
                                            <button v-for="m in subMatches" :key="m.id" @mousedown.prevent="addSubMember(m)"
                                                class="w-full text-start px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 flex flex-col">
                                                <span class="text-gray-900 dark:text-white">{{ m.name || m.email }}</span>
                                                <span v-if="m.name" class="text-xs text-gray-400">{{ m.email }}</span>
                                            </button>
                                        </div>
                                    </div>
                                </div>
                                <p class="text-[11px] text-gray-400 mt-1">{{ $t('dashboardSettings.notifyDesc') }}</p>
                            </div>
                            </div>

                            <!-- Owner: who it belongs to; the owner (or an org admin) hands it over. -->
                            <div data-testid="settings-owner">
                                <div class="text-sm font-medium text-gray-900 dark:text-white mb-1.5">{{ $t('dashboardSettings.ownerTitle') }}</div>
                                <div class="flex items-center justify-between gap-3">
                                    <div class="flex items-center gap-2.5 min-w-0">
                                        <div class="w-8 h-8 shrink-0 rounded-full bg-gray-100 dark:bg-gray-800 flex items-center justify-center text-sm font-medium text-gray-600 dark:text-gray-300">
                                            {{ (ownerPerson?.name || ownerPerson?.email || '?').charAt(0).toUpperCase() }}
                                        </div>
                                        <div class="flex flex-col min-w-0">
                                            <span class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ ownerPerson?.name || ownerPerson?.email || $t('dashboardCollab.share.unknownPerson') }}</span>
                                            <span v-if="ownerPerson?.name && ownerPerson?.email" class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ ownerPerson.email }}</span>
                                        </div>
                                    </div>
                                    <button v-if="capabilities?.includes('transfer') && !transferOpen" type="button" data-testid="settings-transfer"
                                        class="shrink-0 text-xs font-medium text-blue-700 dark:text-blue-400 hover:underline" @click="transferOpen = true">
                                        {{ $t('dashboardSettings.transferOwnership') }}
                                    </button>
                                </div>
                                <div v-if="transferOpen" class="mt-3 p-3 rounded-lg border border-gray-200 dark:border-gray-700">
                                    <HistoryOwner :dashboard-id="dashboardId" @transferred="onTransferred" />
                                </div>
                            </div>
                        </section>

                        <!-- What viewers get -->
                        <section v-show="tab === 'viewers'" data-testid="settings-viewers">
                            <div class="divide-y divide-gray-100 dark:divide-gray-800 -mt-3">
                                <div class="flex items-start justify-between gap-4 py-3">
                                    <div class="min-w-0">
                                        <div class="text-sm text-gray-800 dark:text-gray-200">{{ $t('share.includeDataTab') }}</div>
                                        <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('share.includeDataTabDesc') }}</div>
                                    </div>
                                    <UToggle v-model="includeDataTab" :disabled="!canSettings" class="flex-shrink-0 mt-0.5" data-testid="include-data-tab-toggle" />
                                </div>

                                <div class="py-3" data-testid="chat-settings">
                                    <div class="flex items-start justify-between gap-4">
                                        <div class="min-w-0">
                                            <div class="text-sm text-gray-800 dark:text-gray-200">{{ $t('share.allowChat') }}</div>
                                            <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('share.allowChatDesc') }}</div>
                                        </div>
                                        <UToggle v-model="chatEnabled" :disabled="!canSettings" class="flex-shrink-0 mt-0.5" data-testid="chat-enabled-toggle" />
                                    </div>
                                    <div v-if="chatEnabled" class="mt-3 ms-0.5 space-y-3 border-s-2 border-gray-100 dark:border-gray-800 ps-3">
                                        <div class="space-y-1.5">
                                            <div class="text-xs font-medium text-gray-600 dark:text-gray-400">{{ $t('share.chatScope') }}</div>
                                            <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-agents">
                                                <input type="radio" value="agents" v-model="chatScope" class="mt-0.5"
                                                    :disabled="!canSettings || reportAgents.length === 0" />
                                                <span class="flex flex-col">
                                                    <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeAgents') }}</span>
                                                    <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeAgentsDesc') }}</span>
                                                </span>
                                            </label>
                                            <div v-if="chatScope === 'agents' && reportAgents.length > 0" class="ms-6 space-y-1">
                                                <label v-for="agent in reportAgents" :key="agent.id" class="flex items-center gap-2 cursor-pointer">
                                                    <input type="checkbox" :value="agent.id" v-model="chatAgentIds" :disabled="!canSettings" />
                                                    <span class="text-xs text-gray-600 dark:text-gray-400">{{ agent.name }}</span>
                                                </label>
                                            </div>
                                            <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-data-only">
                                                <input type="radio" value="data_only" v-model="chatScope" class="mt-0.5" :disabled="!canSettings" />
                                                <span class="flex flex-col">
                                                    <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeDataOnly') }}</span>
                                                    <span class="text-[11px] text-gray-400">{{ $t('share.chatScopeDataOnlyDesc') }}</span>
                                                </span>
                                            </label>
                                            <p v-if="reportAgents.length === 0" class="text-[11px] text-gray-400">{{ $t('share.chatNoAgents') }}</p>
                                        </div>
                                        <div data-testid="chat-model">
                                            <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('share.chatModel') }}</div>
                                            <USelectMenu v-model="chatModelValue" :options="chatModelOptions" value-attribute="value"
                                                option-attribute="label" size="sm" :disabled="!canSettings" :ui="{ rounded: 'rounded-lg' }" />
                                            <p class="text-[11px] text-gray-400 mt-1">{{ $t('share.chatModelDesc') }}</p>
                                        </div>
                                    </div>
                                </div>

                                <!-- Whose credentials a viewer's run uses: only where it
                                     matters (per-user sources), off with the reason on RLS. -->
                                <div v-if="showRunIdentity" class="flex items-start justify-between gap-4 py-3" data-testid="settings-run-identity">
                                    <div class="min-w-0">
                                        <div class="text-sm text-gray-800 dark:text-gray-200">{{ $t('share.runOnBehalf') }}</div>
                                        <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ hasRls ? $t('share.runOnBehalfRlsDisabled') : $t('share.runOnBehalfDesc') }}</div>
                                        <div v-if="!hasRls && !canRunIdentity" class="text-[11px] text-gray-400 mt-1">{{ $t('dashboardSettings.ownerOnly') }}</div>
                                    </div>
                                    <UToggle v-model="runAsCreator" :disabled="hasRls || !canRunIdentity" class="flex-shrink-0 mt-0.5" />
                                </div>
                            </div>
                        </section>

                        <!-- The name in its link -->
                    </template>

                    <!-- The queries behind it; resources and analytics (read only).
                         Mounted on first open, then kept. -->
                    <div v-if="visited.data" v-show="tab === 'data'" class="h-full" data-testid="settings-data">
                        <DataModal embedded :report-id="reportId" :artifact-id="artifactId"
                            :artifact-viz-ids="artifactVizIds" :artifact-mode="artifactMode" />
                    </div>
                    <div v-if="visited.resources" v-show="tab === 'resources'" data-testid="settings-resources">
                        <ArtifactResourceExplorer embedded :artifact-id="dashboardId" view="resources" />
                    </div>
                    <div v-if="visited.analytics" v-show="tab === 'analytics'" data-testid="settings-analytics">
                        <ArtifactResourceExplorer embedded :artifact-id="dashboardId" view="analytics" />
                    </div>
                    <div v-if="visited.notes" v-show="tab === 'notes'" data-testid="settings-notes">
                        <HistoryNotes :dashboard-id="dashboardId" :can-edit="hasCap('settings')" />
                    </div>
                    <!-- History: one instance for its tabs; the menu picks the section. -->
                    <div v-if="visited.history" v-show="isHistoryTab" data-testid="settings-history">
                        <DashboardHistoryModal embedded :dashboard-id="dashboardId" :capabilities="capabilities || []"
                            :section="historySection" @rolled-back="emit('history-changed')" @transferred="emit('history-changed')"
                            @close="isOpen = false" />
                    </div>
                </div>

                <!-- Footer: General and Viewers save together -->
                <div v-if="isSettingsTab" class="flex items-center justify-end gap-2 px-6 py-4 border-t border-gray-100 dark:border-gray-800">
                    <span v-if="dirty" class="me-auto text-xs text-gray-500 dark:text-gray-400">{{ $t('dashboardSettings.unsaved') }}</span>
                    <button @click="requestClose" data-testid="dashboard-settings-cancel"
                        class="h-9 px-4 text-sm font-medium rounded-full text-blue-700 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950">
                        {{ $t('common.cancel') }}
                    </button>
                    <button @click="save" :disabled="saving || loading || !slugValid || !dirty"
                        data-testid="dashboard-settings-save"
                        class="inline-flex items-center gap-1.5 h-9 px-5 text-sm font-medium rounded-full text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed">
                        <Spinner v-if="saving" class="w-3.5 h-3.5" />
                        {{ $t('common.save') }}
                    </button>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import DataModal from './DataModal.vue'
import DashboardHistoryModal from './DashboardHistoryModal.vue'
import HistoryNotes from './history/HistoryNotes.vue'
import HistoryOwner from './history/HistoryOwner.vue'
import ArtifactResourceExplorer from '~/components/ArtifactResourceExplorer.vue'
import { buildRecurringCron, parseRecurringCron, type RecurInterval } from '@/composables/useScheduleBuilder'
import { refreshModeFromReport, refreshModeSettings, type RefreshMode } from '@/composables/useRefreshMode'

const props = defineProps<{
    // The conversation the dashboard was built in: its model and agents.
    reportId: string
    dashboardId: string
    // The version open in the editor, for the Data tab.
    artifactId?: string | null
    artifactVizIds?: string[]
    artifactMode?: string | null
    // Tabs the reader may open (the frame knows): resources, analytics.
    resourcesAvailable?: boolean
    showAnalytics?: boolean
    // Its name, and how to change it (the frame's rename; absent: not theirs).
    title?: string
    rename?: (title: string) => Promise<boolean>
}>()
const emit = defineEmits<{ (e: 'saved', settings: any): void; (e: 'history-changed'): void }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()
const { smtpEnabled } = useAppSettings()
const { getCronLabel } = useCronLabel()
const { formatDateTime } = useFormatDate()

const isOpen = ref(false)
const loading = ref(false)

// ---- Tabs ----
type HistoryTab = 'versions' | 'conversations'
type Tab = 'general' | 'viewers' | 'notes' | 'data' | 'resources' | 'analytics' | HistoryTab
const HISTORY_TABS: HistoryTab[] = ['versions', 'conversations']
const tab = ref<Tab>('general')
type TabItem = { key: Tab; group: number; icon: string; label: string; desc: string; groupLabel?: string }
const tabs = computed<TabItem[]>(() => {
    const list: TabItem[] = [
        { key: 'general', group: 1, icon: 'heroicons:adjustments-horizontal', label: t('dashboardSettings.tabGeneral'), desc: '' },
        { key: 'viewers', group: 1, icon: 'heroicons:eye', label: t('dashboardSettings.tabViewers'), desc: t('dashboardSettings.viewersDesc') },
        // The README every editor's agent reads (collab01).
        ...(capabilities.value?.includes('edit') ? [{ key: 'notes' as const, group: 1, icon: 'heroicons:document-text', label: t('dashboardCollab.history.tabNotes'), desc: t('dashboardSettings.notesDesc') }] : []),
        { key: 'data', group: 2, icon: 'heroicons:circle-stack', label: t('dashboardSettings.tabData'), desc: '' },
    ]
    if (props.resourcesAvailable) list.push({ key: 'resources', group: 2, icon: 'heroicons:server-stack', label: t('artifactResources.inspect'), desc: t('artifactResources.readOnlyLabel') })
    if (props.showAnalytics) list.push({ key: 'analytics', group: 2, icon: 'heroicons:chart-bar', label: t('artifactResources.analytics'), desc: t('artifactResources.readOnly') })
    // History (collab01): editors. Versions holds what is live and Restore.
    if (capabilities.value?.includes('edit')) {
        const h = { group: 3, groupLabel: t('dashboardCollab.history.title') }
        list.push(
            { key: 'versions', icon: 'heroicons:clock', label: t('dashboardCollab.history.tabVersions'), desc: t('dashboardSettings.versionsDesc'), ...h },
            { key: 'conversations', icon: 'heroicons:chat-bubble-left-right', label: t('dashboardCollab.history.tabConversations'), desc: t('dashboardSettings.conversationsDesc'), ...h },
        )
    }
    return list
})
const currentTab = computed(() => tabs.value.find(x => x.key === tab.value) || tabs.value[0])
const isSettingsTab = computed(() => tab.value === 'general' || tab.value === 'viewers')
const isHistoryTab = computed(() => (HISTORY_TABS as string[]).includes(tab.value))
const historySection = computed(() => (isHistoryTab.value ? tab.value as HistoryTab : undefined))
// Data, resources, analytics and history load when first opened.
const visited = reactive<Record<string, boolean>>({})
const visitKey = (k: Tab) => ((HISTORY_TABS as string[]).includes(k) ? 'history' : k)
watch([tab, isOpen], () => { if (isOpen.value) visited[visitKey(tab.value)] = true })
const saving = ref(false)

// What the reader may change (the server decides; null: not known yet).
const capabilities = ref<string[] | null>(null)
const hasCap = (cap: string) => capabilities.value === null || capabilities.value.includes(cap)
const canSettings = computed(() => hasCap('settings'))
const canRunIdentity = computed(() => hasCap('run_identity'))

// ---- Refresh ----
const refreshModeOptions = computed(() => [
    { value: 'off' as const, label: t('dashboardSettings.refreshOff') },
    { value: 'recurring' as const, label: t('dashboardSettings.refreshRecurring') },
    { value: 'on_open' as const, label: t('dashboardSettings.refreshOnOpen') },
])
const refreshMode = ref<RefreshMode>('off')
const recurInterval = ref<RecurInterval>('day')
const recurEveryN = ref(15)
const recurHour = ref(8)
const recurMinute = ref(0)
const recurDays = ref<number[]>([1])
const recurDayOfMonth = ref(1)
const lastRunAt = ref<string | null>(null)
const weekdays = computed(() => ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].map((d, value) => ({
    value, label: t(`scheduledPrompt.dow${d}`), short: t(`scheduledPrompt.dow${d}Short`),
})))
function toggleRecurDay(value: number) {
    if (!recurDays.value.includes(value)) recurDays.value = [...recurDays.value, value].sort((a, b) => a - b)
    else if (recurDays.value.length > 1) recurDays.value = recurDays.value.filter(d => d !== value)
}
const currentCron = () => buildRecurringCron({
    interval: recurInterval.value, everyN: recurEveryN.value, hour: recurHour.value,
    minute: recurMinute.value, days: recurDays.value, dayOfMonth: recurDayOfMonth.value,
})
const scheduleLabel = computed(() => getCronLabel(currentCron()))
const recurTime = computed({
    get: () => `${String(recurHour.value).padStart(2, '0')}:${String(recurMinute.value).padStart(2, '0')}`,
    set: (value: string) => {
        const m = /^(\d{1,2}):(\d{2})/.exec(value || '')
        if (!m || Number(m[1]) > 23 || Number(m[2]) > 59) return
        recurHour.value = Number(m[1])
        recurMinute.value = Number(m[2])
    },
})
function hydrateCron(cron: string | null | undefined) {
    recurInterval.value = 'day'; recurEveryN.value = 15; recurHour.value = 8; recurMinute.value = 0
    recurDays.value = [1]; recurDayOfMonth.value = 1
    const patch = cron ? parseRecurringCron(cron) : null
    if (patch) Object.entries({
        interval: recurInterval, everyN: recurEveryN, hour: recurHour, minute: recurMinute,
        days: recurDays, dayOfMonth: recurDayOfMonth,
    }).forEach(([k, r]) => { if ((patch as any)[k] !== undefined) (r as any).value = (patch as any)[k] })
}

// ---- Subscribers (emailed after each scheduled run) ----
type Subscriber = { type: 'user'; id: string } | { type: 'email'; address: string }
const subscribers = ref<Subscriber[]>([])
const members = ref<{ id: string; name: string; email: string }[]>([])
const subInput = ref('')
const subDropdown = ref(false)
const memberName = (id: string) => { const m = members.value.find(x => x.id === id); return m ? (m.name || m.email) : id }
const subEmails = computed(() => subscribers.value.map(s => s.type === 'email' ? s.address : members.value.find(m => m.id === s.id)?.email))
const subMatches = computed(() => {
    const q = subInput.value.toLowerCase().trim()
    if (!q) return []
    return members.value.filter(m => !subEmails.value.includes(m.email)
        && (m.email.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))).slice(0, 5)
})
const isEmail = (s: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s)
function addSubEmail(raw: string) {
    const clean = raw.trim().toLowerCase()
    if (clean && isEmail(clean) && !subEmails.value.includes(clean)) {
        subscribers.value.push({ type: 'email', address: clean })
        subInput.value = ''
    }
}
function addSubMember(m: { id: string }) {
    if (!subscribers.value.some(s => s.type === 'user' && s.id === m.id)) subscribers.value.push({ type: 'user', id: m.id })
    subInput.value = ''
    subDropdown.value = false
}
const subEnter = () => (subMatches.value.length ? addSubMember(subMatches.value[0]) : addSubEmail(subInput.value))
const subBlur = () => setTimeout(() => { subDropdown.value = false; if (isEmail(subInput.value)) addSubEmail(subInput.value) }, 200)

// ---- What viewers get ----
const includeDataTab = ref(true)
const chatEnabled = ref(false)
const chatScope = ref<'agents' | 'data_only'>('agents')
const chatAgentIds = ref<string[]>([])
const reportAgents = ref<{ id: string; name: string }[]>([])
// Stored chat model: null = the conversation's own, ORG_DEFAULT = the
// organization's, else a model id. USelectMenu reads '' as nothing picked,
// so "the conversation's" carries its own sentinel.
const INHERIT_MODEL = '__inherit__'
const ORG_DEFAULT_MODEL = 'org_default'
const chatModelId = ref(INHERIT_MODEL)
const chatModels = ref<{ id: string; name: string; provider?: string; isDefault?: boolean }[]>([])
const reportModelId = ref('')
const chatModelValue = computed({
    // Without a conversation model, inheriting is the organization default: one option.
    get: () => (!reportModelId.value && chatModelId.value === INHERIT_MODEL ? ORG_DEFAULT_MODEL : chatModelId.value),
    set: (v: string) => { chatModelId.value = v },
})
const chatModelOptions = computed(() => {
    const orgDefault = chatModels.value.find(m => m.isDefault)
    const options = [{
        value: ORG_DEFAULT_MODEL,
        label: orgDefault ? t('share.chatModelOrgDefault', { name: orgDefault.name }) : t('share.chatModelOrgDefaultPlain'),
    }]
    if (reportModelId.value) {
        const inherited = chatModels.value.find(m => m.id === reportModelId.value)
        options.unshift({ value: INHERIT_MODEL, label: inherited ? t('share.chatModelReport', { name: inherited.name }) : t('share.chatModelReportPlain') })
    }
    options.push(...chatModels.value.map(m => ({ value: m.id, label: m.provider ? `${m.name} · ${m.provider}` : m.name })))
    if (![INHERIT_MODEL, ORG_DEFAULT_MODEL].includes(chatModelId.value) && !chatModels.value.some(m => m.id === chatModelId.value)) {
        options.push({ value: chatModelId.value, label: t('share.chatModelUnavailable') })
    }
    return options
})
const runAsCreator = ref(false)
const hasRls = ref(false)
const hasUserScoped = ref(false)
const showRunIdentity = computed(() => hasUserScoped.value || hasRls.value)

// ---- Owner ----
const ownerId = ref<string | null>(null)
const ownerPerson = computed(() => members.value.find(m => m.id === ownerId.value) || null)
const transferOpen = ref(false)
async function onTransferred() {
    transferOpen.value = false
    emit('history-changed')
    await load()
}

// ---- Name ----
const nameDraft = ref('')
const nameChanged = computed(() => !!props.rename && !!nameDraft.value.trim() && nameDraft.value.trim() !== (props.title || ''))

// ---- Link name ----
const slugDraft = ref('')
const SLUG_RULE = /^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$/
const slugValid = computed(() => !slugDraft.value || SLUG_RULE.test(slugDraft.value))
watch(slugDraft, (v) => { if (v !== v.toLowerCase()) slugDraft.value = v.toLowerCase() })
const linkPrefix = computed(() => `${window.location.origin}/d/`)
// The add-on shows the host without the scheme; the full link is below.
const linkPrefixShort = computed(() => `${window.location.host}/d/`)
// The dashboard's name as a link name, offered when the field is empty.
const slugSuggestion = computed(() => {
    const s = nameDraft.value.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
        .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80).replace(/-+$/, '')
    return SLUG_RULE.test(s) ? s : ''
})
// What the link will be once saved: the name if valid, else the id.
const effectiveLink = computed(() => `${linkPrefix.value}${slugDraft.value && slugValid.value ? slugDraft.value : props.dashboardId}`)
const linkCopied = ref(false)
async function copyEffectiveLink() {
    try {
        await navigator.clipboard.writeText(effectiveLink.value)
        linkCopied.value = true
        setTimeout(() => { linkCopied.value = false }, 2000)
    } catch { /* clipboard refused: nothing to undo */ }
}

// ---- Load / save ----
// What was read on open: the save sends only what changed, so an editor
// never sends (and is never refused for) a setting only the owner changes.
const saved = ref<Record<string, any>>({})

function chatIdsPayload(): string[] | null {
    if (chatScope.value === 'data_only') return []
    const all = reportAgents.value.map(a => a.id)
    const picked = chatAgentIds.value.filter(id => all.includes(id))
    // Everything checked = inherit the roster (["*"] resets it server-side).
    return picked.length === all.length && all.length > 0 ? ['*'] : picked
}

function current(): Record<string, any> {
    const mode = refreshModeSettings(refreshMode.value, currentCron())
    return {
        cron_schedule: mode.cron_expression,
        refresh_on_view: mode.refresh_on_view,
        notification_subscribers: refreshMode.value === 'recurring' ? subscribers.value : [],
        include_data_tab: includeDataTab.value,
        chat_enabled: chatEnabled.value,
        chat_data_source_ids: chatIdsPayload(),
        chat_model_id: chatModelId.value === INHERIT_MODEL ? '' : chatModelId.value,
        shared_run_identity: runAsCreator.value ? 'creator' : 'viewer',
        slug: slugDraft.value.trim(),
    }
}
const changes = computed(() => {
    const now = current()
    return Object.fromEntries(Object.entries(now).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(saved.value[k])))
})
const settingsDirty = computed(() => canSettings.value && Object.keys(changes.value).length > 0)
const dirty = computed(() => settingsDirty.value || nameChanged.value)

async function load() {
    const id = props.dashboardId
    loading.value = true
    try {
        const [settingsRes, statusRes, reportRes, agentsRes, modelsRes, membersRes, sharingRes] = await Promise.all([
            useMyFetch(`/dashboards/${id}/settings`),
            useMyFetch(`/dashboards/${id}/publish`),
            useMyFetch(`/reports/${props.reportId}`),
            useMyFetch(`/reports/${props.reportId}/artifact_chat/agents`),
            useMyFetch('/llm/models?is_enabled=true'),
            useMyFetch('/organization/members'),
            useMyFetch(`/dashboards/${id}/sharing`),
            loadCapabilities(id),
        ])
        if (id !== props.dashboardId) return
        if (settingsRes.error.value) throw settingsRes.error.value
        const st = (settingsRes.data.value || {}) as any
        const report = (reportRes.data.value || {}) as any
        ownerId.value = ((sharingRes.data.value || {}) as any).owner_id || null
        members.value = ((membersRes.data.value as any[]) || []).map((u: any) => ({ id: u.id, name: u.name || '', email: u.email }))
        refreshMode.value = refreshModeFromReport(st)
        hydrateCron(st.cron_schedule)
        subscribers.value = (st.notification_subscribers || []).map((x: any) => ({ ...x }))
        const status = (statusRes.data.value || {}) as any
        lastRunAt.value = status.published ? (status.last_run?.at || null) : (report.last_run_at || null)

        includeDataTab.value = st.include_data_tab !== false
        chatEnabled.value = st.chat_enabled === true
        reportAgents.value = (((agentsRes.data.value as any)?.agents) || report.data_sources || []).map((a: any) => ({ id: a.id, name: a.name }))
        const stored = st.chat_data_source_ids
        chatScope.value = Array.isArray(stored) && stored.length === 0 ? 'data_only'
            : (stored == null && reportAgents.value.length === 0 ? 'data_only' : 'agents')
        chatAgentIds.value = Array.isArray(stored) && stored.length > 0 ? [...stored] : reportAgents.value.map(a => a.id)
        chatModelId.value = st.chat_model_id || INHERIT_MODEL
        reportModelId.value = report.model_id || ''
        chatModels.value = ((modelsRes.data.value as any[]) || []).map((m: any) => ({
            id: m.id, name: m.name || m.model_id, provider: m.provider?.name, isDefault: !!m.is_default,
        }))
        runAsCreator.value = st.shared_run_identity === 'creator'
        hasRls.value = !!report.has_rls
        hasUserScoped.value = !!report.has_user_scoped
        slugDraft.value = st.slug || ''
        nameDraft.value = props.title || ''
        saved.value = current()
    } catch (e) {
        toast.add({ title: t('dashboardSettings.loadFailed'), description: getErrorMessage(e), color: 'red' })
        isOpen.value = false
    } finally {
        loading.value = false
    }
}

async function loadCapabilities(id: string) {
    capabilities.value = null
    try {
        let res = await useMyFetch(`/d/${id}/draft`)
        if (res.error.value || !res.data.value) res = await useMyFetch(`/d/${id}`)
        const caps = (res.data.value as any)?.capabilities
        if (id === props.dashboardId) capabilities.value = !res.error.value && Array.isArray(caps) ? caps : null
    } catch { /* the server decides */ }
}

async function save() {
    if (!dirty.value || !slugValid.value) return
    saving.value = true
    try {
        // The name first (the frame's rename says how it went), then the rest.
        if (nameChanged.value && !(await props.rename!(nameDraft.value.trim()))) return
        if (settingsDirty.value) {
            const res = await useMyFetch(`/dashboards/${props.dashboardId}/settings`, { method: 'PUT', body: changes.value })
            if (res.error.value) throw res.error.value
            toast.add({ title: t('dashboardSettings.saved'), color: 'green' })
            emit('saved', res.data.value)
        }
        isOpen.value = false
    } catch (e) {
        toast.add({ title: t('dashboardSettings.saveFailed'), description: getErrorMessage(e), color: 'red' })
    } finally {
        saving.value = false
    }
}

// Closing with unsaved changes asks first (Cancel, X, Esc, outside click).
function requestClose() {
    if (dirty.value && !window.confirm(t('dashboardSettings.discardConfirm'))) return
    isOpen.value = false
}

function open(at: Tab = 'general') {
    tab.value = at
    transferOpen.value = false
    for (const k of Object.keys(visited)) delete visited[k]
    isOpen.value = true
    visited[visitKey(at)] = true
    load()
}
defineExpose({ open })
</script>
