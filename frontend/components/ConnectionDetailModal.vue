<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }">
    <div class="p-5">
      <!-- Header -->
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-3">
          <DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" class="h-6" />
          <div>
            <div class="font-medium text-gray-900 dark:text-white">{{ connection?.name }}</div>
            <div class="text-xs text-gray-400 dark:text-gray-500">{{ connection?.type }}</div>
          </div>
        </div>
        <button @click="isOpen = false" class="text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <!-- Status & Info -->
      <div class="space-y-3 py-4 border-t border-gray-100 dark:border-gray-800">
        <!-- Status -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.status') }}</span>
          <div class="flex items-center gap-2">
            <span :class="['w-2 h-2 rounded-full', isConnected ? 'bg-green-500' : 'bg-red-500']"></span>
            <span class="text-xs text-gray-700 dark:text-gray-300">{{ isConnected ? $t('data.connected') : $t('data.disconnected') }}</span>
          </div>
        </div>

        <!-- Tables (SQL connections) or Tools (MCP/custom_api) -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ isToolProvider ? $t('data.toolsLabel') : $t('data.tablesLabel') }}</span>
          <span class="text-xs text-gray-700 dark:text-gray-300">{{ isToolProvider ? toolCount : tableCount }}</span>
        </div>

        <!-- Data Agents -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.agentsLabel') }}</span>
          <span class="text-xs text-gray-700 dark:text-gray-300">{{ agentCount }}</span>
        </div>

        <!-- Last Checked -->
        <div class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.lastChecked') }}</span>
          <span class="text-xs text-gray-700 dark:text-gray-300">{{ lastCheckedDisplay || $t('data.never') }}</span>
        </div>

        <!-- Last Indexed (terminal state) — service-principal run, admin-only.
             Per-user viewers get their own "refreshed" line below instead. -->
        <div v-if="canUpdateDataSource && indexingState && !isIndexingActive(indexingState) && indexingState.finished_at" class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">Last indexed</span>
          <span class="text-xs text-gray-700 dark:text-gray-300">
            {{ lastIndexedDisplay }}
            <span v-if="indexingState.stats?.elapsed_s != null" class="text-gray-400 dark:text-gray-500">
              · {{ formatIndexDuration(indexingState.stats.elapsed_s) }}
            </span>
            <span v-if="indexingState.stats?.source_bytes" class="text-gray-400 dark:text-gray-500">
              · {{ formatIndexBytes(indexingState.stats.source_bytes) }}
            </span>
          </span>
        </div>

        <!-- Per-user "last refreshed" — when the viewer runs on their own creds,
             show when THEY last pulled their accessible tables (not the SP run). -->
        <div v-if="isPerUserViewer && myLastRefreshedDisplay" class="flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.lastRefreshed') }}</span>
          <span class="text-xs text-gray-700 dark:text-gray-300">{{ myLastRefreshedDisplay }}</span>
        </div>
      </div>

      <!-- Indexing block — service-principal run (live progress / logs / reindex).
           Admin-only: this is the shared catalog index, not the viewer's. -->
      <div v-if="canUpdateDataSource" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <ConnectionIndexingProgress
          v-if="indexingState"
          :indexing="indexingState"
          :show-logs="true"
          :allow-cancel="true"
          :cancelling="cancelling"
          @cancel="cancelIndexing"
        />
        <div v-if="!isIndexingActive(indexingState)" class="mt-2">
          <UButton size="xs" color="gray" variant="soft" :loading="reindexing" @click="reindex">
            <UIcon name="heroicons-arrow-path" class="w-3.5 h-3.5 me-1" />
            {{ indexingState?.status === 'failed' ? 'Retry' : 'Reindex' }}
          </UButton>
        </div>
      </div>

      <!-- Auto-reindex schedule (enterprise `scheduled_reindex`). Admin-only.
           Periodically re-indexes the shared catalog so tables stay fresh
           without a manual reindex. -->
      <div v-if="canUpdateDataSource && !isToolProvider" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.autoReindex') }}</span>
            <UIcon v-if="!autoReindexLicensed" name="heroicons-lock-closed" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </div>
          <UToggle
            :model-value="autoReindexEnabled"
            :disabled="!autoReindexLicensed || savingAutoReindex"
            size="sm"
            @update:model-value="onToggleAutoReindex"
          />
        </div>
        <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">
          {{ autoReindexLicensed ? $t('data.autoReindexHint') : $t('data.autoReindexEnterprise') }}
        </p>

        <!-- Schedule picker — either a recurring interval OR a fixed daily time.
             Only when enabled & licensed. -->
        <div v-if="autoReindexLicensed && autoReindexEnabled" class="mt-2 space-y-2">
          <!-- Mode toggle -->
          <div class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexSchedule') }}</span>
            <div class="inline-flex rounded-md border border-gray-200 dark:border-gray-800 overflow-hidden text-xs">
              <button
                type="button"
                :disabled="savingAutoReindex"
                :class="reindexMode === 'interval' ? 'bg-blue-50 text-blue-700' : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
                class="px-2 py-1 disabled:opacity-50"
                @click="setReindexMode('interval')"
              >{{ $t('data.autoReindexModeInterval') }}</button>
              <button
                type="button"
                :disabled="savingAutoReindex"
                :class="reindexMode === 'time' ? 'bg-blue-50 text-blue-700' : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
                class="px-2 py-1 border-l border-gray-200 dark:border-gray-800 disabled:opacity-50"
                @click="setReindexMode('time')"
              >{{ $t('data.autoReindexModeTime') }}</button>
            </div>
          </div>

          <!-- Interval: number + unit (1 minute minimum) -->
          <div v-if="reindexMode === 'interval'" class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexEvery') }}</span>
            <div class="flex items-center gap-1">
              <input
                type="number"
                min="1"
                v-model.number="intervalValue"
                :disabled="savingAutoReindex"
                class="w-16 text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onScheduleChange"
              />
              <select
                v-model="intervalUnit"
                :disabled="savingAutoReindex"
                class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onScheduleChange"
              >
                <option value="minutes">{{ $t('data.unitMinutes') }}</option>
                <option value="hours">{{ $t('data.unitHours') }}</option>
              </select>
            </div>
          </div>

          <!-- Fixed daily time (interpreted in the org timezone) -->
          <div v-else class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexAt') }}</span>
            <input
              type="time"
              v-model="reindexAtTime"
              :disabled="savingAutoReindex"
              class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
              @change="onScheduleChange"
            />
          </div>

          <p v-if="reindexScheduleError" class="text-[11px] text-amber-600">{{ reindexScheduleError }}</p>
          <p v-else-if="reindexMode === 'time'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('data.autoReindexTimeHint') }}</p>
        </div>

        <!-- Last background failure, if any. -->
        <p v-if="autoReindexError" class="text-[11px] text-red-500 mt-1.5 truncate" :title="autoReindexError">
          {{ $t('data.autoReindexLastError') }}: {{ autoReindexError }}
        </p>
      </div>

      <!-- Per-connection request rate limit (enterprise `connection_rate_limit`).
           Admin-only. Hard-blocks agent queries once a fixed per-window
           threshold is crossed; the budget is shared across all users. -->
      <div v-if="canUpdateDataSource && !isToolProvider" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.rateLimit') }}</span>
            <UIcon v-if="!rateLimitLicensed" name="heroicons-lock-closed" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </div>
          <UToggle
            :model-value="rateLimitEnabled"
            :disabled="!rateLimitLicensed || savingRateLimit"
            size="sm"
            @update:model-value="onToggleRateLimit"
          />
        </div>
        <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">
          {{ rateLimitLicensed ? $t('data.rateLimitHint') : $t('data.rateLimitEnterprise') }}
        </p>

        <!-- Per-window caps. Blank / 0 means "no limit" for that window. -->
        <div v-if="rateLimitLicensed && rateLimitEnabled" class="mt-2 space-y-2">
          <div
            v-for="w in rateLimitWindows"
            :key="w.key"
            class="flex items-center justify-between"
          >
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ w.label }}</span>
            <div class="flex items-center gap-1">
              <input
                type="number"
                min="0"
                v-model.number="w.model.value"
                :disabled="savingRateLimit"
                :placeholder="$t('data.rateLimitNoLimit')"
                class="w-24 text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onRateLimitChange"
              />
              <span class="text-[11px] text-gray-400 dark:text-gray-500 w-14">{{ w.unit }}</span>
            </div>
          </div>
          <p v-if="rateLimitError" class="text-[11px] text-red-500">{{ rateLimitError }}</p>
        </div>
      </div>

      <!-- Per-user summary — honest, user-scoped view for OBO viewers: what THEY
           can see, not the service-principal's "Discovered N tables" / logs. -->
      <div v-else-if="isPerUserViewer" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <div class="flex items-center gap-1.5 text-xs text-green-700">
          <UIcon name="heroicons-check-circle" class="w-4 h-4 flex-shrink-0" />
          <span>{{ isToolProvider
            ? $t('data.toolsAccessible', { n: toolCount })
            : $t('data.tablesAccessible', { n: tableCount }) }}</span>
        </div>
      </div>

      <!-- Query identity toggle (admin/owner on delegated connections) -->
      <div v-if="requiresUserAuth && canSwitchIdentity" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <div class="text-xs text-gray-500 dark:text-gray-400 mb-2">{{ $t('data.runQueriesAs') }}</div>
        <div class="grid grid-cols-2 gap-2">
          <button
            @click="setIdentity('service_account')"
            :disabled="switchingIdentity"
            :class="['inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg border disabled:opacity-60',
                     queryIdentity === 'service_account'
                       ? 'bg-blue-50 border-blue-300 text-blue-700'
                       : 'bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50']"
          >
            <UIcon name="heroicons-shield-check" class="w-3.5 h-3.5" />
            {{ $t('data.serviceAccount') }}
          </button>
          <button
            @click="setIdentity('self')"
            :disabled="switchingIdentity"
            :class="['inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg border disabled:opacity-60',
                     queryIdentity === 'self'
                       ? 'bg-blue-50 border-blue-300 text-blue-700'
                       : 'bg-white dark:bg-gray-900 border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50']"
          >
            <UIcon name="heroicons-user" class="w-3.5 h-3.5" />
            {{ $t('data.me') }}
          </button>
        </div>

        <!-- "Me" selected: connect / disconnect / reload -->
        <div v-if="queryIdentity === 'self'" class="mt-3">
          <div v-if="!hasUserCredentials">
            <button
              @click="openCredentialsModal"
              :disabled="connecting"
              class="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-60 disabled:cursor-not-allowed"
            >
              <Spinner v-if="connecting" class="w-3.5 h-3.5" />
              <UIcon v-else name="heroicons-key" class="w-3.5 h-3.5" />
              {{ $t('data.connect') }}
            </button>
            <p class="text-xs text-gray-400 dark:text-gray-500 mt-1.5 text-center">{{ $t('data.connectToQueryAsYou') }}</p>
          </div>
          <div v-else class="flex items-center gap-2">
            <button
              @click="reloadMySchema"
              :disabled="reloadingMySchema"
              class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            >
              <Spinner v-if="reloadingMySchema" class="w-3.5 h-3.5" />
              <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
              {{ reloadingMySchema ? $t('data.refreshing') : $t('data.reloadMyTables') }}
            </button>
            <button
              @click="disconnect"
              :disabled="disconnecting"
              class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-red-600 bg-white dark:bg-gray-900 border border-red-200 dark:border-red-900/50 rounded-lg hover:bg-red-50 dark:hover:bg-red-950/30 disabled:opacity-50"
            >
              <Spinner v-if="disconnecting" class="w-3.5 h-3.5" />
              <UIcon v-else name="heroicons-arrow-right-on-rectangle" class="w-3.5 h-3.5" />
              {{ disconnecting ? $t('data.disconnecting') : $t('data.disconnect') }}
            </button>
          </div>
        </div>
        <p v-else class="mt-2 text-xs text-gray-400 dark:text-gray-500">{{ $t('data.serviceAccountNote') }}</p>
      </div>

      <!-- Actions -->
      <div class="flex items-center gap-2 pt-4 border-t border-gray-100 dark:border-gray-800">
        <button
          v-if="canUpdateDataSource"
          @click="testConnection"
          :disabled="testing"
          class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
        >
          <Spinner v-if="testing" class="w-3.5 h-3.5" />
          <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
          {{ testing ? $t('data.testing') : $t('data.test') }}
        </button>
        <!-- Full Edit button (admin with update_data_source permission) -->
        <button
          v-if="canUpdateDataSource"
          @click="openEdit"
          class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50"
        >
          <UIcon name="heroicons-pencil" class="w-3.5 h-3.5" />
          {{ $t('data.edit') }}
        </button>

        <!-- Connect / Disconnect (user auth required, no admin permission) -->
        <template v-else-if="requiresUserAuth && !canSwitchIdentity">
          <!-- Per-user reload: refresh the tables THIS user can see (their
               overlay) via their own creds — the per-user counterpart to the
               admin Reindex. -->
          <button
            v-if="hasUserCredentials"
            @click="reloadMySchema"
            :disabled="reloadingMySchema"
            class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
          >
            <Spinner v-if="reloadingMySchema" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
            {{ reloadingMySchema ? $t('data.refreshing') : $t('data.reloadMyTables') }}
          </button>
          <button
            v-if="hasUserCredentials"
            @click="disconnect"
            :disabled="disconnecting"
            class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-red-600 bg-white dark:bg-gray-900 border border-red-200 dark:border-red-900/50 rounded-lg hover:bg-red-50 dark:hover:bg-red-950/30 disabled:opacity-50"
          >
            <Spinner v-if="disconnecting" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-right-on-rectangle" class="w-3.5 h-3.5" />
            {{ disconnecting ? 'Disconnecting…' : 'Disconnect' }}
          </button>
          <!-- Owner runs via the connection's system (service principal) creds. -->
          <div
            v-else-if="usesServiceAccount"
            class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg"
          >
            <UIcon name="heroicons-shield-check" class="w-3.5 h-3.5" />
            Service account
          </div>
          <button
            v-else
            @click="openCredentialsModal"
            :disabled="connecting"
            class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-60 disabled:cursor-not-allowed"
          >
            <Spinner v-if="connecting" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-key" class="w-3.5 h-3.5" />
            {{ $t('data.connect') }}
          </button>
        </template>
      </div>

      <!-- Test Result -->
      <div v-if="testResult" class="mt-3 text-xs text-center" :class="testResult.success ? 'text-green-600' : 'text-red-600'">
        {{ testResult.message }}
      </div>

      <!-- Delete Section (only for admins) -->
      <div v-if="canUpdateDataSource" class="pt-4 mt-4 border-t border-gray-100 dark:border-gray-800">
        <div v-if="!confirmingDelete">
          <button
            @click="confirmingDelete = true"
            class="w-full inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs rounded-lg transition-colors text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 cursor-pointer"
          >
            <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
            {{ $t('data.deleteConnection') }}
          </button>
        </div>

        <!-- Confirm delete -->
        <div v-else class="space-y-3">
          <!-- Warning for impacted agents -->
          <div v-if="agentCount > 0" class="p-3 bg-amber-50 border border-amber-200 rounded-lg">
            <div class="flex items-start gap-2">
              <UIcon name="heroicons-exclamation-triangle" class="w-4 h-4 text-amber-600 flex-shrink-0 mt-0.5" />
              <div class="text-xs">
                <p class="font-medium text-amber-800">{{ agentCount === 1 ? $t('data.impactAgentsOne', { count: agentCount }) : $t('data.impactAgentsMany', { count: agentCount }) }}</p>
                <p class="text-amber-700 mt-1">
                  {{ agentNames.slice(0, 3).join(', ') }}{{ agentNames.length > 3 ? ' ' + $t('data.andMore', { n: agentNames.length - 3 }) : '' }}
                </p>
                <p class="text-amber-600 mt-1">{{ $t('data.tablesRemovedNote') }}</p>
              </div>
            </div>
          </div>

          <p class="text-xs text-gray-600 dark:text-gray-400 text-center">{{ $t('data.deleteConfirm') }}</p>
          <div class="flex gap-2">
            <button
              @click="confirmingDelete = false"
              :disabled="deleting"
              class="flex-1 px-3 py-2 text-xs text-gray-600 dark:text-gray-400 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50"
            >
              {{ $t('data.cancel') }}
            </button>
            <button
              @click="deleteConnection"
              :disabled="deleting"
              class="flex-1 inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-white bg-red-600 rounded-lg hover:bg-red-700 disabled:opacity-50"
            >
              <Spinner v-if="deleting" class="w-3.5 h-3.5" />
              {{ deleting ? $t('data.deleting') : $t('data.delete') }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </UModal>

  <!-- Edit Connection Modal -->
  <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-xl' }">
    <UCard>
      <template #header>
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <DataSourceIcon :type="connection?.type" :connector-key="connection?.connector_key" class="h-5" />
            <h3 class="text-lg font-semibold">{{ $t('data.editConnection') }}</h3>
          </div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" @click="showEditModal = false" />
        </div>
      </template>

      <div v-if="loadingDetails" class="py-8 text-center">
        <Spinner class="h-5 w-5 mx-auto text-gray-400 dark:text-gray-500" />
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">{{ $t('common.loading') }}</p>
      </div>

      <ConnectForm
        v-else-if="editFormValues"
        mode="edit"
        :initialType="connection?.type"
        :connectionId="connection?.id"
        :initialValues="editFormValues"
        :forceShowSystemCredentials="true"
        :showRequireUserAuthToggle="true"
        :showTestButton="true"
        :showLLMToggle="false"
        :allowNameEdit="true"
        :hideHeader="true"
        @success="handleEditSuccess"
      />
    </UCard>
  </UModal>

  <!-- MCP Edit Modal -->
  <AddMCPModal
    v-model="showMcpEditModal"
    :editConnection="connection"
    @created="handleEditSuccess"
  />

  <!-- User Credentials Modal (for users without update permission but require auth) -->
  <!-- The modal derives the connection id from a data-source-shaped object
       (.connections[0].id), so wrap the connection to satisfy that contract. -->
  <UserDataSourceCredentialsModal
    v-model="showCredentialsModal"
    :dataSource="connectionAsDataSource"
    @saved="handleCredentialsSaved"
  />
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ConnectForm from '~/components/datasources/ConnectForm.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import ConnectionIndexingProgress from '~/components/ConnectionIndexingProgress.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import { useCan } from '~/composables/usePermissions'
import { isIndexingActive, type ConnectionIndexing } from '~/composables/useConnectionStatus'
import { useEnterprise } from '~/ee/composables/useEnterprise'

const props = defineProps<{
  modelValue: boolean
  connection: any
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'updated'): void
}>()

const { t } = useI18n()
const toast = useToast()
const signIn = useConnectionSignIn()
// True while awaiting the OAuth authorize redirect; spins the Connect button.
const connecting = ref(false)

const isOpen = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const testing = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const showEditModal = ref(false)
const showMcpEditModal = ref(false)
const loadingDetails = ref(false)
const connectionDetails = ref<any>(null)
const showCredentialsModal = ref(false)
const confirmingDelete = ref(false)
const deleting = ref(false)
const indexingState = ref<ConnectionIndexing | null>(null)
const reindexing = ref(false)
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL_MS = 2000

// Permission and auth checks
const canUpdateDataSource = computed(() => useCan('update_data_source'))
const requiresUserAuth = computed(() => props.connection?.auth_policy === 'user_required')
// Locally-overridable user status: the query-identity PATCH returns a fresh status
// which we apply immediately, so the modal reflects the switch without waiting on
// (or depending on) the parent re-passing the connection prop.
const statusOverride = ref<any>(null)
// Optimistic identity selection — highlights the chosen button the instant it's
// clicked, before the request returns; cleared once the authoritative status lands.
const pendingIdentity = ref<'self' | 'service_account' | null>(null)
const userStatus = computed(() => statusOverride.value || props.connection?.user_status || null)
const hasUserCredentials = computed(() => !!userStatus.value?.has_user_credentials)
// Owner/admin runs via the connection's system (service principal) creds.
const usesServiceAccount = computed(() => userStatus.value?.effective_auth === 'system')
// Non-admin viewer running on their own per-user (OBO) creds: show a user-scoped
// summary instead of the shared service-principal indexing run + logs.
const isPerUserViewer = computed(() =>
  requiresUserAuth.value && hasUserCredentials.value && !canUpdateDataSource.value
)
// Admin/owner query-identity toggle (delegated/OBO connections).
const canSwitchIdentity = computed(() => !!userStatus.value?.can_switch_identity)
const queryIdentity = computed<'self' | 'service_account'>(() =>
  (pendingIdentity.value
    ?? (userStatus.value?.query_identity === 'service_account' ? 'service_account' : 'self'))
)
const switchingIdentity = ref(false)
async function setIdentity(identity: 'self' | 'service_account') {
  if (!props.connection?.id || switchingIdentity.value) return
  if (queryIdentity.value === identity) return
  pendingIdentity.value = identity // optimistic highlight
  switchingIdentity.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/query-identity`, {
      method: 'PATCH',
      body: { query_identity: identity },
    })
    if (error.value) {
      pendingIdentity.value = null
      toast.add({
        title: t('data.switchIdentityFailed'),
        description: (error.value as any)?.data?.detail || (error.value as any)?.message,
        color: 'red',
      })
    } else {
      // Apply the authoritative status returned by the endpoint, then let the
      // parent refresh table counts / overlays in the background.
      if (data.value) statusOverride.value = data.value
      pendingIdentity.value = null
      emit('updated')
    }
  } catch (e: any) {
    pendingIdentity.value = null
    toast.add({ title: t('data.switchIdentityFailed'), description: e?.message, color: 'red' })
  } finally {
    switchingIdentity.value = false
  }
}
const disconnecting = ref(false)
// The credentials modal expects a data-source-shaped object whose
// `connections[0].id` is the connection to authorize. We only have the
// connection here, so wrap it.
const connectionAsDataSource = computed(() =>
  props.connection ? { ...props.connection, connections: [props.connection] } : null
)

const _TOOL_PROVIDER_TYPES = [
  'mcp', 'custom_api', 'onedrive', 'google_drive', 'outlook_mail', 'gmail_mail',
]
const isToolProvider = computed(() => _TOOL_PROVIDER_TYPES.includes(props.connection?.type))
const toolCount = computed(() => props.connection?.tool_count || 0)

const isConnected = computed(() => {
  // Check multiple possible status fields
  const conn = props.connection
  if (!conn) return false
  
  // Direct status fields
  if (conn.last_status === 'success' || conn.status === 'success') return true
  if (conn.last_status === 'error' || conn.status === 'error') return false
  
  // User status
  const userStatus = conn.user_status?.connection
  if (userStatus === 'success') return true
  if (userStatus === 'error' || userStatus === 'offline') return false
  
  // Default to true if connection exists (assume healthy)
  return true
})

// Prefer a freshly-reloaded per-user count (set by reloadMySchema) over the
// value carried on the connection prop, so the count updates without waiting
// for the parent to refetch the connections list.
const myTableCountOverride = ref<number | null>(null)
const tableCount = computed(() => myTableCountOverride.value ?? (props.connection?.table_count || 0))
const agentCount = computed(() => props.connection?.agent_count || 0)
const agentNames = computed(() => props.connection?.agent_names || [])

const lastCheckedDisplay = computed(() => {
  const lastChecked = props.connection?.last_checked_at || props.connection?.user_status?.last_checked_at
  if (!lastChecked) return null
  const seconds = Math.floor((Date.now() - new Date(lastChecked).getTime()) / 1000)
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
})

const lastIndexedDisplay = computed(() => {
  const ts = indexingState.value?.finished_at
  if (!ts) return ''
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
})

// When THIS user last pulled their accessible tables. Prefer a fresh local
// timestamp set right after a per-user reload; otherwise the last successful
// use of their creds from the connection payload.
const myRefreshedAt = ref<string | null>(null)
const myLastRefreshedDisplay = computed(() => {
  const ts = myRefreshedAt.value || props.connection?.user_status?.last_used_at
  if (!ts) return ''
  const seconds = Math.floor((Date.now() - new Date(ts).getTime()) / 1000)
  if (seconds < 60) return t('data.justNow')
  if (seconds < 3600) return t('data.minutesAgo', { n: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('data.hoursAgo', { n: Math.floor(seconds / 3600) })
  return t('data.daysAgo', { n: Math.floor(seconds / 86400) })
})

async function fetchIndexing() {
  if (!props.connection?.id) return
  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}/indexing`, { method: 'GET' })
    if ((data as any).value) {
      indexingState.value = (data as any).value as ConnectionIndexing
    }
  } catch {
    // 404 = no indexing run ever; transient errors handled silently.
  }
}

function startPollingIfActive() {
  stopPolling()
  if (!isIndexingActive(indexingState.value)) return
  pollTimer = setInterval(() => {
    if (!isOpen.value || !isIndexingActive(indexingState.value)) {
      stopPolling()
      return
    }
    fetchIndexing()
  }, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

// ── Auto-reindex schedule (enterprise `scheduled_reindex`) ──────────────────
const { hasFeature } = useEnterprise()
const autoReindexLicensed = computed(() => hasFeature('scheduled_reindex'))
const autoReindexEnabled = ref(true)
const autoReindexError = ref<string | null>(null)
const savingAutoReindex = ref(false)

// Schedule: either a recurring interval (value + unit) OR a fixed daily time.
const MIN_INTERVAL_MINUTES = 1
const reindexMode = ref<'interval' | 'time'>('interval')
const intervalValue = ref<number>(12)
const intervalUnit = ref<'minutes' | 'hours'>('hours')
const reindexAtTime = ref<string>('02:00')
const reindexScheduleError = ref<string | null>(null)

// Resolve the interval inputs to minutes, enforcing the 10-minute floor.
function resolvedIntervalMinutes(): number {
  const raw = Number(intervalValue.value) || 0
  const mins = intervalUnit.value === 'hours' ? raw * 60 : raw
  return Math.max(MIN_INTERVAL_MINUTES, Math.round(mins))
}

async function fetchAutoReindexConfig() {
  // Only admins can read connection detail (config-bearing). The list payload
  // doesn't carry the schedule fields, so fetch them here.
  if (!props.connection?.id || !canUpdateDataSource.value || isToolProvider.value) return
  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'GET' })
    const d = (data as any).value
    if (d) {
      autoReindexEnabled.value = d.auto_reindex_enabled !== false
      autoReindexError.value = d.last_reindex_error || null
      reindexMode.value = d.reindex_schedule_mode === 'time' ? 'time' : 'interval'
      reindexAtTime.value = d.reindex_at_time || '02:00'
      // Prefer the minutes column; fall back to the legacy hours field. Present
      // whole-hour intervals in hours, otherwise minutes.
      const mins = d.reindex_interval_minutes
        ?? (d.reindex_interval_hours ? d.reindex_interval_hours * 60 : null)
        ?? (12 * 60)
      if (mins % 60 === 0) {
        intervalUnit.value = 'hours'
        intervalValue.value = mins / 60
      } else {
        intervalUnit.value = 'minutes'
        intervalValue.value = mins
      }
    }
  } catch {
    // Non-fatal — section just shows defaults.
  }
}

async function saveAutoReindex() {
  if (!props.connection?.id || savingAutoReindex.value) return
  savingAutoReindex.value = true
  try {
    const body: Record<string, any> = {
      auto_reindex_enabled: autoReindexEnabled.value,
      reindex_schedule_mode: reindexMode.value,
    }
    if (reindexMode.value === 'time') {
      body.reindex_at_time = reindexAtTime.value
    } else {
      body.reindex_interval_minutes = resolvedIntervalMinutes()
    }
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, {
      method: 'PUT',
      body,
    })
    if (error.value) {
      toast.add({
        title: t('data.autoReindexSaveFailed'),
        description: (error.value as any)?.data?.detail || (error.value as any)?.message,
        color: 'red',
      })
    }
  } finally {
    savingAutoReindex.value = false
  }
}

function onToggleAutoReindex(val: boolean) {
  autoReindexEnabled.value = val
  saveAutoReindex()
}

function setReindexMode(mode: 'interval' | 'time') {
  if (reindexMode.value === mode) return
  reindexMode.value = mode
  onScheduleChange()
}

function onScheduleChange() {
  reindexScheduleError.value = null
  if (reindexMode.value === 'interval') {
    const mins = resolvedIntervalMinutes()
    // Reflect the enforced floor back into the inputs so the UI is honest.
    if (mins === MIN_INTERVAL_MINUTES && resolvedRawMinutes() < MIN_INTERVAL_MINUTES) {
      reindexScheduleError.value = t('data.autoReindexMinInterval', { n: MIN_INTERVAL_MINUTES })
      intervalUnit.value = 'minutes'
      intervalValue.value = MIN_INTERVAL_MINUTES
    }
  } else if (!reindexAtTime.value) {
    reindexAtTime.value = '02:00'
  }
  saveAutoReindex()
}

function resolvedRawMinutes(): number {
  const raw = Number(intervalValue.value) || 0
  return intervalUnit.value === 'hours' ? raw * 60 : raw
}

// ── Per-connection request rate limit (enterprise `connection_rate_limit`) ───
const rateLimitLicensed = computed(() => hasFeature('connection_rate_limit'))
const rateLimitEnabled = ref(false)
const rateLimitPerMinute = ref<number | null>(null)
const rateLimitPerHour = ref<number | null>(null)
const rateLimitPerDay = ref<number | null>(null)
const rateLimitError = ref<string | null>(null)
const savingRateLimit = ref(false)

// Rendered rows; each binds to one window ref.
const rateLimitWindows = computed(() => [
  { key: 'minute', label: t('data.rateLimitPerMinute'), unit: t('data.rateLimitReqMin'), model: rateLimitPerMinute },
  { key: 'hour', label: t('data.rateLimitPerHour'), unit: t('data.rateLimitReqHour'), model: rateLimitPerHour },
  { key: 'day', label: t('data.rateLimitPerDay'), unit: t('data.rateLimitReqDay'), model: rateLimitPerDay },
])

// Normalize an input value to a non-negative int or null (blank / 0 = no limit).
function normalizeRateLimit(v: number | null): number | null {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.floor(n)
}

async function fetchRateLimitConfig() {
  if (!props.connection?.id || !canUpdateDataSource.value || isToolProvider.value) return
  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'GET' })
    const d = (data as any).value
    if (d) {
      rateLimitEnabled.value = d.rate_limit_enabled === true
      rateLimitPerMinute.value = d.rate_limit_per_minute ?? null
      rateLimitPerHour.value = d.rate_limit_per_hour ?? null
      rateLimitPerDay.value = d.rate_limit_per_day ?? null
    }
  } catch {
    // Non-fatal — section just shows defaults.
  }
}

async function saveRateLimit() {
  if (!props.connection?.id || savingRateLimit.value) return
  savingRateLimit.value = true
  rateLimitError.value = null
  try {
    const body: Record<string, any> = {
      rate_limit_enabled: rateLimitEnabled.value,
      // Send 0 for "no limit" so a cleared field persists (the API treats
      // 0/null identically as unlimited).
      rate_limit_per_minute: normalizeRateLimit(rateLimitPerMinute.value) ?? 0,
      rate_limit_per_hour: normalizeRateLimit(rateLimitPerHour.value) ?? 0,
      rate_limit_per_day: normalizeRateLimit(rateLimitPerDay.value) ?? 0,
    }
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, {
      method: 'PUT',
      body,
    })
    if (error.value) {
      rateLimitError.value = (error.value as any)?.data?.detail || (error.value as any)?.message || t('data.rateLimitSaveFailed')
      toast.add({
        title: t('data.rateLimitSaveFailed'),
        description: rateLimitError.value,
        color: 'red',
      })
    }
  } finally {
    savingRateLimit.value = false
  }
}

function onToggleRateLimit(val: boolean) {
  rateLimitEnabled.value = val
  saveRateLimit()
}

function onRateLimitChange() {
  rateLimitError.value = null
  // Reflect the normalization back into the inputs so the UI is honest.
  rateLimitPerMinute.value = normalizeRateLimit(rateLimitPerMinute.value)
  rateLimitPerHour.value = normalizeRateLimit(rateLimitPerHour.value)
  rateLimitPerDay.value = normalizeRateLimit(rateLimitPerDay.value)
  saveRateLimit()
}

async function reindex() {
  if (!props.connection?.id || reindexing.value) return
  reindexing.value = true
  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}/reindex?force=true`, { method: 'POST' })
    const result = (data as any).value
    if (result?.indexing) {
      indexingState.value = result.indexing as ConnectionIndexing
    }
    startPollingIfActive()
  } finally {
    reindexing.value = false
  }
}

const cancelling = ref(false)
async function cancelIndexing() {
  if (!props.connection?.id || cancelling.value) return
  cancelling.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/indexing/cancel`, { method: 'POST' })
    if (error.value) {
      toast.add({ title: t('data.stopIndexingFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
    } else {
      const result = (data as any).value
      if (result?.indexing) indexingState.value = result.indexing as ConnectionIndexing
      // Keep polling briefly — the runner finalizes the row asynchronously.
      fetchIndexing()
    }
  } catch (e: any) {
    toast.add({ title: t('data.stopIndexingFailed'), description: e?.message, color: 'red' })
  } finally {
    cancelling.value = false
  }
}

function formatIndexBytes(n?: number | null): string {
  if (!n || n <= 0) return ''
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = n
  let i = 0
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++ }
  return `${i === 0 ? Math.round(size) : size.toFixed(1)} ${units[i]}`
}

function formatIndexDuration(seconds?: number | null): string {
  if (seconds == null) return ''
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

const editFormValues = computed(() => {
  if (!connectionDetails.value) return null
  return {
    name: connectionDetails.value.name,
    config: connectionDetails.value.config || {},
    auth_policy: connectionDetails.value.auth_policy,
    has_credentials: connectionDetails.value.has_credentials,
    credentials: {}
  }
})

async function testConnection() {
  if (!props.connection?.id || testing.value) return
  testing.value = true
  testResult.value = null
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/test`, { method: 'POST' })
    if (error.value) {
      testResult.value = { success: false, message: error.value.message || t('data.testFailed') }
    } else {
      const result = data.value as any
      testResult.value = {
        success: result.success,
        message: result.success ? t('data.connectionSuccessful') : (result.message || t('data.connectionFailed'))
      }
    }
    emit('updated')
  } catch (e: any) {
    testResult.value = { success: false, message: e.message || t('data.testFailed') }
  } finally {
    testing.value = false
  }
}

async function openEdit() {
  isOpen.value = false
  await nextTick()

  if (isToolProvider.value) {
    showMcpEditModal.value = true
    return
  }

  loadingDetails.value = true
  showEditModal.value = true

  try {
    const { data } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'GET' })
    if (data.value) {
      connectionDetails.value = data.value
    }
  } finally {
    loadingDetails.value = false
  }
}

function handleEditSuccess() {
  showEditModal.value = false
  connectionDetails.value = null
  emit('updated')
}

async function openCredentialsModal() {
  // OAuth-only (Entra/OBO) connections have nothing to type or pick — redirect
  // straight to the provider instead of opening an empty credentials modal,
  // collapsing the old Connect → Sign in two-click flow into one. The button
  // keeps spinning through the slow authorize round-trip until the browser
  // navigates away. Anything else (multiple auth modes, no oauth) falls back
  // to the modal as before.
  connecting.value = true
  const result = await signIn.triggerUserSignIn(props.connection)
  if (result.redirecting) return // keep spinning; the page is navigating to the provider
  connecting.value = false
  if (result.error) {
    toast.add({ title: t('data.oauthStartFailed'), description: result.error, color: 'red' })
  }
  isOpen.value = false
  showCredentialsModal.value = true
}

const reloadingMySchema = ref(false)
async function reloadMySchema() {
  // Per-user reindex: re-fetch THIS user's accessible tables (their overlay)
  // via their own creds — the per-user counterpart to the admin /reindex.
  if (!props.connection?.id || reloadingMySchema.value) return
  reloadingMySchema.value = true
  try {
    const { data, error } = await useMyFetch(`/connections/${props.connection.id}/my-schema/refresh`, { method: 'POST' })
    if (!error.value) {
      const result = data.value as any
      if (result?.table_count != null) myTableCountOverride.value = result.table_count
      myRefreshedAt.value = new Date().toISOString()
      // Intentionally NOT emitting 'updated': the reload only changes this
      // user's overlay/count, which we already reflect locally above. Emitting
      // would trigger the parent's full refreshData (incl. the admin-only demos
      // fetch), producing a spurious access.denied for non-admins.
    }
  } finally {
    reloadingMySchema.value = false
  }
}

async function disconnect() {
  // Per-user creds are CONNECTION-level — clear them via the connection endpoint.
  if (!props.connection?.id || disconnecting.value) return
  disconnecting.value = true
  try {
    await useMyFetch(`/connections/${props.connection.id}/my-credentials`, { method: 'DELETE' })
    emit('updated')
    isOpen.value = false
  } finally {
    disconnecting.value = false
  }
}

function handleCredentialsSaved() {
  emit('updated')
}

async function deleteConnection() {
  if (!props.connection?.id || deleting.value) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, { method: 'DELETE' })
    if (error.value) {
      testResult.value = { success: false, message: error.value.message || t('data.deleteFailed') }
      confirmingDelete.value = false
    } else {
      isOpen.value = false
      emit('updated')
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e.message || t('data.deleteFailed') }
    confirmingDelete.value = false
  } finally {
    deleting.value = false
  }
}

// Reset state when modal closes
watch(isOpen, (val) => {
  if (!val) {
    testResult.value = null
    confirmingDelete.value = false
    connecting.value = false
    stopPolling()
    return
  }
  // Modal opened — seed indexing state from props, fetch fresh, then poll
  // if active.
  myTableCountOverride.value = null
  myRefreshedAt.value = null
  statusOverride.value = null
  pendingIdentity.value = null
  indexingState.value = (props.connection?.indexing as ConnectionIndexing) || null
  fetchIndexing().then(() => startPollingIfActive())
  fetchAutoReindexConfig()
  fetchRateLimitConfig()
})

// If the parent swaps the connection prop while the modal is open, refresh.
watch(() => props.connection?.id, () => {
  if (!isOpen.value) return
  statusOverride.value = null
  pendingIdentity.value = null
  indexingState.value = (props.connection?.indexing as ConnectionIndexing) || null
  fetchIndexing().then(() => startPollingIfActive())
  fetchRateLimitConfig()
})

onBeforeUnmount(() => stopPolling())
</script>
