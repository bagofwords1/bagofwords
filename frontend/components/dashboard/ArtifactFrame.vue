<template>
  <div class="h-full w-full flex flex-col bg-white dark:bg-gray-900">
    <!-- Header / Toolbar -->
    <!-- Wraps rather than clips: with presence, Publish and Share the
         right-hand group can outgrow a narrow pane. -->
    <div v-if="!verificationPreview" class="flex-shrink-0 flex flex-wrap items-center justify-between gap-x-2 gap-y-2 px-4 py-2 bg-gradient-to-b from-cyan-50/50 dark:from-cyan-900/10 to-white dark:to-gray-900 border-b border-gray-200 dark:border-gray-700/60">
      <div class="flex items-center gap-3 min-w-0 grow basis-52">
        <UTooltip :text="$t('artifactFrame.backToChat')">
          <button @click="$emit('close')" class="hover:bg-gray-100 dark:hover:bg-gray-700 p-1 rounded">
            <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
          </button>
        </UTooltip>

        <!-- Artifact Selector Dropdown -->
        <div class="flex items-center gap-2 min-w-0 flex-1">
          <!-- Rename (owner only): the selector becomes an input in place so
               the toolbar keeps its width. Enter saves, Escape cancels. -->
          <form
            v-if="isRenaming"
            class="flex items-center gap-1"
            @submit.prevent="saveRename"
          >
            <UInput
              ref="renameInputRef"
              v-model="renameDraft"
              size="xs"
              class="min-w-[280px]"
              :maxlength="255"
              :disabled="isSavingRename"
              :placeholder="tm('artifactFrame.renamePlaceholder', selectedArtifact?.mode)"
              @keydown.escape.prevent="cancelRename"
            />
            <UTooltip :text="$t('common.save')">
              <button
                type="submit"
                :disabled="isSavingRename || !renameDraft.trim()"
                class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
              >
                <Spinner v-if="isSavingRename" class="w-3.5 h-3.5 text-gray-500" />
                <Icon v-else name="heroicons:check" class="w-3.5 h-3.5 text-emerald-600" />
              </button>
            </UTooltip>
            <UTooltip :text="$t('common.cancel')">
              <button
                type="button"
                :disabled="isSavingRename"
                @click="cancelRename"
                class="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
              >
                <Icon name="heroicons:x-mark" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
              </button>
            </UTooltip>
          </form>
          <!-- One control: "<dashboard> · v3". Its menu lists the versions,
               grouped under each dashboard when this conversation holds more
               than one; Live marks what viewers see. Whether the open version
               is live is said once, by the Publish button. -->
          <USelectMenu
            v-else-if="artifactsList.length > 0"
            :model-value="pickerValue"
            :options="pickerOptions"
            value-attribute="value"
            option-attribute="label"
            size="xs"
            class="max-w-[320px] min-w-0"
            data-testid="frame-version-picker"
            @update:model-value="onPick"
            :popper="{ placement: 'bottom-start' }"
            :ui-menu="{ width: 'w-80', container: 'z-50 group' }"
            :ui="{ option: { base: 'py-1.5' } }"
          >
            <template #label>
              <span class="flex items-center gap-1.5 min-w-0 text-xs">
                <Icon :name="modeIcon(selectedArtifactItem?.mode)" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" />
                <span class="truncate">{{ selectedDashboardTitle }}</span>
                <span class="text-ink-muted shrink-0">·</span>
                <Icon v-if="viewing" name="heroicons:eye" class="w-3.5 h-3.5 shrink-0 text-blue-500" />
                <span class="font-medium tabular-nums shrink-0">v{{ viewing ? viewing.version : selectedArtifactItem?.version }}</span>
              </span>
            </template>
            <template #option="{ option }">
              <!-- A dashboard's heading (only with several dashboards) -->
              <div v-if="option.header" class="w-full min-w-0 pt-1" data-testid="frame-dashboard-heading">
                <div class="flex items-center gap-1.5 text-xs font-medium text-gray-900 dark:text-white min-w-0">
                  <Icon :name="modeIcon(option.mode)" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" />
                  <span class="truncate">{{ option.title }}</span>
                </div>
                <div class="text-2xs text-ink-muted ps-5">{{ $t('dashboardCollab.versions.createdOn', { when: formatRelativeTime(option.created_at) }) }}</div>
              </div>
              <div v-else-if="option.allVersions" class="flex items-center gap-1.5 w-full text-xs text-blue-600 dark:text-blue-400" data-testid="frame-version-all">
                <Icon name="heroicons:clock" class="w-3.5 h-3.5" />{{ $t('dashboardCollab.versions.allVersions') }}
              </div>
              <div v-else class="flex flex-col w-full min-w-0" :class="option.grouped ? 'ps-5' : ''"
                :data-testid="option.elsewhere ? 'frame-version-elsewhere' : 'frame-version-row'">
                <span class="flex items-center gap-1.5 text-xs min-w-0">
                  <span class="font-medium tabular-nums">v{{ option.version }}</span>
                  <span v-if="option.name" class="truncate text-gray-700 dark:text-gray-300">“{{ option.name }}”</span>
                  <span v-if="option.live" class="px-1.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300 text-2xs font-medium">{{ $t('dashboardCollab.versions.live') }}</span>
                </span>
                <span class="text-2xs text-ink-muted">
                  <template v-if="option.elsewhere">{{ $t('dashboardCollab.versions.elsewhereBy', { name: option.author || $t('dashboardCollab.history.someone') }) }} · </template>{{ formatRelativeTime(option.created_at) }}
                </span>
              </div>
            </template>
          </USelectMenu>
          <span v-else class="text-xs text-ink-muted italic">No artifacts yet</span>

          <!-- A run elsewhere created a new artifact: offer it, never hijack -->
          <button
            v-if="offeredArtifactId"
            @click="acceptOfferedArtifact"
            class="text-xs px-2 py-1 bg-emerald-50 dark:bg-emerald-950 text-emerald-600 hover:bg-emerald-100 rounded border border-emerald-200 transition-colors flex items-center gap-1"
          >
            <Icon name="heroicons:sparkles" class="w-3 h-3" />
            New artifact — view
          </button>

        </div>
      </div>

      <div class="flex flex-wrap items-center justify-end gap-2 ms-auto shrink-0 max-w-full">
        <span v-if="isLoading" class="text-xs text-ink-muted">{{ t('artifactFrame.loading') }}</span>

        <!-- Refresh Dashboard (rerun + refresh). Everyday action, so it stays
             a visible button.
             Disabled while previewing as someone else: neither refresh
             endpoint takes run_as_user_id, so the rerun would resolve
             identity params as YOU and replace what is on screen while the
             banner still says "Viewing as X". -->
        <UTooltip v-if="!viewing" :text="isViewAsActive ? $t('artifactFrame.refreshDisabledViewAs') : $t('artifactFrame.refreshData')">
          <button
            @click="refreshDashboard"
            :disabled="isRefreshing || isViewAsActive"
            class="p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Spinner v-if="isRefreshing" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
            <Icon v-else name="heroicons:arrow-path" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
          </button>
        </UTooltip>

        <!-- Settings stay visible next to Refresh: one window with a tab
             each for the schedule, what viewers get, the link, the queries
             behind it (Data), its resources and its analytics. Who has
             access is Share's. -->
        <DashboardSettingsModal v-if="report && selectedArtifact?.artifact_id" ref="settingsModalRef" :report-id="reportId"
          :dashboard-id="selectedArtifact.artifact_id"
          :artifact-id="selectedArtifact.id"
          :artifact-viz-ids="selectedArtifact.content?.visualization_ids || []"
          :artifact-mode="selectedArtifact.mode"
          :title="currentArtifactTitle()"
          :rename="canRename ? renameTo : undefined"
          :resources-available="resourcesAvailable && !verificationPreview"
          :show-analytics="(isReportOwner || dashboardCaps.includes('analytics')) && !verificationPreview"
          @history-changed="onHistoryChanged" />

        <!-- Everything used less than daily lives behind one ⋯ with labelled
             rows: rename, every export (download), full screen, new tab.
             Icon-only buttons for all of these made the bar unreadable. -->
        <UDropdown
          v-if="moreMenuItems.length"
          :items="moreMenuItems"
          :popper="{ placement: 'bottom-end' }"
          :ui="{ width: 'w-auto min-w-[11rem]', item: { padding: 'px-2 py-1.5' }, container: 'z-50 group' }"
        >
          <UTooltip :text="$t('artifactFrame.more')">
            <button
              type="button"
              :aria-label="$t('artifactFrame.more')"
              class="p-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors"
            >
              <Spinner v-if="isExporting" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
              <Icon v-else name="heroicons:ellipsis-horizontal" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
            </button>
          </UTooltip>
          <template #item="{ item }">
            <div class="flex items-center gap-2 text-start w-full" :data-testid="item.testid">
              <UIcon :name="item.icon" class="w-4 h-4 shrink-0 text-ink-muted" />
              <span class="text-xs font-medium text-gray-900 dark:text-white">{{ item.label }}</span>
            </div>
          </template>
        </UDropdown>

        <!-- View as (page artifacts only): preview the dashboard as another
             viewer — anonymous or any org member, searchable by name/email.
             Identity-only — data is unaffected. -->
        <USelectMenu
          v-if="canViewAs && !viewing"
          v-model="viewAsMode"
          :options="viewAsOptions"
          value-attribute="value"
          option-attribute="label"
          searchable
          :search-attributes="['label', 'email']"
          :searchable-placeholder="$t('artifactFrame.searchMembers')"
          size="xs"
          class="min-w-[130px]"
          :ui-menu="{ container: 'z-50 group' }"
        >
          <template #label>
            <span class="flex items-center gap-1 text-xs" :class="viewAsMode !== 'you' ? 'text-indigo-600 font-medium' : ''">
              <Icon name="heroicons:eye" class="w-3.5 h-3.5" />
              {{ viewAsLabel }}
            </span>
          </template>
          <template #option="{ option }">
            <div class="flex flex-col">
              <span class="text-xs">{{ option.label }}</span>
              <span v-if="option.email" class="text-2xs text-ink-muted">{{ option.email }}</span>
            </div>
          </template>
        </USelectMenu>
        <!-- Delegated-source caveat, visible before a target is even picked -->
        <UTooltip
          v-if="canViewAs && credentialScoped && !viewing"
          :text="$t('artifactFrame.viewAsTooltip')"
          :popper="{ placement: 'bottom' }"
        >
          <Icon name="heroicons:information-circle" class="w-3.5 h-3.5 text-amber-500" />
        </UTooltip>

        <!-- Who else is editing this dashboard right now (collab01) -->
        <div v-if="presenceOthers.length" data-testid="frame-presence">
          <PresenceAvatars :others="presenceOthers" />
        </div>

        <!-- Share Dashboard -->
        <ShareModal v-if="report && selectedArtifact?.artifact_id" :report="report" share-type="artifact" :title="tm('share.shareDashboard', selectedArtifact?.mode)"
          :artifact-mode="selectedArtifact.mode"
          :item-name="selectedArtifact.title" :dashboard-id="selectedArtifact.artifact_id" />

        <!-- Publish (the version open here), last: the toolbar's main action.
             "Review changes" (someone published since) opens the draft page's
             changes view. -->
        <PublishButton v-if="report && selectedArtifact && !viewing" :key="publishKey" :artifact="selectedArtifact" @review="reviewChanges"
          @status="publishStatus = $event" @open-version="openVersionById" />
      </div>
    </div>

    <!-- Live moved on past this conversation's own unpublished work: offer
         to look at it (nothing is copied until "Edit here"). -->
    <div v-if="liveAhead && !verificationPreview" class="flex-shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2 text-xs bg-amber-50 dark:bg-amber-950/40 border-b border-amber-200 dark:border-amber-900 text-amber-900 dark:text-amber-200"
      data-testid="frame-live-ahead">
      <UIcon name="i-heroicons-arrow-path-rounded-square" class="w-4 h-4 shrink-0" />
      <span class="flex-1 min-w-0">{{ $t('dashboardCollab.versions.liveAhead', { name: liveAhead.live.by?.name || $t('dashboardCollab.history.someone'), live: liveAhead.live.version, here: liveAhead.newest_here?.version }) }}</span>
      <button type="button" class="font-medium underline underline-offset-2" data-testid="frame-live-ahead-view" @click="viewLiveAhead">
        {{ $t('dashboardCollab.versions.viewLive', { n: liveAhead.live.version }) }}
      </button>
      <button type="button" class="text-amber-700 dark:text-amber-300 hover:underline" data-testid="frame-live-ahead-keep" @click="keepWorkingHere">
        {{ $t('dashboardCollab.versions.keepWorking', { n: liveAhead.newest_here?.version }) }}
      </button>
    </div>

    <!-- Viewing a version made in another conversation (or live, when it
         moved on): shown read-only, nothing copied. "Edit here" copies it in
         (its charts run again as you) and the next edit builds on it. -->
    <div v-if="viewing && !verificationPreview" class="flex-shrink-0 flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2 text-xs bg-blue-50 dark:bg-blue-950/40 border-b border-blue-200 dark:border-blue-900 text-blue-900 dark:text-blue-200"
      data-testid="frame-viewing-bar">
      <UIcon name="i-heroicons-eye" class="w-4 h-4 shrink-0" />
      <span class="flex-1 min-w-0">
        {{ viewing.live
          ? $t('dashboardCollab.versions.viewingLiveBar', { n: viewing.version, here: selectedArtifactItem?.version })
          : $t('dashboardCollab.versions.viewingBar', { n: viewing.version, name: viewing.author || $t('dashboardCollab.history.someone'), here: selectedArtifactItem?.version }) }}
      </span>
      <button type="button" class="inline-flex items-center gap-1 font-medium underline underline-offset-2 disabled:opacity-50" :disabled="bringing" data-testid="frame-viewing-edit" @click="editViewing">
        <Spinner v-if="bringing" class="w-3 h-3" />
        {{ $t('dashboardCollab.versions.editHere', { n: viewing.version }) }}
      </button>
      <button v-if="selectedArtifactItem" type="button" class="text-blue-700 dark:text-blue-300 hover:underline" data-testid="frame-viewing-back" @click="viewing = null">
        {{ $t('dashboardCollab.versions.backTo', { n: selectedArtifactItem.version }) }}
      </button>
    </div>

    <!-- Iframe Container -->
    <div ref="polishContainerRef" class="flex-1 min-h-0 relative bg-white dark:bg-gray-900">
      <!-- The version being viewed, as its preview page draws it. -->
      <iframe v-if="viewingSrc" :key="viewingSrc" :src="viewingSrc" :title="$t('dashboardCollab.history.preview')"
        class="absolute inset-0 z-[35] w-full h-full border-0 bg-white dark:bg-gray-900" data-testid="frame-viewing" />
      <!-- This conversation's agent is changing a dashboard others edit too
           (collab01): outline it, as Figma does, with whose agent it is. -->
      <div
        v-if="showAgentOutline"
        class="absolute inset-0 z-[25] pointer-events-none rounded-sm ring-2 ring-inset ring-violet-500/80 dark:ring-violet-400/80"
        data-testid="agent-editing-outline"
      >
        <span class="absolute top-0 end-0 flex items-center gap-1 rounded-es-md bg-violet-600 text-white ps-2 pe-2.5 py-0.5 text-2xs font-medium shadow-sm">
          <Icon name="heroicons:sparkles" class="w-3 h-3" />
          {{ $t('dashboardCollab.presence.agentEditing', { name: agentOwnerFirstName }) }}
        </span>
      </div>
      <!-- View-as switch in flight: cover the pane with a spinner until the
           new identity (and any identity-scoped re-runs) fully settle, so the
           transient anonymous fallback never flashes as the final render. -->
      <div
        v-if="previewLoading"
        class="absolute inset-0 z-30 flex flex-col items-center justify-center gap-3 bg-white/70 dark:bg-gray-900/70 backdrop-blur-[1px]"
      >
        <Spinner class="w-6 h-6 text-indigo-500" />
        <span class="text-xs text-gray-500 dark:text-gray-400">Switching view...</span>
      </div>

      <!-- View-as banner: keep the simulated view unmistakable, and be honest
           about its scope (identity only — data is not re-scoped). -->
      <div
        v-if="viewAsMode !== 'you' && canViewAs"
        class="absolute top-2 left-1/2 -translate-x-1/2 z-20 flex flex-col items-center gap-1 max-w-[90%]"
      >
        <!-- Failed member fetch: never claim "Viewing as X" over an anonymous
             payload — say what actually happened and offer a retry. -->
        <div
          v-if="previewViewerFailed"
          class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-amber-500 text-white shadow-lg text-xs"
        >
          <Icon name="heroicons:exclamation-triangle" class="w-3.5 h-3.5" />
          <span>Couldn't load {{ viewAsLabel }}'s identity — showing anonymous view</span>
          <button @click="retryPreviewViewer" class="font-semibold underline underline-offset-2 hover:text-amber-100">Retry</button>
          <button @click="viewAsMode = 'you'" class="font-semibold underline underline-offset-2 hover:text-amber-100">Exit</button>
        </div>
        <div v-else class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-indigo-600 text-white shadow-lg text-xs">
          <Icon name="heroicons:eye" class="w-3.5 h-3.5" />
          <span>Viewing as {{ viewAsLabel }}</span>
          <button @click="viewAsMode = 'you'" class="font-semibold underline underline-offset-2 hover:text-indigo-200">Exit</button>
        </div>
        <!-- Delegated sources can't be impersonated: the preview binds this
             user's identity parameters, but per-user-credential sources
             (e.g. Power BI) still run as YOU — their rows may differ. -->
        <div
          v-if="credentialScoped"
          class="flex items-center gap-1.5 px-3 py-1 rounded-full bg-amber-50 dark:bg-amber-900/40 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-200 shadow text-2xs"
        >
          <Icon name="heroicons:exclamation-triangle" class="w-3.5 h-3.5 shrink-0" />
          <span>Identity parameters only — sources that authenticate per user still run with your credentials, so this user's actual rows may differ.</span>
        </div>
      </div>
      <!-- Some charts withheld from this viewer: the dashboard still renders
           the ones they can read; say which are missing and why. -->
      <div
        v-if="partiallyWithheld && !isLoading"
        class="absolute top-2 left-1/2 -translate-x-1/2 z-20 max-w-[90%] flex items-start gap-1.5 px-3 py-1.5 rounded-lg bg-amber-50 dark:bg-amber-900/40 border border-amber-200 dark:border-amber-700 text-amber-800 dark:text-amber-200 shadow text-2xs"
      >
        <Icon name="heroicons:lock-closed" class="w-3.5 h-3.5 shrink-0 mt-0.5" />
        <div>
          <div v-if="withheldCharts.some(c => c.noAccess)">
            {{ $t('artifactFrame.partialNoAccess', { charts: withheldCharts.filter(c => c.noAccess).map(c => c.title).join(', ') }) }}
          </div>
          <div v-if="withheldCharts.some(c => !c.noAccess)">
            {{ $t('artifactFrame.partialFailed', { charts: withheldCharts.filter(c => !c.noAccess).map(c => c.title).join(', ') }) }}
          </div>
        </div>
      </div>

      <!-- Brought in from elsewhere (bring / Edit here / fork): the copied
           queries are running again as this user and have no rows yet. Cover
           the dashboard until they finish instead of letting it render empty. -->
      <div
        v-if="hydration && !isLoading && hasArtifact && !isPendingArtifact && !showViewerGate"
        data-testid="artifact-hydration"
        class="absolute inset-0 z-30 flex items-center justify-center overflow-y-auto py-8 px-4 bg-white/95 dark:bg-gray-900/95"
      >
        <div v-if="hydration.phase === 'running'" class="flex flex-col items-center gap-3 text-center max-w-sm">
          <Spinner class="w-6 h-6 text-gray-400" />
          <div class="text-sm text-gray-700 dark:text-gray-200">{{ t('artifactFrame.hydrating.title') }}</div>
          <div class="text-xs text-gray-500 dark:text-gray-400">
            {{ t('artifactFrame.hydrating.progress', { done: hydration.done, total: hydration.total }) }}
          </div>
          <div class="w-48 h-1.5 rounded-full bg-gray-100 dark:bg-gray-800 overflow-hidden">
            <div class="h-full bg-blue-500 transition-all" :style="{ width: `${hydration.total ? Math.round(100 * hydration.done / hydration.total) : 0}%` }" />
          </div>
        </div>
        <div v-else-if="hydration.phase === 'timeout'" class="flex flex-col items-center gap-3 text-center max-w-sm">
          <Icon name="heroicons:clock" class="w-6 h-6 text-amber-500" />
          <div class="text-sm text-gray-700 dark:text-gray-200">{{ t('artifactFrame.hydrating.timeout') }}</div>
          <div class="flex gap-2">
            <UButton size="xs" color="gray" variant="soft" @click="reloadAfterHydration">{{ t('artifactFrame.hydrating.reload') }}</UButton>
            <UButton size="xs" color="gray" variant="ghost" @click="dismissHydration">{{ t('artifactFrame.hydrating.showAnyway') }}</UButton>
          </div>
        </div>
        <div v-else class="flex flex-col gap-3 w-full max-w-md">
          <div class="flex items-start gap-2">
            <Icon name="heroicons:exclamation-triangle" class="w-5 h-5 shrink-0 text-amber-500 mt-0.5" />
            <div class="text-sm text-gray-800 dark:text-gray-100">
              {{ t('artifactFrame.hydrating.failedTitle', { failed: hydration.failed.length, total: hydration.total }) }}
            </div>
          </div>
          <ul class="flex flex-col gap-2">
            <li v-for="f in hydration.failed" :key="f.queryId"
              class="rounded-md border border-gray-200 dark:border-gray-700 px-3 py-2">
              <div class="text-xs font-medium text-gray-800 dark:text-gray-100">{{ f.title || t('artifactFrame.dataUntitledQuery') }}</div>
              <div class="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5 break-words whitespace-pre-line">
                {{ f.noAccess ? t('artifactFrame.hydrating.noAccess') : (f.reason || t('artifactFrame.hydrating.noReason')) }}
              </div>
            </li>
          </ul>
          <div v-if="hydration.rerunError" class="text-[11px] text-red-600 dark:text-red-400 break-words">{{ hydration.rerunError }}</div>
          <div class="flex gap-2">
            <UButton size="xs" color="blue" :loading="hydration.rerunning" data-testid="hydration-run-again" @click="runFailedAgain">
              {{ t('artifactFrame.hydrating.runAgain') }}
            </UButton>
            <UButton size="xs" color="gray" variant="ghost" @click="dismissHydration">{{ t('artifactFrame.hydrating.showAnyway') }}</UButton>
          </div>
        </div>
      </div>

      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 flex items-center justify-center bg-white dark:bg-gray-900">
        <div class="flex flex-col items-center gap-3">
          <Spinner class="w-6 h-6 text-ink-muted" />
          <span class="text-sm text-ink-muted">{{ t('artifactFrame.loading') }}</span>
        </div>
      </div>

      <!-- Empty State: Has visualizations but no artifact - show Generate Dashboard button -->
      <!-- No dashboard here yet: make one (from this conversation's charts,
           or by asking), or open one you may edit. -->
      <div v-else-if="!hasArtifact" class="absolute inset-0 flex items-center justify-center overflow-y-auto py-8 bg-white dark:bg-gray-900">
        <DashboardStart :chart-count="hasSuccessfulVisualizations ? visualizationsData.length : 0" :report-id="reportId"
          @generate="generateDashboardPrompt" @brought="onBroughtFromStart" />
      </div>

      <!-- Pending Artifact State (generating) -->
      <div v-else-if="isPendingArtifact" class="absolute inset-0 flex items-center justify-center bg-white dark:bg-gray-900">
        <div class="flex flex-col items-center gap-3">
          <Spinner class="w-6 h-6 text-ink-muted" />
          <span class="text-sm text-ink-muted">
            {{ selectedArtifact?.mode === 'slides' ? 'Generating slides...' : 'Generating dashboard...' }}
          </span>
        </div>
      </div>

      <!-- Snapshot withheld: this dashboard's data is per-user (user-scoped
           sources or RLS), so the shared snapshot is hidden from non-owners
           and steps arrive with empty data. Mirror the /r page: auto-run the
           queries as the viewer (gate shows 'loading'), with state-specific
           fallbacks when a run can't succeed. This must precede every render
           mode — withheld empty data must never reach the artifact code, nor
           surface as a code error with a Fix Error button. The backend never
           withholds from the report owner, so owners never see this. -->
      <div v-else-if="showViewerGate" class="absolute inset-0 flex items-center justify-center bg-white dark:bg-gray-900">
        <ViewerRunGate :state="gateState" :report-id="reportId"
          :is-running="isViewerRunning" :source-errors="dataSourceErrors"
          :error-message="gateErrorMessage" :source-type="gateSourceType" @run="runAsViewer" />
      </div>

      <!-- Slides Mode with Preview Images - Use SlideViewer -->
      <SlideViewer
        v-else-if="hasSlidesWithPreviews && selectedArtifact"
        :artifact-id="selectedArtifact.id"
        class="absolute inset-0"
      />

      <!-- Slides without previews: content.code is python-pptx source, which
           the browser cannot render — never inject it into the iframe. -->
      <div v-else-if="slidesPreviewsMissing" class="absolute inset-0 flex flex-col items-center justify-center bg-white dark:bg-gray-900">
        <Icon name="heroicons:presentation-chart-bar" class="w-8 h-8 text-ink-muted mb-3" />
        <template v-if="selectedArtifact?.status === 'failed'">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Slides generation failed</h3>
          <p class="text-xs text-ink-muted mb-4 max-w-sm text-center">
            The presentation could not be built. Ask the agent to regenerate the slides.
          </p>
        </template>
        <template v-else>
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Slide previews unavailable</h3>
          <p class="text-xs text-ink-muted mb-4 max-w-sm text-center">
            The deck was built, but preview images could not be generated on the server. You can still download the PowerPoint file.
          </p>
          <UButton @click="exportPptx" :disabled="isExporting" size="xs" color="purple" variant="soft">
            <Icon name="heroicons:arrow-down-tray" class="w-4 h-4" />
            Export PPTX
          </UButton>
        </template>
      </div>

      <!-- Doc Mode - owner editing (TipTap) -->
      <DocEditor
        v-else-if="isDocMode && selectedArtifact && isEditingDoc"
        ref="docEditorRef"
        :key="selectedArtifactId"
        :markdown="docMarkdown"
        :visualizations="visualizationsData"
        :title="selectedArtifact?.title"
        class="absolute inset-0"
        @save="saveDocEdit"
        @cancel="isEditingDoc = false"
        @export-pdf="exportDocPdf"
      />

      <!-- Doc Mode - markdown document with live visualizations -->
      <DocViewer
        v-else-if="isDocMode && selectedArtifact"
        :markdown="docMarkdown"
        :visualizations="visualizationsData"
        class="absolute inset-0"
      />

      <!-- Iframe Render Error State -->
      <div v-else-if="iframeError" class="absolute inset-0 flex flex-col items-center justify-center bg-white dark:bg-gray-900">
        <Icon name="heroicons:exclamation-triangle" class="w-8 h-8 text-red-400 mb-3" />
        <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Dashboard failed to render</h3>
        <!-- A viewer missing some charts' data: the likely cause is artifact
             code that assumed those rows exist. They cannot fix it (it is not
             their dashboard), so explain instead of offering Fix Error. -->
        <p v-if="partiallyWithheld" class="text-xs text-gray-500 dark:text-gray-400 max-w-md text-center">
          {{ tm('artifactFrame.partialRenderFailed', selectedArtifact?.mode) }}
        </p>
        <p v-else class="text-xs text-ink-muted mb-3 max-w-md text-center font-mono bg-gray-50 dark:bg-gray-900 rounded p-2 border">
          {{ iframeError.length > 200 ? iframeError.slice(0, 200) + '...' : iframeError }}
        </p>
        <UButton
          v-if="!partiallyWithheld"
          @click="fixRenderError"
          size="xs"
          color="red"
          variant="soft"
        >
          <Icon name="heroicons:wrench-screwdriver" class="w-4 h-4" />
          Fix Error
        </UButton>
      </div>

      <!-- Iframe (shown when artifact exists and data is ready) -->
      <iframe
        v-show="hasArtifact && !isLoading && !isPendingArtifact && !hasSlidesWithPreviews && !isDocMode && !showViewerGate && !iframeError && iframeSrcdoc"
        ref="iframeRef"
        data-artifact-frame
        :srcdoc="iframeSrcdoc"
        sandbox="allow-scripts allow-downloads allow-forms"
        class="absolute inset-0 w-full h-full border-0 bg-white dark:bg-gray-900 z-0"
        @load="onIframeLoad"
      />

      <!-- Polish Mode Button (dashboards only — docs have no JSX to polish, and
           slides render as page images through SlideViewer, so there is no
           iframe for the element picker to talk to) -->
      <div
        v-if="!verificationPreview && hasArtifact && !isLoading && !isPendingArtifact && !snapshotWithheld && !iframeError && !isDocMode && !hasSlidesWithPreviews && !slidesPreviewsMissing"
        class="absolute bottom-4 left-4 z-20"
      >
        <button
          @click="togglePolishMode"
          :class="[
            'flex items-center gap-2 px-3 py-2 rounded-full shadow-lg transition-all',
            isPolishMode
              ? 'bg-indigo-600 text-white hover:bg-indigo-700 ring-2 ring-indigo-300'
              : 'bg-gray-800 text-gray-100 hover:bg-gray-700'
          ]"
        >
          <Icon name="heroicons:paint-brush" class="w-4 h-4" />
          <span class="text-xs font-medium">{{ t('toolbar.polishDashboard') }}</span>
        </button>
      </div>

      <!-- Polish Prompt Box -->
      <div
        v-if="polishPromptVisible"
        ref="polishBoxRef"
        class="absolute z-30 w-80 bg-white dark:bg-gray-900 rounded-lg shadow-xl border border-gray-200 dark:border-gray-700 p-3"
        :style="polishPromptPosition"
      >
        <div class="flex items-center gap-2 mb-2">
          <Icon name="heroicons:paint-brush" class="w-3.5 h-3.5 text-indigo-500" />
          <span class="text-xs font-medium text-gray-700 dark:text-gray-300">Polish this element</span>
          <button @click="cancelPolishPrompt" class="ms-auto text-ink-muted hover:text-gray-600">
            <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
          </button>
        </div>
        <div class="text-2xs text-ink-muted mb-2 font-mono bg-gray-50 dark:bg-gray-900 rounded px-2 py-1 truncate">
          &lt;{{ polishSelectedElement?.tag?.toLowerCase() }}&gt; {{ polishSelectedElement?.text?.slice(0, 60) }}
        </div>
        <!-- Grows with the instruction up to ~6 lines, then scrolls, so a long
             prompt stays readable. Enter applies; Shift+Enter breaks the line. -->
        <form @submit.prevent="submitPolishPrompt" class="flex flex-col gap-2">
          <textarea
            ref="polishInputRef"
            v-model="polishInstruction"
            rows="2"
            dir="auto"
            placeholder="e.g. make this bigger, change colors..."
            class="block w-full resize-none max-h-36 overflow-y-auto text-sm border border-gray-200 dark:border-gray-700 rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-indigo-400 focus:border-indigo-400"
            @keydown.enter.exact="onPolishEnter"
            @keydown.escape="cancelPolishPrompt"
          />
          <button
            type="submit"
            :disabled="!polishInstruction.trim()"
            class="self-end px-3 py-1.5 bg-indigo-600 text-white text-sm rounded-md hover:bg-indigo-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {{ $t('artifactFrame.apply') }}
          </button>
        </form>
      </div>
    </div>

    <!-- Fullscreen Modal -->
    <Teleport to="body">
      <UModal v-model="isFullscreenOpen" :ui="{ width: 'sm:max-w-[98vw]', height: 'h-[98vh]' }">
        <div class="h-full flex flex-col">
          <!-- Modal Header -->
          <div class="p-3 flex justify-between items-center border-b bg-white dark:bg-gray-900">
            <div class="flex items-center gap-3">
              <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ selectedArtifact?.title || reportData?.title || 'Artifact' }}</span>
              <span v-if="selectedArtifact" class="text-xs text-ink-muted">v{{ selectedArtifact.version }}</span>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="closeFullscreen" />
          </div>

          <!-- Modal Content - Full artifact iframe or SlideViewer -->
          <div class="flex-1 min-h-0 relative bg-white dark:bg-gray-900">
            <!-- Slides with previews use SlideViewer -->
            <SlideViewer
              v-if="isFullscreenOpen && hasSlidesWithPreviews && selectedArtifact"
              :artifact-id="selectedArtifact.id"
              class="absolute inset-0"
            />
            <!-- Docs render the DocViewer -->
            <DocViewer
              v-else-if="isFullscreenOpen && isDocMode && selectedArtifact"
              :markdown="docMarkdown"
              :visualizations="visualizationsData"
              class="absolute inset-0"
            />
            <!-- Slides without previews have no renderable content -->
            <div v-else-if="isFullscreenOpen && slidesPreviewsMissing" class="absolute inset-0 flex flex-col items-center justify-center text-ink-muted">
              <Icon name="heroicons:presentation-chart-bar" class="w-8 h-8 mb-2" />
              <span class="text-sm">Slide previews unavailable</span>
            </div>
            <!-- Other artifacts use iframe -->
            <iframe
              ref="fullscreenRuntimeFrame"
              v-else-if="isFullscreenOpen && iframeSrcdoc"
              :srcdoc="iframeSrcdoc"
              sandbox="allow-scripts allow-downloads allow-forms"
              class="absolute inset-0 w-full h-full border-0"
            />
          </div>
        </div>
      </UModal>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import type { ExportFormat } from '~/composables/useArtifactExports'
import { ref, computed, onMounted, onUnmounted, onBeforeUnmount, watch, toRaw, nextTick } from 'vue';
import { useMyFetch as useApplicationFetch } from '~/composables/useMyFetch';
import DashboardSettingsModal from './DashboardSettingsModal.vue';
import DashboardStart from './DashboardStart.vue';
import ShareModal from '../ShareModal.vue';
import PublishButton from './PublishButton.vue';
import PresenceAvatars from './collab/PresenceAvatars.vue';
import Spinner from '../Spinner.vue';
import SlideViewer from './SlideViewer.vue';
import DocViewer from './DocViewer.vue';
import DocEditor from './DocEditor.vue';
import ViewerRunGate from './ViewerRunGate.vue';
import { buildArtifactIframeHtml, isHtmlSlidesCode, inlineableArtifactLibs, artifactPageLibs } from '~/utils/artifactIframe';
import { artifactLibSources, ensureArtifactLibs, whenArtifactLibsSettled } from '~/utils/artifactLibs';

import { polishPromptPosition as computePolishPromptPosition, buildPolishPrompt } from '~/utils/polishPrompt';
import { newestVersionOf } from '~/utils/artifactVersionLatest';
import { viewingPath } from '~/utils/dashboardViewing';

const { t } = useI18n();
const { tm } = useArtifactWording();
const toast = useToast();
const config = useRuntimeConfig();
const { token } = useAuth();
const { organization } = useOrganization();

const { relativeTime: formatRelativeTime } = useRelativeTime()

// Copy artifact ID to clipboard
async function copyArtifactId(id: string) {
  try {
    await navigator.clipboard.writeText(id);
    toast.add({ title: t('artifactFrame.copied'), description: t('artifactFrame.artifactIdCopied'), color: 'green' });
  } catch {
    toast.add({ title: t('artifactFrame.copyFailed'), color: 'red' });
  }
}

interface ArtifactItem {
  id: string;                 // version id
  artifact_id?: string;       // parent identity — shared by all versions of one artifact
  title: string;
  version: number;
  created_at: string;
  mode: string;
  status?: string;
  name?: string | null;       // a name someone gave the version (collab01)
}

// Download the iframe libraries now, in parallel with the artifact/data
// requests (utils/artifactLibs.ts).
ensureArtifactLibs(artifactPageLibs({ reactBuild: 'development', babel: false, pdf: false }));

interface ArtifactFramePrefetch {
  artifactId: string;
  queries?: Promise<any> | null;
  viewerContext?: Promise<any> | null;
  queriesUsed?: boolean;
  viewerContextUsed?: boolean;
}

const props = defineProps<{
  reportId: string;
  report?: any;
  artifacts?: ArtifactItem[];
  /** Full latest-artifact object already fetched by the report page. Used
   *  only as a mount-time seed when it matches the initial selection —
   *  version switches and refreshes always refetch. */
  latestArtifact?: any;
  artifactCode?: string;
  /** Artifact the opener wants shown. Read as a mount-time seed, because the
   *  `artifact:select` window event cannot reach a frame that has not mounted
   *  yet — and this frame shares a v-if chain with ChatSummary, so opening the
   *  summary unmounts it and every click from there arrives too early. A prop
   *  is already reactive state on the page, so it survives the remount. */
  requestedArtifactId?: string | null;
  /** A dashboard to open on its newest version (Edit from its draft page,
   *  `?dashboard=`), instead of the conversation's newest version. */
  requestedDashboardId?: string | null;
  /** Server-brokered preview: no user credential/session enters this page. */
  verificationPreview?: boolean;
  /** This conversation's own agent is running (collab01): outline the
   *  dashboard when others edit it too. */
  agentRunning?: boolean;
  /** First-load requests the report page started before this frame mounted
   *  (same URLs this frame would request). Each is used at most once, only
   *  for the artifact it was started for; anything else fetches normally. */
  prefetch?: ArtifactFramePrefetch | null;
}>();

// The preview's server-side request broker supplies normal authorized API
// responses. Rendering and parameter execution stay on this shared host.
const useMyFetch: typeof useApplicationFetch = (async (request: any, options: any = {}) => {
  if (!props.verificationPreview) return useApplicationFetch(request, options);
  const path = String(request).startsWith('/api/') ? String(request) : `/api${request}`;
  try {
    const value = await $fetch(path, options);
    return { data: ref(value), error: ref(null), pending: ref(false), status: ref('success'), refresh: () => {} };
  } catch (error) {
    return { data: ref(null), error: ref(error), pending: ref(false), status: ref('error'), refresh: () => {} };
  }
}) as typeof useApplicationFetch;

let verificationRevision = 0;
// A full payload carries only the latest accepted result for each query.
const verificationRequests = new Map<string, string>();
function verificationEvent(kind: string, fields: Record<string, any> = {}) {
  if (!props.verificationPreview) return;
  window.dispatchEvent(new CustomEvent('bow:artifact-evidence', {
    detail: { kind, artifact_id: props.requestedArtifactId, ...fields },
  }));
}

defineEmits<{
  (e: 'close'): void;
}>();

// Fullscreen modal state
const isFullscreenOpen = ref(false);

// Export state
const isExporting = ref(false);

// Refresh state
const isRefreshing = ref(false);

// Iframe render error state
const iframeError = ref<string | null>(null);

// Polish mode state
const isPolishMode = ref(false);
const polishPromptVisible = ref(false);
const polishInstruction = ref('');
const polishInputRef = ref<HTMLTextAreaElement | null>(null);
const polishContainerRef = ref<HTMLElement | null>(null);
const polishBoxRef = ref<HTMLElement | null>(null);
const polishBoxHeight = ref(0);
const polishSelectedElement = ref<{ tag: string; classes: string; text: string; htmlSnippet: string; rect: { top: number; left: number; width: number; height: number } } | null>(null);

const polishPromptPosition = computed(() => {
  if (!polishSelectedElement.value?.rect) return { top: '50%', left: '50%' };
  // polishContainerRef is the inline pane the iframe fills, so the rect the
  // iframe reports maps 1:1 onto it.
  const { top, left } = computePolishPromptPosition(
    polishSelectedElement.value.rect,
    { width: polishBoxRef.value?.offsetWidth ?? 320, height: polishBoxHeight.value },
    { width: polishContainerRef.value?.clientWidth ?? Infinity, height: polishContainerRef.value?.clientHeight ?? Infinity },
  );
  return { top: top + 'px', left: left + 'px' };
});

watch(polishBoxRef, (el, _prev, onCleanup) => {
  if (!el) return;
  polishBoxHeight.value = el.offsetHeight;
  const observer = new ResizeObserver(() => { polishBoxHeight.value = el.offsetHeight; });
  observer.observe(el);
  onCleanup(() => observer.disconnect());
});

// Fit the instruction box to its text; max-h caps it and scrolling takes over.
watch(polishInstruction, async () => {
  await nextTick();
  const el = polishInputRef.value;
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
});

function togglePolishMode() {
  if (isPolishMode.value) {
    exitPolishMode();
  } else {
    enterPolishMode();
  }
}

function enterPolishMode() {
  isPolishMode.value = true;
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  // Tell iframe to enable pick mode (srcdoc iframe inherits parent origin)
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_ENTER' }, '*');
}

function exitPolishMode() {
  isPolishMode.value = false;
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_EXIT' }, '*');
}

function cancelPolishPrompt() {
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_ENTER' }, '*');
}

// Enter applies — except the Enter that commits an IME candidate (CJK input),
// which would submit a half-written instruction. Chrome/Firefox flag it with
// isComposing; Safari fires it after compositionend, where only keyCode 229
// gives it away.
function onPolishEnter(e: KeyboardEvent) {
  if (e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  submitPolishPrompt();
}

function submitPolishPrompt() {
  if (!polishInstruction.value.trim() || !polishSelectedElement.value) return;

  const artifactTitle = selectedArtifact.value?.title || 'the dashboard';
  const artifactId = selectedArtifact.value?.id || selectedArtifactId.value || '';
  const el = polishSelectedElement.value;
  const prompt = buildPolishPrompt({
    title: artifactTitle,
    artifactId: String(artifactId),
    htmlSnippet: el.htmlSnippet,
    instruction: polishInstruction.value.trim(),
  });

  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: true }
  }));

  exitPolishMode();
}

// Refresh Dashboard - reruns report queries and refreshes data
async function refreshDashboard() {
  if (isRefreshing.value) return;
  // Belt and braces with the button's :disabled — refreshing under view-as
  // would resolve identity params as the caller, not the previewed member.
  if (isViewAsActive.value) return;

  isRefreshing.value = true;
  isLoading.value = true;

  try {
    // Rerun the queries behind the dashboard being viewed. The owner's
    // refresh updates the shared snapshot (owner-only endpoint); a non-owner
    // refreshes into their own per-viewer results instead.
    const artifactParam = selectedArtifactId.value ? `?artifact_id=${selectedArtifactId.value}` : '';
    const endpoint = isReportOwner.value
      ? `/api/reports/${props.reportId}/rerun${artifactParam}`
      : `/api/r/${props.reportId}/run${artifactParam}`;
    const { data, error } = await useMyFetch(endpoint, { method: 'POST' });
    if (error.value) throw error.value;
    if (!isReportOwner.value) {
      dataSourceErrors.value = ((data.value as any)?.data_source_errors) || [];
    }

    // Refresh artifact data
    await refreshAll();

    // Be honest about what actually ran — a rerun where every step failed
    // (or nothing ran) must not read as a successful refresh.
    const run: any = data.value || {};
    const summary = t('artifactFrame.refreshSummary', { succeeded: run.steps_succeeded ?? 0, total: run.steps_total ?? 0 });
    if (!run.steps_total) {
      toast.add({ title: t('artifactFrame.refreshNothing'), color: 'orange' });
    } else if (run.steps_failed > 0) {
      toast.add({
        title: run.steps_succeeded > 0 ? tm('artifactFrame.refreshPartial', selectedArtifact.value?.mode) : tm('artifactFrame.refreshFailed', selectedArtifact.value?.mode),
        description: summary,
        color: run.steps_succeeded > 0 ? 'orange' : 'red',
      });
    } else {
      toast.add({ title: tm('artifactFrame.refreshed', selectedArtifact.value?.mode), description: summary, color: 'green' });
    }
  } catch (error: any) {
    console.error('Failed to refresh dashboard:', error);
    toast.add({ title: t('artifactFrame.error'), description: tm('artifactFrame.refreshDashboardFailed', selectedArtifact.value?.mode, { error: error.message || '' }), color: 'red' });
    // fetchData never ran, so clear the loading overlay it would have reset —
    // otherwise the dashboard stays hidden behind the spinner forever.
    isLoading.value = false;
  } finally {
    isRefreshing.value = false;
  }
}

// Open fullscreen modal
function openFullscreen() {
  isFullscreenOpen.value = true;
}

// Close fullscreen modal
function closeFullscreen() {
  isFullscreenOpen.value = false;
}

// Export artifact as PPTX
function handleExport(format: ExportFormat) {
  if (format === 'pdf') return exportPdf();
  if (format === 'pptx') return exportPptx();
  return exportHtml();
}

// Download the dashboard or deck as a PDF. Same headless-Chromium render the
// emailed share already uses; the request can run for minutes, so this uses
// native fetch (no ofetch retry to silently re-run the render) exactly like
// the PPTX and HTML paths below.
async function exportPdf() {
  if (!selectedArtifactId.value || isExporting.value) return;

  isExporting.value = true;
  try {
    const headers: Record<string, string> = {
      Authorization: `${token.value}`,
    };
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id;
    }

    const response = await fetch(
      `${config.public.baseURL}/artifacts/${selectedArtifactId.value}/export/pdf`,
      { method: 'GET', headers },
    );

    if (!response.ok) {
      // The route reports actionable reasons (timed out, snapshot withheld,
      // artifact never rendered) — surface them, not a bare status code.
      let detail = `HTTP error! status: ${response.status}`;
      try {
        const body = await response.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* non-JSON error body */
      }
      throw new Error(detail);
    }

    // Same detachment as the other two paths: buffer the bytes and wrap them
    // in a fresh local Blob so the download URL is never the remote response.
    const arrayBuffer = await response.arrayBuffer();
    const localBlob = new Blob([arrayBuffer], { type: 'application/pdf' });

    const rawTitle = selectedArtifact.value?.title || 'artifact';
    const fallbackName =
      `${String(rawTitle).replace(/[^\w\s.-]/g, '').trim().slice(0, 120) || 'artifact'}.pdf`;
    const filename =
      filenameFromDisposition(response.headers.get('Content-Disposition')) || fallbackName;

    const url = window.URL.createObjectURL(localBlob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', filename);
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    toast.add({ title: t('exports.complete'), description: t('exports.pdfDone') });
  } catch (error: any) {
    console.error('Failed to export PDF:', error);
    toast.add({
      title: t('exports.failed'),
      description: error.message || t('exports.failed'),
      color: 'red',
    });
  } finally {
    isExporting.value = false;
  }
}

async function exportPptx() {
  if (!selectedArtifactId.value || isExporting.value) return;

  isExporting.value = true;
  try {
    // Use native fetch for blob download with same auth pattern as useMyFetch
    const headers: Record<string, string> = {
      Authorization: `${token.value}`,
    };
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id;
    }

    const response = await fetch(`${config.public.baseURL}/artifacts/${selectedArtifactId.value}/export/pptx`, {
      method: 'GET',
      headers
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    // Materialise the response body as bytes, then wrap in a fresh local
    // Blob with an explicit MIME type. Going through ArrayBuffer + new Blob
    // detaches the download URL from the remote response (binary content
    // never reaches the DOM as HTML).
    const arrayBuffer = await response.arrayBuffer();
    const localBlob = new Blob([arrayBuffer], {
      type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    });
    const rawTitle = selectedArtifact.value?.title || 'presentation';
    const safeName = String(rawTitle).replace(/[^\w\s.-]/g, '').slice(0, 120) || 'presentation';
    const url = window.URL.createObjectURL(localBlob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', `${safeName}.pptx`);
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    toast.add({ title: t('artifactFrame.exportComplete'), description: t('artifactFrame.exportPptxDone') });
  } catch (error: any) {
    console.error('Failed to export PPTX:', error);
    toast.add({ title: t('artifactFrame.exportFailed'), description: error.message || t('artifactFrame.exportPptxFailed'), color: 'red' });
  } finally {
    isExporting.value = false;
  }
}

/**
 * Filename the server chose, honouring the RFC 5987 form first — dashboard
 * titles are routinely non-Latin and only that form carries them intact.
 */
function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim());
    } catch {
      /* fall through to the ASCII form */
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header);
  return plain ? plain[1] : null;
}

// Export the dashboard as one standalone HTML file that works fully offline.
async function exportHtml() {
  if (!selectedArtifactId.value || isExporting.value) return;

  isExporting.value = true;
  try {
    const headers: Record<string, string> = {
      Authorization: `${token.value}`,
    };
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id;
    }

    const response = await fetch(
      `${config.public.baseURL}/artifacts/${selectedArtifactId.value}/export/html`,
      { method: 'GET', headers },
    );

    if (!response.ok) {
      // The route reports real, actionable reasons (missing vendored libs, a
      // withheld snapshot) — surface them instead of a bare status code.
      let detail = `HTTP error! status: ${response.status}`;
      try {
        const body = await response.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* non-JSON error body */
      }
      throw new Error(detail);
    }

    // Same detachment as the PPTX path: buffer the bytes and wrap them in a
    // fresh local Blob so the download URL is never the remote response. That
    // matters more here — the payload IS html, and handing the browser a
    // remote text/html URL to open would run it in our origin.
    const arrayBuffer = await response.arrayBuffer();
    const localBlob = new Blob([arrayBuffer], { type: 'text/html' });

    const rawTitle = selectedArtifact.value?.title || 'dashboard';
    const fallbackName =
      `${String(rawTitle).replace(/[^\w\s.-]/g, '').trim().slice(0, 120) || 'dashboard'}.html`;
    const filename =
      filenameFromDisposition(response.headers.get('Content-Disposition')) || fallbackName;

    const url = window.URL.createObjectURL(localBlob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', filename);
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    toast.add({
      title: t('artifactFrame.exportComplete'),
      description: t('artifactFrame.exportHtmlDone'),
    });
  } catch (error: any) {
    console.error('Failed to export HTML:', error);
    toast.add({
      title: t('artifactFrame.exportFailed'),
      description: error.message || tm('artifactFrame.exportHtmlFailed', selectedArtifact.value?.mode),
      color: 'red',
    });
  } finally {
    isExporting.value = false;
  }
}

// Viewer identity for ARTIFACT_DATA.current_user (per-render, never stored on
// the artifact). null until fetched — and stays null on any failure so the
// dashboard renders anonymously instead of blocking.
const viewerContext = ref<any>(null);
async function fetchViewerContext() {
  try {
    const pf = props.prefetch;
    if (pf?.viewerContext && !pf.viewerContextUsed && !props.verificationPreview) {
      pf.viewerContextUsed = true;
      const res: any = await pf.viewerContext;
      if (!res?.error?.value) {
        viewerContext.value = res?.data?.value || null;
        return;
      }
    }
    const { data } = await useMyFetch('/api/users/me/viewer_context');
    viewerContext.value = data.value || null;
  } catch {
    viewerContext.value = null;
  }
}

const iframeRef = ref<HTMLIFrameElement | null>(null);
const fullscreenRuntimeFrame = ref<HTMLIFrameElement | null>(null);
// Fullscreen runs a second copy of the artifact: it seeds from the same
// srcdoc, then needs every live update (data, theme, param status) too.
const fullscreenReady = ref(false);
watch(isFullscreenOpen, (open) => { if (!open) fullscreenReady.value = false; });
function postToRuntimeFrames(message: any) {
  for (const frame of [iframeRef.value, fullscreenRuntimeFrame.value]) {
    try { frame?.contentWindow?.postMessage(message, '*'); } catch { /* frame not ready */ }
  }
}
const resourcesAvailable = ref(false);


useArtifactRuntime(() => ({ frames: [iframeRef.value, fullscreenRuntimeFrame.value], artifactId: selectedArtifact.value?.artifact_id, readOnly: !!props.verificationPreview || viewAsMode.value !== "you" }));
const isLoading = ref(true);

// App color mode, forwarded into the artifact iframe (initial srcdoc + live
// toggle via postMessage so switching themes doesn't reload the artifact).
const colorMode = useColorMode();
// Plain (non-reactive) snapshot read by the srcdoc computed, so a mode toggle
// updates the live iframe via postMessage instead of recomputing srcdoc and
// reloading the artifact.
let artifactColorMode: 'light' | 'dark' = colorMode.value === 'dark' ? 'dark' : 'light';
watch(() => colorMode.value, (v) => {
  artifactColorMode = v === 'dark' ? 'dark' : 'light';
  postToRuntimeFrames({ type: 'ARTIFACT_SET_COLOR_MODE', mode: artifactColorMode });
});
const dataReady = ref(false);  // Guards iframeSrcdoc to prevent rendering before data loads

// ── Query parameters ─────────────────────────────────────────────────────────
// Declarations per query (qid → ParamSpec[]) and the current applied values.
// Identity-source params never carry a client value — the server binds them
// per viewer; the artifact only shows "scoped to you".
const queryParamSpecs = ref<Record<string, any[]>>({});
const paramValues = ref<Record<string, any>>({});
// Host-resolved stable choices per param name: static declared options and
// options-source query rows (the filter-space pattern — e.g. a 'Genres' query
// feeding the genre control of 'Albums by Genre'). Controls read these via
// useParamOptions(), never the filtered data itself.
const paramOptions = ref<Record<string, Array<{ value: any; label: string }>>>({});
const paramRunLoading = ref(false);
// Highest ARTIFACT_SET_PARAMS seq received from the iframe; echoed back in
// paramsPayload().ack so the runtime store can tell which of its in-flight
// commits this data push reflects (older pushes must not clobber newer typing).
let paramAckSeq = 0;
// Latest run id per query — a slower, superseded run must not overwrite the
// rows a newer run already delivered (the 'Si' echo landing after 'Sir').
let paramRunCounter = 0;
const latestParamRunForQid: Record<string, number> = {};
// Snapshot embedded in the iframe's srcdoc; frozen per data load so live
// param/view-as updates (postMessage) never trigger an iframe reload.
const srcdocSeed = ref<any>(null);

// Which sandbox runtime generation the selected artifact was authored for.
// Read from content.runtime_version so a legacy row keeps its legacy look.
function artifactRuntime() {
  const v = Number((selectedArtifact.value as any)?.content?.runtime_version || 0);
  return { version: Number.isFinite(v) ? v : 0 };
}

function paramsPayload() {
  // Aggregate declarations by name: same-named params across queries render
  // as ONE control that drives all of them.
  const byName: Record<string, any> = {};
  for (const [qid, specs] of Object.entries(queryParamSpecs.value)) {
    for (const spec of specs || []) {
      if (!byName[spec.name]) byName[spec.name] = { ...spec, query_ids: [] };
      byName[spec.name].query_ids.push(qid);
    }
  }
  return {
    declarations: Object.values(byName),
    values: { ...paramValues.value },
    options: { ...paramOptions.value },
    ack: paramAckSeq,
  };
}

function normalizeStaticOptions(opts: any[]): Array<{ value: any; label: string }> {
  return (opts || []).map((v: any) =>
    (v && typeof v === 'object' && 'value' in v) ? v : { value: v, label: String(v) });
}

// Resolve each param's stable choice list. Options-source rows win over a
// static list; a source query outside the artifact is fetched from the
// report-level list once. Dedupe by value; label falls back to the value.
async function resolveParamOptions(loadedQueries: any[], fetchAll: () => Promise<any[]>) {
  const byId = new Map(loadedQueries.map((q: any) => [String(q.id), q]));
  const out: Record<string, Array<{ value: any; label: string }>> = {};
  let fetchedAll = false;
  for (const specs of Object.values(queryParamSpecs.value)) {
    for (const spec of (specs as any[]) || []) {
      if (!spec?.name) continue;
      if (!out[spec.name] && Array.isArray(spec.options) && spec.options.length) {
        out[spec.name] = normalizeStaticOptions(spec.options);
      }
      const src = spec.options_source;
      if (!src?.query_id) continue;
      // Match by id, falling back to title — refs persisted from agent runs
      // before server-side canonicalization name the query loosely.
      const findSrc = () => {
        const ref = String(src.query_id);
        if (byId.has(ref)) return byId.get(ref);
        const lowered = ref.toLowerCase();
        for (const q of byId.values()) {
          if (String(q?.title || '').toLowerCase() === lowered) return q;
        }
        return null;
      };
      let srcQ: any = findSrc();
      if (!srcQ && !fetchedAll) {
        fetchedAll = true;
        try {
          for (const q of await fetchAll()) byId.set(String(q.id), q);
        } catch { /* options degrade to static/none */ }
        srcQ = findSrc();
      }
      const rows = srcQ?.default_step?.data?.rows || srcQ?.__step_rows || [];
      const seen = new Set<string>();
      const opts: Array<{ value: any; label: string }> = [];
      for (const r of rows) {
        const value = r?.[src.value_column];
        if (value === undefined || value === null) continue;
        const key = String(value);
        if (seen.has(key)) continue;
        seen.add(key);
        const label = src.label_column ? (r?.[src.label_column] ?? value) : value;
        opts.push({ value, label: String(label) });
      }
      if (opts.length) out[spec.name] = opts;
    }
  }
  derivedFallbackOptions(out, (qid) => {
    const step = byId.get(String(qid))?.default_step;
    return step ? { data: step.data, applied_params: step.applied_params } : null;
  });
  paramOptions.value = out;
}

// Last-resort options tier: an input param with neither static options nor an
// options-source query still deserves a populated control. Derive stable
// choices ONCE per load from the initially loaded rows of its declaring
// queries, matching a column by (singularized) param name — e.g. genre_id →
// GenreId, genres → GenreName. Computed only here, never on param-driven data
// pushes, so the list cannot narrow itself to the current selection.
function derivedFallbackOptions(
  out: Record<string, Array<{ value: any; label: string }>>,
  dataOfQuery: (qid: string) => any,
) {
  const norm = (s: any) => String(s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  for (const [qid, specs] of Object.entries(queryParamSpecs.value)) {
    const entry = dataOfQuery(qid) || {};
    const data = entry.data || entry;
    const applied = entry.applied_params || {};
    const rows = data.rows || [];
    const cols: string[] = (data.columns || []).map((c: any) => c.field || c.headerName).filter(Boolean);
    if (!rows.length || !cols.length) continue;
    for (const spec of (specs as any[]) || []) {
      if (!spec?.name || out[spec.name] || spec.source === 'identity') continue;
      // A snapshot the param already filtered yields a degenerate one-value
      // list — only derive when the snapshot ran with this param = All.
      if (applied[spec.name] !== null && applied[spec.name] !== undefined) continue;
      let n = norm(spec.name);
      if (n.endsWith('s')) n = n.slice(0, -1);
      const valueCol = cols.find(c => norm(c) === norm(spec.name))
        || cols.find(c => norm(c) === n)
        || cols.find(c => norm(c).startsWith(n))
        || cols.find(c => norm(c).includes(n));
      if (!valueCol) continue;
      // Friendlier labels: for an id-ish column, use a name/title sibling
      // sharing the stem (genre_id → genre_name) when one exists.
      const stem = norm(valueCol).replace(/id$/, '');
      const labelCol = stem
        ? cols.find(c => [stem + 'name', stem + 'title', stem].includes(norm(c)) && c !== valueCol)
        : null;
      const seen = new Set<string>();
      const opts: Array<{ value: any; label: string }> = [];
      for (const r of rows) {
        const value = r?.[valueCol];
        if (value === undefined || value === null) continue;
        const key = String(value);
        if (seen.has(key)) continue;
        seen.add(key);
        opts.push({ value, label: String(labelCol ? (r?.[labelCol] ?? value) : value) });
        if (opts.length >= 200) break;
      }
      if (opts.length) out[spec.name] = opts;
    }
  }
}

function paramSubsetForQuery(qid: string) {
  const out: Record<string, any> = {};
  for (const spec of queryParamSpecs.value[qid] || []) {
    if (spec.source === 'identity') continue; // server-resolved, client values rejected
    if (Object.prototype.hasOwnProperty.call(paramValues.value, spec.name)) {
      out[spec.name] = paramValues.value[spec.name];
    }
  }
  return out;
}

function queriesWithIdentityParams(): string[] {
  return Object.entries(queryParamSpecs.value)
    .filter(([, specs]) => (specs || []).some((s: any) =>
      s.source === 'identity' || s.source === 'input_identity_default'))
    .map(([qid]) => qid);
}

function postParamsStatus(loading: boolean, error: string | null = null) {
  paramRunLoading.value = loading;
  verificationEvent(error ? 'error' : 'params_status', { loading, message: error });
  postToRuntimeFrames({ type: 'ARTIFACT_PARAMS_STATUS', payload: { loading, error } });
}

// Execute the parameterized queries server-side (viewer mode: per-viewer
// cached results, no new Step) and swap the fresh rows into the payload.
async function runParamQueries(
  changes: Record<string, any> | null,
  targets: string[] | null = null,
  opts: { force?: boolean; identityOnly?: boolean } = {},
) {
  const changedNames = Object.keys(changes || {});
  verificationEvent('params_commit', { changes, targets, commit_seq: paramAckSeq });

  // A name no query declares would silently no-op (nothing to run) — the
  // classic generated-code bug of setParam('genre') vs a declared genre_id.
  // Fail loudly into the params status channel instead.
  const declaredNames = new Set(
    Object.values(queryParamSpecs.value).flatMap((specs: any) =>
      (specs || []).map((s: any) => s.name)));
  const unknown = changedNames.filter(n => !declaredNames.has(n));
  if (unknown.length) {
    postParamsStatus(false,
      `Unknown parameter(s): ${unknown.join(', ')} — declared: ${[...declaredNames].join(', ') || 'none'}. ` +
      'Controls must call setParam with a declared param name.');
    return;
  }
  for (const name of changedNames) paramValues.value[name] = (changes as any)[name];

  let affected = Object.entries(queryParamSpecs.value)
    .filter(([qid, specs]) => {
      if (targets && !targets.includes(qid)) return false;
      if (opts.identityOnly) {
        return (specs || []).some((s: any) =>
          s.source === 'identity' || s.source === 'input_identity_default');
      }
      if (!changedNames.length) return (specs || []).length > 0;
      return (specs || []).some((s: any) => changedNames.includes(s.name));
    })
    .map(([qid]) => qid);
  affected = [...new Set(affected)];
  if (!affected.length) return;

  const myRun = ++paramRunCounter;
  for (const qid of affected) latestParamRunForQid[qid] = myRun;

  postParamsStatus(true);
  let firstError: string | null = null;
  const runAs = (viewAsMode.value !== 'you' && viewAsMode.value !== 'anonymous')
    ? viewAsMode.value : undefined;
  await Promise.all(affected.map(async (qid) => {
    try {
      const { data: runRes, error } = await useMyFetch(`/api/queries/${qid}/run`, {
        method: 'POST',
        body: {
          mode: 'viewer',
          params: paramSubsetForQuery(qid),
          force_refresh: !!opts.force,
          ...(runAs ? { run_as_user_id: runAs } : {}),
        },
      });
      const res: any = runRes.value;
      if (error.value || !res || res.status !== 'success') {
        const msg = (error.value as any)?.data?.detail || res?.error || 'Query run failed';
        if (!firstError) firstError = String(msg);
        return;
      }
      if (latestParamRunForQid[qid] !== myRun) return; // superseded mid-flight
      if (props.verificationPreview && res.verification_request_id) verificationRequests.set(qid, res.verification_request_id);
      for (const viz of visualizationsData.value) {
        if (viz.queryId === qid) {
          viz.rows = res.data?.rows || [];
          viz.columns = res.data?.columns || [];
          viz.stepStatus = 'success';
        }
      }
    } catch (e: any) {
      if (!firstError) firstError = e?.message || 'Query run failed';
    }
  }));
  // New array identity so watchers fire and the fresh rows reach the iframe.
  visualizationsData.value = [...visualizationsData.value];
  // A fully superseded run stays quiet: the newer run owns the status line
  // and will push its own (fresher) data.
  if (myRun === paramRunCounter) {
    postParamsStatus(false, firstError);
    syncParamsToUrl();
  }
}

// Shareable state: param values mirror into the URL (?qp_<name>=...) and are
// restored on load.
const paramRoute = useRoute();
const paramRouter = useRouter();
function syncParamsToUrl() {
  try {
    const q: Record<string, any> = { ...paramRoute.query };
    for (const k of Object.keys(q)) if (k.startsWith('qp_')) delete q[k];
    for (const [name, value] of Object.entries(paramValues.value)) {
      if (value === null || value === undefined || value === '') continue;
      q[`qp_${name}`] = typeof value === 'object' ? JSON.stringify(value) : String(value);
    }
    paramRouter.replace({ query: q });
  } catch { /* non-fatal */ }
}
function readParamsFromUrl(): Record<string, any> {
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(paramRoute.query || {})) {
    if (!k.startsWith('qp_') || v == null) continue;
    const name = k.slice(3);
    const raw = Array.isArray(v) ? v[0] : v;
    try { out[name] = JSON.parse(String(raw)); }
    catch { out[name] = String(raw); }
  }
  return out;
}
const iframeReady = ref(false);
const visualizationsData = ref<any[]>([]);
const filesData = ref<any[]>([]);
const reportData = ref<any>(null);

// Resolve embedded files (generated images / uploaded images/PDFs) for the
// artifact to short-lived, file-scoped token URLs the sandbox <BowFile> can load
// without a session (the iframe can't send an auth header). The durable
// reference stays the file id; the token is minted fresh here per render.
async function fetchArtifactFiles(): Promise<any[]> {
  const files = (selectedArtifact.value as any)?.content?.files;
  if (!Array.isArray(files) || files.length === 0) return [];

  const resolved = await Promise.all(files.map(async (f: any) => {
    try {
      const { data } = await useMyFetch(`/api/files/${f.id}/embed_token`);
      const url = (data.value as any)?.url || '';
      return { id: f.id, content_type: f.content_type, filename: f.filename, url };
    } catch (e) {
      console.error('[ArtifactFrame] Failed to mint file token', f.id, e);
      return { id: f.id, content_type: f.content_type, filename: f.filename, url: '' };
    }
  }));
  return resolved;
}

// Artifact selection state
const artifactsList = ref<ArtifactItem[]>([]);
const selectedArtifactId = ref<string | undefined>(undefined);
const selectedArtifact = ref<any>(null);
watch(() => selectedArtifact.value?.artifact_id, async (id) => {
  resourcesAvailable.value = false;
  if (!id || props.verificationPreview) return;
  try {
    await $fetch(`/api/artifacts/${encodeURIComponent(id)}/runtime/context`, {headers:{Authorization:token.value || ''}});
    if (selectedArtifact.value?.artifact_id === id) resourcesAvailable.value = true;
  } catch { /* Disabled on existing installations until explicitly enabled. */ }
}, {immediate:true});

// Availability of PDF / PPTX / HTML for whatever is on screen; the public
// share page derives its list from the same composable.
const { availableExports } = useArtifactExports(selectedArtifact)

// The pickers: which dashboard (a conversation can hold several), then
// which of its versions. One line of versions per dashboard; Live is the
// one viewers see (from the Publish button's status).
const modeIcon = (mode?: string) =>
  mode === 'doc' ? 'heroicons:document-text' : (mode === 'slides' ? 'heroicons:presentation-chart-bar' : 'heroicons:squares-2x2');
const dashboardKeyOf = (a: ArtifactItem) => a.artifact_id || a.id;
const selectedArtifactItem = computed(() => artifactsList.value.find(a => a.id === selectedArtifactId.value) || null);
// Every dashboard in this conversation, newest first: its versions and when
// it was first made (what tells same-named dashboards apart).
const dashboardGroups = computed(() => {
  const groups = new Map<string, ArtifactItem[]>();
  for (const a of artifactsList.value) {
    const k = dashboardKeyOf(a);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k)!.push(a);
  }
  return [...groups.entries()].map(([key, rows]) => {
    const versions = [...rows].sort((x, y) => y.version - x.version);
    const first = [...rows].sort((x, y) => new Date(x.created_at).getTime() - new Date(y.created_at).getTime())[0];
    return { key, title: versions[0]?.title || 'Untitled', mode: versions[0]?.mode, created_at: first?.created_at, versions };
  }).sort((x, y) => new Date(y.versions[0]?.created_at || 0).getTime() - new Date(x.versions[0]?.created_at || 0).getTime());
});
const selectedDashboardKey = computed(() => (selectedArtifactItem.value ? dashboardKeyOf(selectedArtifactItem.value) : undefined));
const selectedDashboardTitle = computed(() => selectedArtifactItem.value?.title || t('artifactFrame.selectArtifact'));

const publishStatus = ref<any>(null);
const liveVersionId = computed<string | null>(() => publishStatus.value?.published?.source_version_id || null);
const liveNumber = computed<number | null>(() => publishStatus.value?.published?.source_version_number || null);
// Every version of the open dashboard, from any conversation (History):
// ours are picked directly; one made elsewhere offers Preview or Bring here.
const ELSEWHERE = 'elsewhere:';
const ALL_VERSIONS = 'all-versions';
const allVersions = ref<any[]>([]);
async function loadAllVersions() {
  const dashboardId = selectedArtifactItem.value?.artifact_id;
  if (!dashboardId || props.verificationPreview) { allVersions.value = []; return; }
  const { data, error } = await useMyFetch(`/artifacts/${encodeURIComponent(dashboardId)}/history`);
  if (dashboardId !== selectedArtifactItem.value?.artifact_id) return;
  allVersions.value = error.value ? [] : (((data.value as any)?.versions) || []);
}
watch(() => [selectedArtifactItem.value?.artifact_id, artifactsList.value.length, liveVersionId.value], loadAllVersions);
const pickerOptions = computed(() => {
  const several = dashboardGroups.value.length > 1;
  const out: any[] = [];
  for (const g of dashboardGroups.value) {
    if (several) {
      out.push({ value: `heading:${g.key}`, label: g.title, header: true, disabled: true,
                 title: g.title, mode: g.mode, created_at: g.created_at });
    }
    const isSelected = g.key === selectedDashboardKey.value;
    const rows: any[] = g.versions.map(a => ({
      value: a.id, label: `${g.title} v${a.version}`, version: a.version, name: a.name || null,
      created_at: a.created_at, grouped: several, live: isSelected && a.id === liveVersionId.value,
    }));
    if (isSelected) {
      // From other conversations only live and the newest one there; the
      // rest stay in Settings > Versions ("All versions…").
      const here = new Set(g.versions.map(a => a.id));
      const others = allVersions.value.filter(v => !here.has(v.id) && String(v.report_id) !== String(props.reportId));
      const newestOther = others.reduce((m: any, v: any) => (!m || v.number > m.number ? v : m), null);
      for (const v of others) {
        if (!v.live && v !== newestOther && v.id !== viewing.value?.versionId) continue;
        rows.push({ value: `${ELSEWHERE}${v.id}`, label: `${g.title} v${v.number}`, version: v.number, name: v.name || null,
                    created_at: v.created_at, grouped: several, live: !!v.live, elsewhere: true, author: v.author?.name || null,
                    versionId: v.id });
      }
      rows.sort((x, y) => y.version - x.version);
    }
    out.push(...rows);
    if (isSelected && allVersions.value.length && dashboardCaps.value.includes('edit')) {
      out.push({ value: ALL_VERSIONS, label: t('dashboardCollab.versions.allVersions'), allVersions: true });
    }
  }
  return out;
});

// A version made in another conversation is viewed in place, read-only:
// nothing is copied until "Edit here" brings it in (a copy whose charts run
// again as you; the next edit builds on it). Live is the published page, as
// readers see it (viewingPath).
const viewing = ref<{ versionId: string; version: number; author?: string | null; live?: boolean } | null>(null);
const viewingSrc = computed(() => {
  const dashboardId = selectedArtifactItem.value?.artifact_id;
  return viewing.value && dashboardId ? viewingPath(dashboardId, viewing.value) : null;
});
const pickerValue = computed(() => (viewing.value ? `${ELSEWHERE}${viewing.value.versionId}` : selectedArtifactId.value));
function onPick(value: string) {
  if (value === ALL_VERSIONS) { settingsModalRef.value?.open('versions'); return; }
  if (value && String(value).startsWith(ELSEWHERE)) {
    const option = pickerOptions.value.find(o => o.value === value);
    if (option) viewing.value = { versionId: option.versionId, version: option.version, author: option.author, live: option.live };
    return;
  }
  viewing.value = null;
  selectedArtifactId.value = value;
}
// Picking a version of our own, or the agent saving one, ends the viewing.
watch(selectedArtifactId, () => { viewing.value = null; });
const bringing = ref(false);
async function bringHere(versionId: string | null) {
  const dashboardId = selectedArtifactItem.value?.artifact_id;
  if (!dashboardId || bringing.value) return null;
  bringing.value = true;
  try {
    const { data, error } = await useMyFetch(`/artifacts/${encodeURIComponent(dashboardId)}/bring`, {
      method: 'POST', body: { report_id: props.reportId, version_id: versionId },
    });
    if (error.value) throw error.value;
    const res = data.value as any;
    await fetchArtifactsList(true);
    if (res?.version_id && artifactsList.value.some(a => a.id === res.version_id)) {
      await switchToArtifact(res.version_id);
    }
    return res;
  } catch (e: any) {
    toast.add({ title: t('dashboardCollab.versions.bringFailed'), description: e?.data?.detail?.message || undefined, color: 'red' });
    return null;
  } finally {
    bringing.value = false;
  }
}
async function editViewing() {
  const v = viewing.value;
  if (!v) return;
  const res = await bringHere(v.versionId);
  if (res) viewing.value = null;
}
// The chat's note above the prompt offers "Edit here" too.
function onEditHere(e: Event) {
  if ((e as CustomEvent).detail?.report_id === props.reportId) editViewing();
}
onMounted(() => window.addEventListener('artifact:edit-here', onEditHere));
onBeforeUnmount(() => window.removeEventListener('artifact:edit-here', onEditHere));
// "Open v4" (the publish question): ours is selected; one from elsewhere viewed.
async function openVersionById(versionId: string) {
  if (artifactsList.value.some(a => a.id === versionId)) {
    await switchToArtifact(versionId);
    return;
  }
  if (!allVersions.value.some(v => v.id === versionId)) await loadAllVersions();
  const v = allVersions.value.find(x => x.id === versionId);
  if (v) viewing.value = { versionId: v.id, version: v.number, author: v.author?.name || null, live: !!v.live };
}

async function viewAskedVersion(n: number) {
  await loadAllVersions();
  const v = allVersions.value.find(x => x.number === n);
  if (!v || artifactsList.value.some(a => a.id === v.id)) return;
  liveAhead.value = null;
  viewing.value = { versionId: v.id, version: v.number, author: v.author?.name || null, live: !!v.live };
}

// On opening (and when another dashboard is picked): live moved on past this
// conversation's work -> show it, read-only, without copying anything; with
// work of its own here nobody published, ask first instead.
const liveAhead = ref<any | null>(null);
const liveAheadDismissed = ref<Set<string>>(new Set());
async function onBroughtFromStart(versionId: string | null) {
  await fetchArtifactsList(true);
  if (versionId && artifactsList.value.some(a => a.id === versionId)) await switchToArtifact(versionId);
}
async function checkLiveAhead() {
  liveAhead.value = null;
  viewing.value = null;
  const dashboardId = selectedArtifactItem.value?.artifact_id;
  if (!dashboardId || props.verificationPreview) return;
  const { data, error } = await useMyFetch(`/artifacts/${encodeURIComponent(dashboardId)}/open-state`, {
    query: { report_id: props.reportId },
  });
  if (error.value || dashboardId !== selectedArtifactItem.value?.artifact_id) return;
  const st = data.value as any;
  if (viewing.value || !st?.behind_live || !st.live) return;
  if (st.unpublished_here) {
    if (!liveAheadDismissed.value.has(st.live.version_id)) liveAhead.value = st;
    return;
  }
  viewing.value = { versionId: st.live.version_id, version: st.live.version, author: st.live.by?.name || null, live: true };
}
// The version open here is what this conversation's next edit builds on
// (collab01): the prompt sends it, and says so when a newer one exists.
function announceOpenVersion() {
  const item = selectedArtifactItem.value;
  const group = item ? dashboardGroups.value.find(g => g.key === dashboardKeyOf(item)) : null;
  const newest = group?.versions[0] || null;
  window.dispatchEvent(new CustomEvent('artifact:open-version', {
    detail: item && !props.verificationPreview ? {
      report_id: props.reportId, version_id: item.id, version: item.version,
      newest_id: newest?.id || item.id, newest_version: newest?.version || item.version,
      viewing_version: viewing.value?.version ?? null,
    } : { report_id: props.reportId, version_id: null },
  }));
}
watch([selectedArtifactId, () => artifactsList.value.length, viewing], announceOpenVersion);
onBeforeUnmount(() => window.dispatchEvent(new CustomEvent('artifact:open-version', {
  detail: { report_id: props.reportId, version_id: null },
})));
function viewLiveAhead() {
  const st = liveAhead.value;
  if (!st) return;
  viewing.value = { versionId: st.live.version_id, version: st.live.version, author: st.live.by?.name || null, live: true };
  liveAhead.value = null;
}
function keepWorkingHere() {
  if (liveAhead.value?.live?.version_id) liveAheadDismissed.value.add(liveAhead.value.live.version_id);
  liveAhead.value = null;
}
watch(() => selectedArtifactItem.value?.artifact_id, (id, previous) => {
  if (id && id !== previous && !props.verificationPreview) checkLiveAhead();
});

// "Use this version" appears when the selection is not the newest version of
// its dashboard (utils/artifactVersionLatest.ts; old versions with no tie to
// their dashboard keep the rule by mode).

// Check if selected artifact is pending (still generating)
const isPendingArtifact = computed(() => {
  return selectedArtifact.value?.status === 'pending';
});

// Check if any artifacts exist
const hasArtifact = computed(() => {
  return artifactsList.value.length > 0;
});

// Check if visualizations data exists
const hasVisualizations = computed(() => {
  return visualizationsData.value.length > 0;
});

// Check if any visualization has a successful step status
const hasSuccessfulVisualizations = computed(() => {
  return visualizationsData.value.some(viz => viz.stepStatus === 'success');
});

// Check if we have slides mode with preview images (use SlideViewer instead of iframe)
const hasSlidesWithPreviews = computed(() => {
  if (!selectedArtifact.value) return false;
  if (selectedArtifact.value.mode !== 'slides') return false;
  const previewImages = selectedArtifact.value.content?.preview_images;
  return Array.isArray(previewImages) && previewImages.length > 0;
});

// Slides artifact whose previews are missing (preview generation failed, or
// the pptx build itself failed) and whose code is python-pptx source, not
// legacy HTML slides. Such an artifact has nothing browser-renderable — show
// the dedicated fallback instead of the iframe.
const slidesPreviewsMissing = computed(() => {
  if (selectedArtifact.value?.mode !== 'slides') return false;
  if (hasSlidesWithPreviews.value) return false;
  return !isHtmlSlidesCode(selectedArtifact.value?.content?.code || '');
});

// Doc mode: markdown document rendered by DocViewer (no iframe, no JSX)
const isDocMode = computed(() => selectedArtifact.value?.mode === 'doc');
const docMarkdown = computed(() => selectedArtifact.value?.content?.markdown || '');

// Owner-only TipTap editing (the API enforces ownership independently)
const { data: sessionUser } = useAuth();
const isReportOwner = computed(() => {
  const uid = (sessionUser.value as any)?.user?.id || (sessionUser.value as any)?.id;
  const ownerId = props.report?.user?.id || props.report?.user_id;
  return !!uid && !!ownerId && String(uid) === String(ownerId);
});
const isEditingDoc = ref(false);
const docEditorRef = ref<any>(null);

// --- Collaboration (collab01) ------------------------------------------
// What this user may do with the dashboard open here: its draft page's
// capabilities, asked once per dashboard. Empty while unknown, and for
// anyone the dashboard does not let edit -- every collaboration control
// then stays hidden, so a dashboard nobody else edits looks as before.
const dashboardMeta = ref<{ capabilities: string[]; isOwner: boolean } | null>(null);
const dashboardCaps = computed<string[]>(() => dashboardMeta.value?.capabilities || []);
const currentDashboardId = computed<string | null>(() => selectedArtifact.value?.artifact_id || null);
let dashboardMetaFor: string | null = null;
async function loadDashboardMeta(force = false) {
  const id = currentDashboardId.value;
  if (!force && id === dashboardMetaFor) return;
  dashboardMetaFor = id;
  if (!force) dashboardMeta.value = null;
  if (!id || props.verificationPreview) return;
  try {
    const { data, error } = await useMyFetch(`/api/d/${encodeURIComponent(id)}/draft`);
    if (dashboardMetaFor !== id) return; // switched dashboards meanwhile
    const meta: any = error.value ? null : data.value;
    dashboardMeta.value = meta ? { capabilities: meta.capabilities || [], isOwner: !!meta.is_owner } : null;
  } catch {
    if (dashboardMetaFor === id) dashboardMeta.value = null;
  }
}
watch(currentDashboardId, () => { loadDashboardMeta(); }, { immediate: true });

// Presence: beat while an editor has a page or slides dashboard open here,
// with this conversation; get back who else is editing it.
const presenceEnabled = computed(() =>
  !props.verificationPreview && !!currentDashboardId.value
  && ['page', 'slides'].includes(selectedArtifact.value?.mode)
  && dashboardCaps.value.includes('edit'));
const { others: presenceOthers } = useDashboardPresence(
  currentDashboardId, computed(() => props.reportId), presenceEnabled);

// This conversation's agent at work on a dashboard someone else edits too
// (they have it open now, or it is not this user's own dashboard): outline
// it with whose agent it is. A dashboard only its owner edits shows nothing.
const showAgentOutline = computed(() =>
  !!props.agentRunning && presenceEnabled.value
  && (presenceOthers.value.length > 0 || dashboardMeta.value?.isOwner === false));
const agentOwnerFirstName = computed(() => {
  const me = sessionUser.value as any;
  const name = String(props.report?.user?.name || me?.user?.name || me?.name || '');
  return name.split(/[\s@]/)[0] || name;
});

// "Review changes" from the publish warning: the draft page's changes view.
// "Review changes" (someone published since): the version open here against
// what readers see now -- not the newest version, which may be theirs.
function reviewChanges() {
  const id = currentDashboardId.value;
  if (!id) return;
  const n = selectedArtifact.value?.version;
  navigateTo(`/d/${encodeURIComponent(id)}/draft?${n ? `version=${n}&` : ''}changes=1`);
}

// The settings window: its History tabs open from the ⋯ menu too.
const settingsModalRef = ref<any>(null);
// Remounting the Publish button re-reads what readers see now.
const publishKey = ref(0);
// A roll back changes what is live; a transfer changes who may do what.
function onHistoryChanged() {
  publishKey.value += 1;
  loadDashboardMeta(true);
}

// --- Rename the dashboard ------------------------------------------------
// "The dashboard" is two records to the user: the report (its card on
// /dashboards, the sidebar entry, the browser tab) and the selected artifact
// version (the label in the dropdown above, the /r/{id} tab). Renaming one
// without the other looked like the rename silently failed, so both are
// written. Both endpoints are owner-only (update_reports + owner_only), so
// the pencil is hidden for everyone else rather than failing on click.
const isRenaming = ref(false);
const renameDraft = ref('');
const isSavingRename = ref(false);
const renameInputRef = ref<any>(null);

const canRename = computed(() =>
  isReportOwner.value && !!selectedArtifactId.value && !isPendingArtifact.value);

function currentArtifactTitle(): string {
  const item = artifactsList.value.find(a => a.id === selectedArtifactId.value);
  const title = item?.title || selectedArtifact.value?.title || '';
  // The model default is a placeholder, not a name worth editing around.
  return title === 'Untitled Artifact' ? '' : title;
}

async function startRename() {
  if (!canRename.value) return;
  renameDraft.value = currentArtifactTitle();
  isRenaming.value = true;
  await nextTick();
  const el = renameInputRef.value?.$el?.querySelector?.('input') || renameInputRef.value?.input;
  el?.focus?.();
  el?.select?.();
}

function cancelRename() {
  if (isSavingRename.value) return;
  isRenaming.value = false;
  renameDraft.value = '';
}

async function saveRename() {
  const title = renameDraft.value.trim();
  if (!title || isSavingRename.value) return;
  if (title === currentArtifactTitle()) { cancelRename(); return; }
  if (await renameTo(title)) {
    isRenaming.value = false;
    renameDraft.value = '';
  }
}

// The rename itself, for the inline field and the settings' General tab.
async function renameTo(title: string): Promise<boolean> {
  const id = selectedArtifactId.value;
  if (!id || !title || isSavingRename.value) return false;
  isSavingRename.value = true;
  try {
    const { data, error } = await useMyFetch(`/api/artifacts/${id}`, {
      method: 'PATCH',
      body: { title },
    });
    if (error.value) throw error.value;

    const saved = (data.value as any)?.title || title;

    // The report title is what every list renders (see /dashboards). Keep it
    // in step, then tell the page and the sidebar — the same event
    // loadReport() fires when the server generates a title.
    if (props.report?.id) {
      const { error: reportError } = await useMyFetch(`/api/reports/${props.report.id}`, {
        method: 'PUT',
        body: { title: saved },
      });
      if (reportError.value) throw reportError.value;
      window.dispatchEvent(new CustomEvent('report:updated', {
        detail: { id: props.report.id, title: saved },
      }));
    }
    // Patch local state instead of refetching: the list endpoint is the same
    // one the report page seeds us from, and a refetch would race an
    // in-flight generation and could snap the selection.
    artifactsList.value = artifactsList.value.map(a => a.id === id ? { ...a, title: saved } : a);
    if (selectedArtifact.value?.id === id) {
      selectedArtifact.value = { ...selectedArtifact.value, title: saved };
    }
    toast.add({ title: tm('artifactFrame.renamed', selectedArtifact.value?.mode), color: 'green' });
    return true;
  } catch (e: any) {
    console.error('[ArtifactFrame] Failed to rename artifact:', e);
    toast.add({
      title: t('artifactFrame.error'),
      description: e?.data?.detail?.message || e?.data?.detail || tm('artifactFrame.renameFailed', selectedArtifact.value?.mode),
      color: 'red',
    });
    return false;
  } finally {
    isSavingRename.value = false;
  }
}

// Switching versions mid-edit would silently retarget the save.
watch(selectedArtifactId, () => { if (isRenaming.value) cancelRename(); });

// --- Overflow (⋯) menu ---------------------------------------------------
type MenuItem = { label: string; icon: string; click: () => void; disabled?: boolean; testid?: string };

// Groups render with dividers between them (Nuxt UI takes an array of
// arrays). Empty groups are dropped so no divider ever leads nowhere.
const moreMenuItems = computed<MenuItem[][]>(() => {
  const edit: MenuItem[] = [];
  if (canRename.value) {
    edit.push({ label: tm('artifactFrame.rename', selectedArtifact.value?.mode), icon: 'i-heroicons-pencil', click: startRename });
  }

  // The .md source download is doc-only; PDF/PPTX/HTML come from
  // useArtifactExports so this menu and the public share page agree on what
  // a given artifact can produce.
  const exports: MenuItem[] = [];
  if (isDocMode.value && !isEditingDoc.value) {
    exports.push({ label: t('docViewer.exportMarkdown'), icon: 'i-heroicons-arrow-down-tray', click: exportDocMarkdown });
  }
  // Viewing another version read-only: exports and full screen would act on
  // our hidden one, so they go; a new tab opens the version being viewed.
  for (const option of viewing.value ? [] : availableExports.value) {
    exports.push({
      label: t('artifactFrame.exportAs', { format: option.label }),
      icon: option.icon,
      disabled: isExporting.value,
      click: () => handleExport(option.format),
    });
  }

  const view: MenuItem[] = viewing.value ? [] : [
    { label: t('artifactFrame.fullScreen'), icon: 'i-heroicons-arrows-pointing-out', click: openFullscreen },
  ];
  if (props.report) {
    // What the editor works on, as /r showed until now: the dashboard's
    // draft, its newest version (phase 8). Whoever may not edit it lands on
    // its published page instead (SharedDashboardView). Without a dashboard
    // id (older rows), the conversation's /r as before.
    const dashboardId = selectedArtifact.value?.artifact_id;
    const viewedId = selectedArtifactItem.value?.artifact_id;
    const page = viewing.value && viewedId ? viewingPath(viewedId, viewing.value, { embed: false })
      : dashboardId ? `/d/${dashboardId}/draft` : `/r/${props.report.id}`;
    view.push({ label: t('artifactFrame.openInNewTab'), icon: 'i-heroicons-arrow-top-right-on-square', click: () => window.open(page, '_blank', 'noopener') });
  }

  if (selectedArtifact.value?.artifact_id && !props.verificationPreview) {
    // Resources and analytics are tabs of the dashboard's settings (the gear).
    const inspectors = [];
    // History: the conversations, versions and releases (Roll back) -- editors.
    if (dashboardCaps.value.includes('edit')) inspectors.push({ label: t('dashboardCollab.conversation.history'), icon: 'i-heroicons-clock', testid: 'history-menu', click: () => { settingsModalRef.value?.open('versions'); } });
    view.unshift(...inspectors);
  }
  return [edit, exports, view].filter(g => g.length > 0);
});

// --- Viewer-run gate state (per-user dashboards viewed by a non-owner) ---
// True when the backend hid the shared snapshot from this reader
// (viewer-identity sharing on user-scoped sources, or RLS): steps arrive with
// snapshot_withheld and empty data, and rendering the artifact against them
// crashes generated code that assumes rows exist. Mirror of /r/[id].
const snapshotWithheld = ref(false);
// Per-chart view of the same policy. A viewer refused ONE dataset used to lose
// the whole dashboard to the gate; now the charts they can read render and
// only these are called out (see showViewerGate).
const withheldCharts = ref<Array<{ title: string; noAccess: boolean }>>([]);
// Set when the charts named above are the OWNER's own failed steps (a fork
// whose hydration ran only part of the dashboard) rather than snapshots
// withheld from a viewer. Banner-only: it must never reach showViewerGate.
const ownerFailedCharts = ref(false);

// ── Brought in: the copies run again as this user ──────────────────────────
// A dashboard brought into this conversation (bring, "Edit vN here", "Edit in
// my conversation", Fork) arrives with its code and queries but no rows: the
// server reruns each copied step as the person who brought it, in the
// background. Whenever a load finds steps still pending, poll their statuses
// (no rows) until they settle, then load the data once. State-driven, so every
// entry point is covered, including a page reload mid-run.
type HydrationFailure = { queryId: string; title: string; reason: string | null; noAccess: boolean };
const hydration = ref<null | {
  phase: 'running' | 'failed' | 'timeout';
  total: number; done: number; failed: HydrationFailure[];
  rerunning: boolean; rerunError: string | null;
}>(null);
const HYDRATION_POLL_MS = 2000;
const HYDRATION_MAX_MS = 10 * 60 * 1000;
let hydrationTimer: ReturnType<typeof setTimeout> | null = null;
let hydrationToken = 0;
let hydrationStartedAt = 0;
let hydrationArtifactId: string | null = null;
const SETTLING = new Set(['pending', 'running']);

function stopHydrationPolling() {
  hydrationToken += 1;
  if (hydrationTimer) clearTimeout(hydrationTimer);
  hydrationTimer = null;
}
function dismissHydration() {
  stopHydrationPolling();
  hydration.value = null;
}
function failuresOf(statuses: any[]): HydrationFailure[] {
  return statuses.filter(s => s.status === 'error').map(s => ({
    queryId: s.query_id, title: s.title || '', reason: s.status_reason || null, noAccess: s.error_code === 'no_access',
  }));
}
// Called by fetchData with the steps it just loaded.
function watchHydration(artifactId: string | undefined | null, steps: any[]) {
  const settling = steps.filter(s => s && SETTLING.has(s.status));
  if (!settling.length || !artifactId || props.verificationPreview) {
    if (hydration.value?.phase === 'running') hydration.value = null;
    return;
  }
  if (hydration.value?.phase === 'running' && hydrationArtifactId === artifactId && hydrationTimer) return;
  stopHydrationPolling();
  hydrationArtifactId = artifactId;
  hydrationStartedAt = Date.now();
  hydration.value = {
    phase: 'running', total: steps.length, done: steps.length - settling.length,
    failed: [], rerunning: false, rerunError: null,
  };
  scheduleHydrationPoll(hydrationToken);
}
function scheduleHydrationPoll(token: number) {
  if (token !== hydrationToken) return;
  if (typeof document !== 'undefined' && document.hidden) return; // resumed on visibilitychange
  hydrationTimer = setTimeout(() => pollHydration(token), HYDRATION_POLL_MS);
}
async function pollHydration(token: number) {
  hydrationTimer = null;
  const artifactId = hydrationArtifactId;
  if (token !== hydrationToken || !artifactId || !hydration.value) return;
  if (Date.now() - hydrationStartedAt > HYDRATION_MAX_MS) {
    hydration.value = { ...hydration.value, phase: 'timeout' };
    return;
  }
  let statuses: any[] | null = null;
  try {
    const { data, error } = await useMyFetch('/api/queries/statuses', {
      query: { report_id: props.reportId, artifact_id: artifactId },
    });
    if (!error.value && Array.isArray(data.value)) statuses = data.value as any[];
  } catch { /* a missed poll just waits for the next */ }
  if (token !== hydrationToken || artifactId !== selectedArtifactId.value) return;
  if (!statuses) { scheduleHydrationPoll(token); return; }
  const settling = statuses.filter(s => SETTLING.has(s.status));
  hydration.value = { ...hydration.value!, total: statuses.length, done: statuses.length - settling.length };
  if (settling.length) { scheduleHydrationPoll(token); return; }
  // Everything ran: load the rows once.
  const failed = failuresOf(statuses);
  await fetchData(artifactId, { inPlace: dataReady.value });
  if (token !== hydrationToken) return;
  hydration.value = failed.length
    ? { phase: 'failed', total: statuses.length, done: statuses.length, failed, rerunning: false, rerunError: null }
    : null;
}
async function reloadAfterHydration() {
  const artifactId = hydrationArtifactId || selectedArtifactId.value;
  dismissHydration();
  await fetchData(artifactId || undefined, { inPlace: dataReady.value });
}
// "Run again": the conversation is this user's, so the report rerun runs the
// dashboard's queries with their credentials and identity.
async function runFailedAgain() {
  const h = hydration.value;
  const artifactId = hydrationArtifactId || selectedArtifactId.value;
  if (!h || h.rerunning || !artifactId) return;
  hydration.value = { ...h, rerunning: true, rerunError: null };
  let rerunError: string | null = null;
  try {
    const { data, error } = await useMyFetch(`/api/reports/${props.reportId}/rerun?artifact_id=${encodeURIComponent(artifactId)}`, { method: 'POST' });
    if (error.value) rerunError = (error.value as any)?.data?.detail?.message || (error.value as any)?.data?.detail || t('artifactFrame.refreshFailed');
    else if ((data.value as any)?.skipped) rerunError = t('artifactFrame.hydrating.stillRunning');
    else if ((data.value as any)?.steps_failed) rerunError = t('artifactFrame.hydrating.stillFailing', { failed: (data.value as any).steps_failed });
  } catch (e: any) {
    rerunError = e?.message || t('artifactFrame.refreshFailed');
  }
  let statuses: any[] = [];
  try {
    const { data } = await useMyFetch('/api/queries/statuses', { query: { report_id: props.reportId, artifact_id: artifactId } });
    statuses = Array.isArray(data.value) ? data.value as any[] : [];
  } catch { /* keep the previous list */ }
  await fetchData(artifactId, { inPlace: dataReady.value });
  if (hydration.value?.phase === 'running') return; // fetchData found steps still running and is polling
  const failed = statuses.length ? failuresOf(statuses) : h.failed;
  hydration.value = failed.length
    ? { phase: 'failed', total: statuses.length || h.total, done: statuses.length || h.total, failed, rerunning: false,
        rerunError: typeof rerunError === 'string' ? rerunError : null }
    : null;
}
function onHydrationVisibility() {
  if (document.hidden) {
    if (hydrationTimer) { clearTimeout(hydrationTimer); hydrationTimer = null; }
  } else if (hydration.value?.phase === 'running' && !hydrationTimer) {
    pollHydration(hydrationToken);
  }
}
onMounted(() => document.addEventListener('visibilitychange', onHydrationVisibility));
onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onHydrationVisibility);
  stopHydrationPolling();
});
watch(selectedArtifactId, () => dismissHydration());
// Every query on the dashboard is withheld — nothing to render but the gate.
const allWithheld = ref(false);
// Some own run was refused by the provider (classified server-side as
// error_code 'no_access'), as opposed to breaking for another reason.
const viewerRunNoAccess = ref(false);
// Some own run stopped because the viewer must sign in to a connection first
// (error_code 'credentials_required') -- they HAVE access.
const viewerRunNeedsConnect = ref(false);
// The dashboard renders, but some of its charts are unavailable to this viewer.
const partiallyWithheld = computed(() =>
  (snapshotWithheld.value || ownerFailedCharts.value)
  && !showViewerGate.value && withheldCharts.value.length > 0);
// True when any of the dashboard's queries reads a delegated (per-user
// credential) source: View-as swaps identity params only — source-level rows
// still come back under the caller's own credentials, so the preview must
// carry a note saying the impersonated user's actual rows may differ.
const credentialScoped = ref(false);
// True when any step already carries this user's own per-viewer result row —
// auto-run only fires for a first-time viewer; a failed earlier run becomes an
// explicit fallback state instead of a retry loop.
const hasOwnResult = ref(false);
// status_reason of the viewer's failed result row, if their last run errored
const viewerRunFailedReason = ref<string | null>(null);
// Data sources whose viewer client couldn't be built on the last run
const dataSourceErrors = ref<Array<{ data_source: string; data_source_id?: string; code?: string; error: string }>>([]);
const isViewerRunning = ref(false);
// Auto-run fires at most once per mount
const autoRunTried = ref(false);
// The report's data sources (with connections), captured by fetchData for the
// gate's brand icon.
const reportSources = ref<any[]>([]);

const gateErrorMessage = computed(() =>
  dataSourceErrors.value[0]?.error || viewerRunFailedReason.value || null);

const gateSourceType = computed<string | null>(() => {
  const conns = reportSources.value.flatMap((ds: any) => ds.connections || []);
  const userScoped = conns.find((c: any) => c.auth_policy && c.auth_policy !== 'system_only');
  return (userScoped || conns[0])?.type || null;
});

// In-app the user is always signed in, so the gate never shows 'signin';
// the machine-readable data-source error codes pick the fallback action.
// The gate covers the dashboard only when there is nothing of it to show:
// before this viewer has any run of their own (it auto-runs and shows
// "loading"), while that run is in flight, or when EVERY query is withheld.
// Withheld is per query, so a viewer refused one dataset still gets every
// chart they can read — the fork makes the same call (keep what ran).
const showViewerGate = computed(() =>
  snapshotWithheld.value && (allWithheld.value || isViewerRunning.value || !hasOwnResult.value));

const gateState = computed<'loading' | 'signin' | 'connect' | 'no_access' | 'error' | 'ready'>(() => {
  if (isViewerRunning.value) return 'loading';
  const errs = dataSourceErrors.value;
  if (errs.some((e) => e.code === 'credentials_required') || viewerRunNeedsConnect.value) return 'connect';
  // Refused at the data-source level (no client) OR at query time (one
  // dataset) — both are "no access", never the provider's HTTP status.
  if (errs.some((e) => e.code === 'no_access') || viewerRunNoAccess.value) return 'no_access';
  if (errs.length > 0 || viewerRunFailedReason.value) return 'error';
  return 'ready';
});

// Run the dashboard's queries as the viewing user. Results land in the
// viewer's own step_user_results rows (the shared snapshot is never touched);
// whose credentials execute is the owner's "Run on my behalf" share setting.
async function runAsViewer() {
  if (isViewerRunning.value) return;
  isViewerRunning.value = true;
  try {
    const artifactParam = selectedArtifactId.value ? `?artifact_id=${selectedArtifactId.value}` : '';
    const { data, error } = await useMyFetch(`/api/r/${props.reportId}/run${artifactParam}`, { method: 'POST' });
    if (error.value) throw error.value;
    const run: any = data.value || {};
    dataSourceErrors.value = run.data_source_errors || [];
    // Reload step data: successful runs replace the withheld snapshot with
    // the viewer's own rows (and clear snapshotWithheld).
    await fetchData(selectedArtifactId.value);
  } catch (e: any) {
    const msg = e?.data?.detail || e?.message || 'Run failed';
    if (!viewerRunFailedReason.value) viewerRunFailedReason.value = msg;
  } finally {
    isViewerRunning.value = false;
  }
}

// "View as" preview (page artifacts): render the dashboard as another viewer
// would see it — anonymous, or any org member (picked by name/email search).
// IDENTITY ONLY — current_user is swapped, but the data payload is untouched:
// per-viewer data scoping happens server-side (viewer runs / RLS), so this
// must never be presented as an access check.
// NOTE: declared after selectedArtifact/isPendingArtifact/snapshotWithheld —
// the immediate watch below evaluates canViewAs during setup, so the refs it
// reads must already be initialized.
const viewAsMode = ref<string>('you');  // 'you' | 'anonymous' | <member user_id>
const previewViewer = ref<any>(null);   // fetched context of the picked member
// True when the picked member's viewer_context fetch failed or came back
// empty. The render still falls back to anonymous (never the wrong person),
// but the banner must SAY so — a banner claiming "Viewing as X" over an
// anonymous payload silently misrepresents what's on screen.
const previewViewerFailed = ref(false);
// True while a view-as switch is settling (member context fetch + identity
// param re-run + data push). Drives a full-pane spinner so the transient
// anonymous fallback never flashes as if it were the final render.
const previewLoading = ref(false);
const membersList = ref<any[]>([]);
const canViewAs = computed(() =>
  selectedArtifact.value?.mode === 'page' && !isPendingArtifact.value && !snapshotWithheld.value);

// Previewing as somebody else. Only the per-query run (/api/queries/{id}/run)
// carries run_as_user_id; both refresh endpoints resolve identity from the
// caller, so refreshing here would silently swap the slice on screen — and,
// for the owner, persist THEIR slice into the snapshot every reader sees.
// Exit view-as first.
const isViewAsActive = computed(() => canViewAs.value && viewAsMode.value !== 'you');

const viewAsOptions = computed(() => [
  { value: 'you', label: 'You', email: (viewerContext.value?.email as string) || '' },
  { value: 'anonymous', label: 'Anonymous user', email: '' },
  ...membersList.value
    .filter((m: any) => m.user?.id && String(m.user.id) !== String(viewerContext.value?.id))
    .map((m: any) => ({
      value: String(m.user.id),
      label: m.user.name || m.user.email || m.email || 'Member',
      email: m.user.email || m.email || '',
    })),
]);

const viewAsLabel = computed(() => {
  if (viewAsMode.value === 'you') return t('artifactFrame.viewAs');
  if (viewAsMode.value === 'anonymous') return t('artifactFrame.anonymousUser');
  const opt = viewAsOptions.value.find(o => o.value === viewAsMode.value);
  return opt?.label || 'Member';
});

const effectiveViewerContext = computed(() => {
  if (viewAsMode.value === 'anonymous') return null;
  if (viewAsMode.value === 'you') return viewerContext.value;
  // A member is picked but their context hasn't resolved yet → render
  // anonymously rather than as the wrong person.
  return previewViewer.value;
});

// Members load lazily, once, when the picker becomes available.
let membersFetched = false;
async function fetchMembersList() {
  if (membersFetched || !organization.value?.id) return;
  membersFetched = true;
  try {
    const { data } = await useMyFetch(`/api/organizations/${organization.value.id}/members`);
    membersList.value = Array.isArray(data.value) ? (data.value as any[]) : [];
  } catch {
    membersList.value = [];
  }
}
watch(canViewAs, (v) => { if (v) fetchMembersList(); }, { immediate: true });

async function fetchPreviewViewer(memberId: string) {
  previewViewerFailed.value = false;
  try {
    const { data, error } = await useMyFetch(`/api/users/${memberId}/viewer_context`);
    previewViewer.value = (!error.value && data.value) ? data.value : null;
  } catch {
    previewViewer.value = null;
  }
  if (!previewViewer.value) previewViewerFailed.value = true;
}

async function retryPreviewViewer() {
  const mode = viewAsMode.value;
  if (mode === 'you' || mode === 'anonymous') return;
  await fetchPreviewViewer(mode);
  if (iframeReady.value) sendDataToIframe();
}

watch(viewAsMode, async (mode) => {
  previewViewer.value = null;
  previewViewerFailed.value = false;
  previewLoading.value = true;
  try {
    if (mode !== 'you' && mode !== 'anonymous') {
      await fetchPreviewViewer(mode);
    }
    // Identity-scoped queries re-run as the previewed member (server-verified:
    // owner/admin only; results are preview-only, cached under the CALLER).
    // Back to 'you' re-scopes to the caller's own slice. Anonymous is
    // identity-display only — the data path needs an authenticated identity.
    try {
      if (mode !== 'anonymous' && queriesWithIdentityParams().length) {
        await runParamQueries(null, null, { identityOnly: true });
      }
    } catch (e) {
      console.error('[ArtifactFrame] View-as param re-run failed:', e);
    }
    // Swapping identity re-sends data into an already-mounted iframe (srcdoc
    // also recomputes, but postMessage covers the no-reload path).
    if (iframeReady.value) sendDataToIframe();
  } finally {
    previewLoading.value = false;
  }
});

// Docs open in edit mode by default for the report owner; everyone else gets
// the read-only viewer. Keyed on the loaded artifact so mode + ownership are
// known (selectedArtifactId changes before the artifact finishes fetching).
watch(selectedArtifact, (art) => {
  isEditingDoc.value = (art?.mode === 'doc') && isReportOwner.value;
}, { immediate: true });

async function saveDocEdit(markdown: string) {
  if (!selectedArtifactId.value) return;
  docEditorRef.value?.setSaving(true);
  try {
    const { data, error } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}/doc_edit`, {
      method: 'POST',
      body: { markdown },
    });
    if (error.value) {
      const detail = (error.value as any)?.data?.detail || t('docEditor.saveFailed');
      docEditorRef.value?.setError(String(detail));
      return;
    }
    const newArtifact: any = data.value;
    await fetchArtifactsList(true);
    if (newArtifact?.id) {
      // Reselect the new version; the selectedArtifact watcher re-enters edit
      // mode (owner) and the editor remounts via :key with the saved content.
      selectedArtifactId.value = newArtifact.id;
    }
    toast.add({ title: t('docEditor.saved'), color: 'green' });
  } catch (e: any) {
    docEditorRef.value?.setError(e?.message || t('docEditor.saveFailed'));
  } finally {
    docEditorRef.value?.setSaving(false);
  }
}

// PDF from the editor toolbar. The server renders the SAVED document, so
// exporting mid-edit would quietly hand back yesterday's text — say so instead.
function exportDocPdf() {
  if (docEditorRef.value?.hasUnsavedChanges?.()) {
    toast.add({
      title: t('docEditor.unsavedForExport'),
      color: 'amber',
    });
    return;
  }
  exportPdf();
}

// Download the doc's markdown source
function exportDocMarkdown() {
  const title = selectedArtifact.value?.title || 'document';
  const blob = new Blob([docMarkdown.value], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${title.replace(/[^\w\d֐-׿؀-ۿ -]+/g, '').trim() || 'document'}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// Generate dashboard prompt - dispatches event to update and submit prompt box
function generateDashboardPrompt() {
  const prompt = `Create a dashboard covering the data and visualizations created in this report. Design it with a clean, modern layout and narrative that presents the insights effectively.`;

  // Dispatch custom event to update and auto-submit the prompt box
  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: true }
  }));
}

// Fix render error - prefill prompt with error details
function fixRenderError() {
  const errorMsg = iframeError.value || 'Unknown error';
  const artifactTitle = selectedArtifact.value?.title || 'the dashboard';
  const artifactId = selectedArtifact.value?.id || selectedArtifactId.value || '';
  const prompt = `The dashboard "${artifactTitle}" (artifact_id: ${artifactId}) failed to render with this error:\n\`\`\`\n${errorMsg}\n\`\`\`\nPlease fix the artifact code so it renders correctly.`;

  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: false }
  }));
}

// Handle artifact:select event (select a specific artifact by ID)
async function handleArtifactSelect(event: Event) {
  const artifactId = (event as CustomEvent).detail?.artifact_id;
  if (!artifactId) return;
  // Unknown id ≠ bad id: the list may simply be stale (artifact created while
  // the pane was closed, a missed artifact:created event, another tab).
  // Refresh once and retry instead of silently dropping the user's click.
  if (!artifactsList.value.some(a => a.id === artifactId)) {
    await fetchArtifactsList(true);
  }
  if (artifactsList.value.some(a => a.id === artifactId)) {
    selectedArtifactId.value = artifactId;
  }
}

// A creation from a run this viewer didn't start (another tab, another user,
// a scheduled run) — offered via a pill instead of taking over the pane.
const offeredArtifactId = ref<string | null>(null);

async function acceptOfferedArtifact() {
  const id = offeredArtifactId.value;
  offeredArtifactId.value = null;
  if (!id) return;
  await switchToArtifact(id);
}

async function switchToArtifact(artifactId: string) {
  // Reset dataReady BEFORE selecting the new artifact so iframeSrcdoc doesn't
  // render new code (with viz[N] refs) against stale visualization data.
  dataReady.value = false;
  await fetchArtifactsList(true);
  selectedArtifactId.value = artifactId;
  // Force refetch in case same artifact transitioned from pending to completed
  await fetchSelectedArtifact();
  // Fetch visualization data for the new artifact before rendering
  await fetchData(artifactId);
}

// Handle artifact:created event (refresh list; select the new artifact only
// when the creation belongs to this viewer's own run — select !== false —
// or updates the artifact already on screen)
async function handleArtifactCreated(event: Event) {
  const detail = (event as CustomEvent).detail || {};
  if (detail.report_id && detail.report_id !== props.reportId) return;
  const artifactId = detail.artifact_id;
  const takeover = detail.select !== false;
  if (!takeover && artifactId && selectedArtifactId.value &&
      artifactId !== selectedArtifactId.value) {
    await fetchArtifactsList(true);
    // A new version of the dashboard on screen (this conversation's own run,
    // re-attached after a reload, counts as "elsewhere"): follow it, so
    // Publish and the next edit act on it and not on the one before.
    const created = artifactsList.value.find(a => a.id === artifactId);
    const onScreen = selectedArtifactItem.value;
    if (created?.artifact_id && created.artifact_id === onScreen?.artifact_id) {
      await switchToArtifact(artifactId);
      return;
    }
    offeredArtifactId.value = artifactId;
    return;
  }
  if (artifactId) {
    await switchToArtifact(artifactId);
  } else {
    dataReady.value = false;
    await fetchArtifactsList(true);
  }
}

// Load artifacts and data on mount
onMounted(async () => {
  window.addEventListener('message', handleIframeMessage);
  window.addEventListener('artifact:select', handleArtifactSelect);
  window.addEventListener('artifact:created', handleArtifactCreated);

  // Honour an explicit open request before anything auto-selects. Seeding here
  // (not via the event) makes the choice independent of mount timing:
  // fetchArtifactsList only auto-picks artifactsList[0] when nothing is
  // selected, so with this set it leaves the requested artifact alone.
  if (props.requestedArtifactId) {
    selectedArtifactId.value = props.requestedArtifactId;
  }

  // First fetch artifact list to know which artifact is selected (viewer
  // identity in parallel — it gates nothing, a failure just renders anonymous)
  await Promise.all([fetchArtifactsList(), fetchViewerContext()]);
  // ?version=N opens that version of the dashboard (a link to one version);
  // without it, the newest -- what the next edit builds on.
  const askedVersion = Number(paramRoute.query.version);
  if (!props.requestedArtifactId && Number.isInteger(askedVersion) && askedVersion > 0) {
    const key = selectedDashboardKey.value;
    const hit = artifactsList.value.find(a => dashboardKeyOf(a) === key && a.version === askedVersion);
    if (hit) selectedArtifactId.value = hit.id;
    // One made in another conversation: view it here, read-only.
    else if (key) viewAskedVersion(askedVersion);
  }

  // Load the selected artifact exactly once after initial selection. The
  // report page already fetched the latest artifact; reuse that object when
  // it is the one selected instead of fetching it again. Any mismatch (doc
  // selected first, prop not resolved yet) falls back to the normal fetch.
  const seed = props.latestArtifact as any;
  if (seed?.id && seed.id === selectedArtifactId.value && seed?.content) {
    selectedArtifact.value = seed;
    const vizIds = seed?.content?.visualization_ids || [];
    window.dispatchEvent(new CustomEvent('artifact:viz-ids', { detail: { visualization_ids: vizIds } }));
  } else {
    await fetchSelectedArtifact();
  }

  // Then fetch visualization data filtered by the selected artifact (if any)
  await fetchData(selectedArtifactId.value);

  // Withheld dashboard, non-owner, no result of their own yet: run the
  // queries for them instead of rendering the artifact against empty data —
  // the gate shows "Loading your data" while it's in flight. Fires once per
  // mount and only for first-time viewers; a viewer whose earlier run failed
  // gets the explicit fallback state instead of a retry loop. (The backend
  // never withholds from the report owner, so this cannot fire for owners.)
  await autoRunForViewer();

  // Paint first, refresh second (refresh_on_view, default-on): the server
  // enforces the flag, a staleness gate, and a cross-worker single-flight
  // claim, so this is at most one owner-credential rerun per interval no
  // matter how many people open the page. Mirrors the /r host.
  try {
    if (props.verificationPreview) return; // Verification only runs explicit viewer queries.
    // The dashboard open here decides (its "refresh when opened", phase 9).
    const open = selectedArtifact.value?.id;
    const { data } = await useMyFetch(`/api/r/${props.reportId}/rerun${open ? `?artifact_id=${encodeURIComponent(open)}` : ''}`, { method: 'POST' });
    const run: any = data.value;
    if (run && !run.skipped && run.steps_succeeded) {
      await refreshDataInPlace();
      // A refresh drops every viewer's own results: run again for them (one
      // more run, whatever ran before), as on a first open.
      autoRunTried.value = false;
      await autoRunForViewer();
    }
  } catch { /* a failed background refresh must never break a rendered page */ }
});

// Also when their last run stopped on a missing sign-in: only a run names the
// connection to connect, and they may have connected since.
async function autoRunForViewer() {
  if (!snapshotWithheld.value || isReportOwner.value || isViewerRunning.value || autoRunTried.value) return;
  const needsRun = !hasOwnResult.value || (viewerRunNeedsConnect.value && dataSourceErrors.value.length === 0);
  if (!needsRun) return;
  autoRunTried.value = true;
  await runAsViewer();
}

// Fetch list of all artifacts for the report
async function fetchArtifactsList(force = false) {
  try {
    let artifacts: ArtifactItem[] | null = null;
    // An EMPTY prop array is "not seeded yet", not an answer: the report page
    // fetches the report once, so a pane opened after an artifact was created
    // would otherwise trust a stale [] and show "No artifacts yet" forever.
    if (!force && Array.isArray(props.artifacts) && props.artifacts.length > 0) {
      artifacts = props.artifacts;
    } else {
      const { data } = await useMyFetch(`/artifacts/report/${props.reportId}`);
      if (data.value && Array.isArray(data.value)) {
        artifacts = data.value as ArtifactItem[];
      }
    }
    if (artifacts) {
      artifactsList.value = [...artifacts];

      // Auto-select the most recent artifact — but never move a deliberate
      // selection: only when nothing is selected yet or the selected id no
      // longer exists (refreshes used to snap the pane to the newest).
      if (artifactsList.value.length > 0) {
        const stillExists = !!selectedArtifactId.value &&
          artifactsList.value.some(a => a.id === selectedArtifactId.value);
        if (!stillExists) {
          selectedArtifactId.value = newestVersionOf(artifactsList.value, props.requestedDashboardId)
            || artifactsList.value[0].id;
        }
      }
    }
  } catch (e) {
    console.log('[ArtifactFrame] No artifacts found');
  }
}

watch(selectedArtifactId, (id) => {
  if (id && id === offeredArtifactId.value) offeredArtifactId.value = null;
});

// Fetch the full artifact content when selection changes
async function fetchSelectedArtifact() {
  if (!selectedArtifactId.value) {
    selectedArtifact.value = null;
    return;
  }

  try {
    const { data } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}`);
    if (data.value) {
      selectedArtifact.value = data.value;
      console.log('[ArtifactFrame] Loaded artifact:', (data.value as any).title);
      // Broadcast active artifact viz IDs so ToolWidgetPreview can show "Added to Dashboard"
      const vizIds = (data.value as any)?.content?.visualization_ids || [];
      window.dispatchEvent(new CustomEvent('artifact:viz-ids', { detail: { visualization_ids: vizIds } }));
    }
  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch artifact:', e);
  }
}

// Watch for artifact selection changes - refetch data filtered by new artifact
watch(selectedArtifactId, async (newId, oldId) => {
  // Initial selection is loaded explicitly by onMounted so artifact detail and
  // query data have one deterministic request each.
  if (oldId === undefined) return;
  iframeError.value = null;
  iframeReady.value = false;
  viewAsMode.value = 'you';
  if (isPolishMode.value) exitPolishMode();
  await fetchSelectedArtifact();
  // Only refetch data if this is a user-initiated change (not initial load)
  if (oldId !== undefined) {
    await fetchData(newId);
  }
});

onUnmounted(() => {
  window.removeEventListener('message', handleIframeMessage);
  window.removeEventListener('artifact:select', handleArtifactSelect);
  window.removeEventListener('artifact:created', handleArtifactCreated);
  if (isPolishMode.value) exitPolishMode();
});

// Handle messages from iframe
function handleIframeMessage(event: MessageEvent) {
  if (event.source !== iframeRef.value?.contentWindow && event.source !== fullscreenRuntimeFrame.value?.contentWindow) return;
  if (event.data?.type === 'ARTIFACT_DATA_RECEIVED') {
    verificationEvent('data_received', { data_revision: event.data.revision });
    return;
  }
  if (event.data?.type === 'ARTIFACT_READY' && event.source === fullscreenRuntimeFrame.value?.contentWindow) {
    fullscreenReady.value = true;
    sendDataToIframe();
  } else if (event.data?.type === 'ARTIFACT_READY') {
    console.log('[ArtifactFrame] Iframe ready');
    iframeError.value = null;
    iframeReady.value = true;
    sendDataToIframe();
  } else if (event.data?.type === 'ARTIFACT_ERROR') {
    verificationEvent('error', { source: 'runtime', message: event.data.payload?.message });
    console.error('[ArtifactFrame] Iframe render error:', event.data.payload?.message);
    iframeError.value = event.data.payload?.message || 'Unknown render error';
  } else if (event.data?.type === 'POLISH_ELEMENT_SELECTED') {
    polishSelectedElement.value = event.data.element;
    polishPromptVisible.value = true;
    polishInstruction.value = '';
    nextTick(() => polishInputRef.value?.focus());
  } else if (event.data?.type === 'ARTIFACT_SET_PARAMS') {
    // A control in the artifact committed param changes: run the consuming
    // queries server-side and push fresh rows back down (to both frames).
    paramAckSeq = Math.max(paramAckSeq, Number(event.data.seq) || 0);
    runParamQueries(event.data.changes || {}, event.data.targets || null, {});
  } else if (event.data?.type === 'ARTIFACT_REFRESH_PARAMS') {
    runParamQueries(null, event.data.targets || null, { force: true });
  }
}

// Send data to iframe via postMessage
function sendDataToIframe() {
  const targets = [
    iframeReady.value ? iframeRef.value?.contentWindow : null,
    fullscreenReady.value ? fullscreenRuntimeFrame.value?.contentWindow : null,
  ].filter((w): w is Window => !!w);
  if (!targets.length) return;

  const payload = JSON.parse(JSON.stringify({
    report: toRaw(reportData.value),
    visualizations: toRaw(visualizationsData.value),
    files: toRaw(filesData.value),
    current_user: toRaw(effectiveViewerContext.value),
    params: paramsPayload(),
    runtime: artifactRuntime(),
    ...(props.verificationPreview ? { verification_revision: ++verificationRevision } : {})
  }));

  verificationEvent('data_sent', {
    data_revision: verificationRevision,
    request_ids: [...new Set(visualizationsData.value.map(v => verificationRequests.get(v.queryId)).filter(Boolean))],
  });
  try {
    for (const target of targets) target.postMessage({ type: 'ARTIFACT_DATA', payload }, '*');
  } catch (err: any) {
    console.error('[ArtifactFrame] Failed to send data to iframe:', err);
    iframeError.value = err?.message || 'Failed to send data to dashboard iframe';
    return;
  }

  dataReady.value = true;
  console.log('[ArtifactFrame] Data sent to iframe:', visualizationsData.value.length, 'visualizations');
}

// Fetch visualization data for the report (optionally filtered by artifact).
// `inPlace` refreshes the rows of a dashboard that is already rendered: no
// loading overlay, no srcdoc rebuild (which reloads the iframe — a visible
// blank flash) and no reset of the viewer's control values; the fresh rows
// reach the live document through postMessage like a param run.
async function fetchData(artifactId?: string, { inPlace = false }: { inPlace?: boolean } = {}) {
  if (!inPlace) {
    isLoading.value = true;
    dataReady.value = false;
  }

  try {
    // Fetch report info
    let reportDataSources: any[] = [];
    let reportSnapshot: any = props.report || null;
    if (!reportSnapshot) {
      const { data: reportRes } = await useMyFetch(`/api/reports/${props.reportId}`);
      reportSnapshot = reportRes.value || null;
    }
    if (reportSnapshot) {
      reportData.value = {
        id: reportSnapshot.id,
        title: reportSnapshot.title,
        theme: reportSnapshot.theme_name || reportSnapshot.report_theme_name
      };
      reportDataSources = reportSnapshot.data_sources || [];
    }
    reportSources.value = reportDataSources;
    // If the report uses a single data source, surface its name on every viz.
    const singleDataSourceName = reportDataSources.length === 1
      ? (reportDataSources[0]?.name || reportDataSources[0]?.title || null)
      : null;

    // Fetch queries with visualizations - filter by artifact_id if provided
    const queryParams = artifactId ? `?report_id=${props.reportId}&artifact_id=${artifactId}` : `?report_id=${props.reportId}`;
    // First load: the report page may already have this exact request in
    // flight (started before this frame mounted).
    let prefetched: any = null;
    const pf = props.prefetch;
    if (!inPlace && pf?.queries && !pf.queriesUsed && !props.verificationPreview && artifactId && pf.artifactId === artifactId) {
      pf.queriesUsed = true;
      prefetched = await pf.queries;
      if (prefetched?.error?.value || !Array.isArray(prefetched?.data?.value)) prefetched = null;
    }
    const { data: queriesRes } = prefetched || await useMyFetch(`/api/queries${queryParams}`);
    const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

    // Build visualization data array
    const vizData: any[] = [];
    let anyWithheld = false;
    let anyOwnResult = false;
    let anyCredentialScoped = false;
    let failedReason: string | null = null;
    const nextWithheldCharts: Array<{ title: string; noAccess: boolean }> = [];
    let visibleQueries = 0;
    // The owner's own steps that hydration could not run — banner, not gate.
    let anyOwnerFailed = false;
    let anyNoAccess = false;
    let anyNeedsConnect = false;

    const nextParamSpecs: Record<string, any[]> = {};
    for (let qi = 0; qi < queries.length; qi++) {
      const query = queries[qi];
      // list_queries already embeds the viewer-safe default Step. Re-fetching
      // it once per query duplicated the dashboard waterfall and payload.
      const step = query.default_step;

      // Declared query parameters (ParamSpec dicts) drive artifact controls.
      if (Array.isArray(query.parameters) && query.parameters.length) {
        nextParamSpecs[query.id] = query.parameters;
      }

      // Delegated (per-user credential) sources: View-as can only swap
      // identity params, not the target's source credentials — the note on
      // the View-as control keys off this.
      if (query.credential_scoped) anyCredentialScoped = true;

      // Per-viewer step-data policy markers: withheld snapshots gate the
      // render; an existing per-viewer result row gates auto-run.
      const vr = step?.viewer_result;
      if (step?.snapshot_withheld) {
        anyWithheld = true;
        nextWithheldCharts.push({
          title: query.title || 'Untitled',
          noAccess: vr?.error_code === 'no_access',
        });
      } else if (step?.status === 'error' && step?.status_reason) {
        // The step's OWNER seeing their own failed step — a forker whose
        // hydration could run only part of the dashboard. `snapshot_withheld`
        // is a viewer-only marker, always false here, so without this branch
        // the two sentences hydrate_fork carefully writes onto the step reached
        // nobody: the charts it could not run rendered blank, with no banner
        // and no explanation. `error_code` tells "you may not read this" from
        // "this query broke" without matching on prose.
        //
        // Sets its own flag, NOT `anyWithheld`: that one drives showViewerGate,
        // and the owner must never be shown the viewer's sign-in/connect gate
        // for their own fork. This feeds the banner alone.
        anyOwnerFailed = true;
        nextWithheldCharts.push({
          title: query.title || 'Untitled',
          noAccess: step.error_code === 'no_access',
        });
      } else if (step) {
        // Only a step that actually loaded counts as viewable. A failed fetch
        // (or a query with no default step) leaves `step` null, and
        // `snapshot_withheld` then reads undefined — which counted it as
        // visible, lifted the gate, and ran the artifact against rows that are
        // uniformly empty: the render crash the gate exists to prevent. An
        // unreadable step is not a readable one, so it stays out of the count.
        visibleQueries += 1;
      }
      if (vr) {
        anyOwnResult = true;
        if (vr.status === 'error' && !failedReason) failedReason = vr.status_reason || null;
        if (vr.status === 'error' && vr.error_code === 'no_access') anyNoAccess = true;
        if (vr.status === 'error' && vr.error_code === 'credentials_required') anyNeedsConnect = true;
      }

      // Process each visualization in the query
      for (const viz of query.visualizations || []) {
        vizData.push({
          id: viz.id,
          title: viz.title || query.title || 'Untitled',
          view: viz.view || {},
          rows: step?.data?.rows || [],
          columns: step?.data?.columns || [],
          dataModel: step?.data_model || {},
          stepStatus: step?.status,
          // Withheld from this viewer: rows are empty on purpose. Exposed so
          // artifact code (and anything reading ARTIFACT_DATA) can tell an
          // unavailable chart from one with genuinely no rows.
          unavailable: !!step?.snapshot_withheld || step?.status === 'error',
          // The reason this chart is empty, in the words the server chose.
          // Empty for a chart that simply has no rows.
          unavailableReason: step?.status === 'error' ? (step?.status_reason || '') : '',
          // Provenance surfaced in the built-in InfoPopover on prebuilt comps
          code: step?.code || '',
          description: viz.description || query.description || step?.description || '',
          dataSource: singleDataSourceName,
          queryId: query.id,
        });
      }
    }
    queryParamSpecs.value = nextParamSpecs;
    watchHydration(artifactId, queries.map((q: any) => q.default_step).filter((st: any) => st && !st.snapshot_withheld));

    // Stable control choices (static options + options-source query rows).
    await resolveParamOptions(queries, async () => {
      const { data } = await useMyFetch(`/api/queries?report_id=${props.reportId}`);
      return Array.isArray(data.value) ? (data.value as any[]) : [];
    });

    // Initialize applied values: declaration defaults, overridden by any
    // qp_<name> URL state (shareable dashboards). Identity params carry no
    // client value — the server binds them per viewer. An in-place refresh
    // keeps whatever the viewer has applied since.
    if (!inPlace) {
      const urlValues = readParamsFromUrl();
      const nextValues: Record<string, any> = {};
      for (const specs of Object.values(nextParamSpecs)) {
        for (const spec of specs as any[]) {
          if (spec.source === 'identity') continue;
          if (Object.prototype.hasOwnProperty.call(urlValues, spec.name)) {
            nextValues[spec.name] = urlValues[spec.name];
          } else if (spec.default !== undefined && spec.default !== null) {
            nextValues[spec.name] = spec.default;
          }
        }
      }
      paramValues.value = nextValues;
    }

    // Reorder vizData to match artifact's visualization_ids order
    // (artifact code references viz[0], viz[1], etc. by index)
    // These are newly fetched snapshots, not the previous parameter-run results.
    verificationRequests.clear();
    const vizIds = selectedArtifact.value?.content?.visualization_ids;
    if (vizIds && vizIds.length > 0) {
      const vizMap = new Map(vizData.map(v => [v.id, v]));
      const ordered = vizIds.map((id: string) => vizMap.get(id)).filter(Boolean);
      // Append any extras not in visualization_ids
      const orderedIds = new Set(vizIds);
      for (const v of vizData) {
        if (!orderedIds.has(v.id)) ordered.push(v);
      }
      visualizationsData.value = ordered;
    } else {
      visualizationsData.value = vizData;
    }
    snapshotWithheld.value = anyWithheld;
    withheldCharts.value = nextWithheldCharts;
    ownerFailedCharts.value = anyOwnerFailed;
    allWithheld.value = queries.length > 0 && visibleQueries === 0;
    viewerRunNoAccess.value = anyNoAccess;
    viewerRunNeedsConnect.value = anyNeedsConnect;
    hasOwnResult.value = anyOwnResult;
    credentialScoped.value = anyCredentialScoped;
    viewerRunFailedReason.value = failedReason;
    console.log('[ArtifactFrame] Fetched', visualizationsData.value.length, 'visualizations');

    // Resolve embedded files (generated images / uploaded images/PDFs) to data URIs.
    try {
      filesData.value = await fetchArtifactFiles();
      if (filesData.value.length) {
        console.log('[ArtifactFrame] Fetched', filesData.value.length, 'embedded file(s)');
      }
    } catch (e) {
      console.error('[ArtifactFrame] Failed to fetch embedded files:', e);
      filesData.value = [];
    }

    // Identity-scoped queries apply the viewer's identity BEFORE first paint:
    // the shared snapshot is the creator's slice, so run those queries as the
    // viewer now (server binds identity; results are per-viewer cached, so a
    // revisit is a cache hit). URL-restored input values apply here too.
    try {
      const urlValueNames = Object.keys(readParamsFromUrl());
      if (urlValueNames.length) {
        await runParamQueries(null, null, {});
      } else if (queriesWithIdentityParams().length) {
        await runParamQueries(null, null, { identityOnly: true });
      }
    } catch (e) {
      console.error('[ArtifactFrame] Initial param run failed:', e);
    }

    // Freeze the srcdoc seed: the iframe's initial document embeds THIS
    // snapshot. Later updates (param runs, view-as swaps) flow via
    // postMessage into the live data store — never by recomputing srcdoc,
    // which would reload the iframe and lose control state.
    if (!inPlace) srcdocSeed.value = JSON.parse(JSON.stringify({
      report: toRaw(reportData.value),
      visualizations: toRaw(visualizationsData.value),
      files: toRaw(filesData.value),
      current_user: toRaw(effectiveViewerContext.value),
      params: paramsPayload(),
      runtime: artifactRuntime(),
    }));

    // The iframe libraries were requested at mount, in parallel with the data
    // above — usually they are here already. Waiting keeps the loading
    // overlay up instead of a blank frame.
    if (!inPlace) {
      const code = props.artifactCode || selectedArtifact.value?.content?.code;
      if (code) {
        try { await whenArtifactLibsSettled(inlineableArtifactLibs(frameIframeOptions(srcdocSeed.value, code))); } catch { /* falls back to <script src> */ }
      }
    }

    // Mark data as ready - triggers iframeSrcdoc to compute with loaded data
    dataReady.value = true;

  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch data:', e);
  } finally {
    isLoading.value = false;
    if (iframeReady.value) {
      sendDataToIframe();
    }
  }
}

// Refresh-on-view finished with new rows: the artifact itself is unchanged,
// so update the data of the rendered dashboard rather than reloading it.
// Before the first paint there is nothing to keep — take the full path.
async function refreshDataInPlace() {
  await fetchData(selectedArtifactId.value, { inPlace: dataReady.value });
}

// Refresh everything
async function refreshAll() {
  await fetchArtifactsList(true);
  await fetchSelectedArtifact();
  await fetchData(selectedArtifactId.value);
}

// Called when iframe loads
function onIframeLoad() {
  // Iframe loaded, but we wait for ARTIFACT_READY message
}

// Sample React code for when no artifact exists
const sampleArtifactCode = computed(() => {
  const SC = '</' + 'script>';
  return `
<script type="text/babel">
// Default Artifact - Create one with the agent!
function App() {
  const data = useArtifactData();

  if (!data) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-pulse text-ink-muted">Loading...</div>
      </div>
    );
  }

  const { report, visualizations } = data;

  return (
    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 dark:from-slate-900 dark:to-slate-950 p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 dark:text-gray-100 tracking-tight">
          {report?.title || 'Dashboard'}
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-2">
          {visualizations.length} visualization{visualizations.length !== 1 ? 's' : ''} available
        </p>
      </div>

      {/* Empty state */}
      {visualizations.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-ink-muted" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-1">No visualizations yet</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 max-w-sm">
            Ask the agent to create visualizations, then generate an artifact to see them here.
          </p>
        </div>
      ) : (
        /* Grid of visualizations */
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {visualizations.map((viz) => (
            <VisualizationCard key={viz.id} viz={viz} />
          ))}
        </div>
      )}
    </div>
  );
}

function VisualizationCard({ viz }) {
  const chartRef = React.useRef(null);
  const chartInstance = React.useRef(null);

  React.useEffect(() => {
    if (!chartRef.current || !viz.rows?.length) return;

    if (chartInstance.current) {
      chartInstance.current.dispose();
    }

    const chart = echarts.init(chartRef.current);
    chartInstance.current = chart;

    const options = buildChartOptions(viz);
    if (options) {
      chart.setOption(options);
    }

    const resizeHandler = () => chart.resize();
    window.addEventListener('resize', resizeHandler);

    return () => {
      window.removeEventListener('resize', resizeHandler);
      chart.dispose();
    };
  }, [viz]);

  const viewType = viz.view?.view?.type || viz.view?.type || viz.dataModel?.type || 'table';

  return (
    <div className="bg-white dark:bg-gray-900 rounded-2xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden hover:shadow-md transition-shadow">
      <div className="px-5 py-4 border-b border-gray-50 dark:border-gray-800">
        <h3 className="font-semibold text-gray-900 dark:text-gray-100">{viz.title}</h3>
        <span className="text-xs text-ink-muted uppercase tracking-wide">{viewType}</span>
      </div>
      <div className="p-5">
        {viz.rows?.length > 0 ? (
          viewType === 'table' ? (
            <TableView data={viz} />
          ) : (
            <div ref={chartRef} className="h-72 w-full" />
          )
        ) : (
          <div className="h-72 flex items-center justify-center text-ink-muted">
            No data available
          </div>
        )}
      </div>
      <div className="px-5 py-3 bg-gray-50/50 dark:bg-gray-800/50 text-xs text-gray-500 dark:text-gray-400">
        {viz.rows?.length || 0} rows
      </div>
    </div>
  );
}

function TableView({ data }) {
  const { rows, columns } = data;
  const cols = columns?.length
    ? columns.map(c => c.field || c.colId || c.headerName)
    : Object.keys(rows[0] || {});

  return (
    <div className="overflow-x-auto max-h-72 rounded-lg border border-gray-100 dark:border-gray-700">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
          <tr>
            {cols.slice(0, 6).map((col) => (
              <th key={col} className="text-left px-3 py-2 font-medium text-gray-600 dark:text-gray-300 border-b border-gray-100 dark:border-gray-700">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row, i) => (
            <tr key={i} className="border-b border-gray-50 dark:border-gray-800 hover:bg-gray-50/50 dark:hover:bg-gray-800/50">
              {cols.slice(0, 6).map((col) => (
                <td key={col} className="px-3 py-2 text-gray-700 dark:text-gray-200">
                  {formatValue(row[col] ?? row[col.toLowerCase()])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 10 && (
        <div className="text-xs text-ink-muted p-2 text-center bg-gray-50 dark:bg-gray-800">
          Showing 10 of {rows.length} rows
        </div>
      )}
    </div>
  );
}

function formatValue(val) {
  if (val === null || val === undefined) return '-';
  if (typeof val === 'number') return val.toLocaleString();
  return String(val);
}

function buildChartOptions(viz) {
  const { rows, view, dataModel } = viz;
  if (!rows?.length) return null;

  const type = (view?.view?.type || view?.type || dataModel?.type || '').toLowerCase();
  const colors = view?.view?.palette?.colors || ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

  const normalizedRows = rows.map(r => {
    const o = {};
    Object.keys(r).forEach(k => o[k.toLowerCase()] = r[k]);
    return o;
  });

  const series = dataModel?.series?.[0] || {};
  const categoryKey = (view?.view?.x || series.key || Object.keys(normalizedRows[0])[0])?.toLowerCase();
  const valueKey = (view?.view?.y || series.value || Object.keys(normalizedRows[0])[1])?.toLowerCase();

  if (!categoryKey) return null;

  const categories = [...new Set(normalizedRows.map(r => String(r[categoryKey] || '')))];
  const values = categories.map(cat => {
    const row = normalizedRows.find(r => String(r[categoryKey]) === cat);
    const v = row ? Number(row[valueKey]) : 0;
    return isNaN(v) ? 0 : v;
  });

  if (type === 'pie_chart' || type === 'pie') {
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['45%', '75%'],
        center: ['50%', '50%'],
        data: categories.map((name, i) => ({
          name,
          value: values[i],
          itemStyle: { color: colors[i % colors.length] }
        })),
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } }
      }]
    };
  }

  if (type === 'bar_chart' || type === 'bar' || !type || type === 'table') {
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'bar',
        data: values,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] },
            { offset: 1, color: colors[0] + '80' }
          ]),
          borderRadius: [6, 6, 0, 0]
        },
        barMaxWidth: 50
      }]
    };
  }

  if (type === 'line_chart' || type === 'line' || type === 'area_chart' || type === 'area') {
    const isArea = type.includes('area');
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'line',
        data: values,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        itemStyle: { color: colors[0] },
        lineStyle: { width: 3 },
        areaStyle: isArea ? {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] + '40' },
            { offset: 1, color: colors[0] + '05' }
          ])
        } : undefined
      }]
    };
  }

  return null;
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
${SC}
`;
});

// Build the full iframe srcdoc with embedded data
// Guard: only compute once ALL data is ready to prevent iframe loading with empty data
const iframeSrcdoc = computed(() => {
  // Docs render in DocViewer, never in the sandbox iframe
  if (isDocMode.value) return undefined;

  // Wait for visualization data to be loaded
  if (!dataReady.value) return undefined;

  // Nothing of the dashboard is viewable yet: generated artifact code
  // routinely assumes rows exist, so don't execute it against all-empty data —
  // the ViewerRunGate covers this state. With SOME charts withheld it does
  // run: those charts get empty rows (flagged `unavailable`) and the banner
  // names them; a render failure then gets a viewer-appropriate message
  // instead of "Fix Error".
  if (showViewerGate.value) return undefined;

  // Slides without previews carry python-pptx source; it must never be
  // injected into the iframe (the browser would render it as raw text).
  if (slidesPreviewsMissing.value) return undefined;

  // If artifacts exist, wait for the selected artifact to be fully loaded
  if (artifactsList.value.length > 0 && !selectedArtifact.value?.content?.code) return undefined;

  // Priority: props > selected artifact from DB > sample code
  const artifactCode = props.artifactCode
    || selectedArtifact.value?.content?.code
    || sampleArtifactCode.value;

  // Frozen seed (set once per data load) so live updates don't reload the
  // iframe; falls back to live refs for the pre-data sample path.
  const seed = srcdocSeed.value || {
    report: reportData.value,
    visualizations: visualizationsData.value,
    files: filesData.value,
    current_user: effectiveViewerContext.value,
    params: paramsPayload(),
    runtime: artifactRuntime(),
  };
  const opts = frameIframeOptions(seed, artifactCode);
  // Wait for the host-fetched libraries (failed ones fall back to <script src>).
  const inlineLibs = artifactLibSources(inlineableArtifactLibs(opts));
  if (inlineLibs === null) return undefined;
  return buildArtifactIframeHtml({ ...opts, inlineLibs });
});

function frameIframeOptions(seed: any, artifactCode: string) {
  return {
    data: seed,
    code: artifactCode,
    mode: selectedArtifact.value?.mode || 'page',
    polishMode: !props.verificationPreview,
    fixtureMode: !!props.verificationPreview,
    resourceApp: selectedArtifact.value?.content?.sdk_version === 1,
    loadingLabel: t('artifactFrame.loadingArtifact'),
    reactBuild: 'development' as const,
    colorMode: artifactColorMode,
    // Used only when its key matches artifactCode (artifactIframe.ts), so an
    // artifactCode prop override or a stale selection always compiles in-frame.
    precompiled: { js: selectedArtifact.value?.compiled_js, key: selectedArtifact.value?.compiled_js_key },
  };
}

// Re-send data when it changes
watch([visualizationsData, filesData, iframeReady], () => {
  if (iframeReady.value && (visualizationsData.value.length > 0 || filesData.value.length > 0)) {
    sendDataToIframe();
  }
});
</script>
