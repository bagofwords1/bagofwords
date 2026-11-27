<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }" prevent-close>
    <div class="p-5" data-testid="custom-query-modal">
      <!-- Header -->
      <div class="flex items-start justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold dark:text-white">
            {{ editing ? 'Edit custom table' : 'New custom table' }}
          </h2>
          <i18n-t scope="global" v-if="perUser" keypath="customTableCopies.subtitle" tag="p"
                  class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            <template #connection><span class="font-medium">{{ activeConnectionName }}</span></template>
          </i18n-t>
          <p v-else class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Runs on a schedule and is stored locally, so agents answer from a
            cached copy instead of querying
            <span class="font-medium">{{ activeConnectionName }}</span> every time.
          </p>
        </div>
        <button class="text-ink-muted hover:text-ink-2" @click="close">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <!-- Connection-wide scope warning: this is created from an agent page but
           the object belongs to the connection. -->
      <div class="mb-3 flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 px-3 py-2">
        <UIcon name="heroicons-information-circle" class="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
        <p class="text-xs text-amber-800 dark:text-amber-200">
          This is created on the connection <b>{{ activeConnectionName }}</b> and can be
          activated by any agent that uses it — it is not limited to this agent.
        </p>
      </div>

      <!-- Tabs -->
      <div class="flex items-center gap-1 border-b border-gray-200 dark:border-gray-800 mb-4">
        <button
          v-for="t in tabs" :key="t.key"
          :data-testid="`cq-tab-${t.key}`"
          class="px-3 py-1.5 text-sm -mb-px border-b-2 transition-colors"
          :class="tab === t.key
            ? 'border-blue-600 text-blue-600 dark:text-blue-400 font-medium'
            : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400'"
          :disabled="t.disabled"
          @click="!t.disabled && (tab = t.key)"
        >
          {{ t.label }}
          <!-- A disabled tab has to say why it is disabled. A blanket "soon"
               told people the feature did not exist yet, when the real reason
               is that a row policy filters on the query's result columns and
               those are only known once it has been saved. -->
          <span v-if="t.disabled && t.hint"
                class="ms-1 text-2xs px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-ink-muted">{{ t.hint }}</span>
        </button>
      </div>

      <!-- ============ QUERY ============ -->
      <div v-show="tab === 'query'">
        <!-- Each person signs in to this connection themselves, so there is no
             shared copy: say so before the admin writes a query they expect
             everyone to see the same rows of. -->
        <div v-if="perUser" data-testid="cq-per-user-notice"
             class="mb-3 flex items-start gap-2 rounded-md bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 px-3 py-2">
          <UIcon name="heroicons-user-circle" class="w-4 h-4 text-blue-600 dark:text-blue-400 mt-0.5 flex-shrink-0" />
          <div class="text-xs text-blue-900 dark:text-blue-200">
            <p class="font-medium">{{ t('customTableCopies.noticeTitle') }}</p>
            <p class="mt-0.5">{{ t('customTableCopies.notice', { connection: activeConnectionName }) }}</p>
          </div>
        </div>
        <!-- Connection picker. An agent can have several; the query runs against
             exactly one, and the query below is written in that source's dialect
             (SQL, or DAX on Power BI). -->
        <div class="mb-3">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Connection</label>
          <div v-if="editing" class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <DataSourceIcon :type="activeConnectionType" class="h-3.5" />
            <span class="font-medium">{{ activeConnectionName }}</span>
            <span class="text-2xs text-ink-muted">
              — fixed after creation; create a new query to use a different connection
            </span>
          </div>
          <USelectMenu
            v-else
            v-model="selectedConnectionId"
            :options="connectionOptions"
            value-attribute="id"
            option-attribute="name"
            data-testid="cq-connection"
          >
            <template #label>
              <span class="flex items-center gap-2 min-w-0">
                <DataSourceIcon :type="activeConnectionType" class="h-3.5 flex-shrink-0" />
                <span class="truncate">{{ activeConnectionName }}</span>
                <span v-if="activeConnectionType" class="text-2xs text-ink-muted">{{ activeConnectionType }}</span>
              </span>
            </template>
            <template #option="{ option }">
              <span class="flex items-center gap-2 min-w-0">
                <DataSourceIcon :type="option.type" class="h-3.5 flex-shrink-0" />
                <span class="truncate">{{ option.name }}</span>
                <span class="text-2xs text-ink-muted">{{ option.type }}</span>
              </span>
            </template>
          </USelectMenu>
          <p v-if="!editing && availableConnections.length > 1" class="text-2xs text-ink-muted mt-1">
            Switching connections clears the preview — the query is dialect- and schema-specific.
          </p>
        </div>

        <!-- Power BI is addressed per semantic model, and a DAX text only names
             tables. Detection from those names is the default; the picker is
             for the case where the same table name lives in two models, or the
             catalog has not been indexed yet. -->
        <div v-if="isDax" class="mb-3" data-testid="cq-target-row">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Semantic model</label>
          <select
            v-model="targetDatasetId" data-testid="cq-target"
            class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white"
          >
            <option value="">Detect from the tables the DAX references</option>
            <option v-for="t in targets" :key="t.datasetId" :value="t.datasetId">
              {{ t.datasetName }}{{ t.workspaceName ? ` (${t.workspaceName})` : '' }} — {{ t.tables.length }} table{{ t.tables.length === 1 ? '' : 's' }}
            </option>
          </select>
          <p class="text-2xs text-ink-muted mt-1">
            <template v-if="currentTarget">
              Tables in this model: {{ currentTarget.tables.slice(0, 12).join(', ') }}{{ currentTarget.tables.length > 12 ? ', …' : '' }}
            </template>
            <template v-else-if="targets.length">
              Table names in the DAX must match the model's; quote names with spaces, e.g. 'Order Details'.
            </template>
            <template v-else>
              No semantic models in the catalog yet — index the connection first, or the DAX cannot be routed to a model.
            </template>
          </p>
        </div>

        <div class="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Name</label>
            <input
              v-model="form.name" data-testid="cq-name"
              placeholder="revenue_summary"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white font-mono"
            />
            <p class="text-2xs text-ink-muted mt-1">Agents query this as a table name.</p>
          </div>
          <div class="col-span-2">
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description <span class="text-ink-muted">(optional)</span></label>
            <textarea
              v-model="form.description" data-testid="cq-description" rows="1" v-text-dir
              placeholder="What this data represents — shown to the agent"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white resize-y min-h-[2.125rem]"
              :class="{ 'h-[4.5rem]': descriptionIsGenerated }"
            />
            <p v-if="descriptionIsGenerated" data-testid="cq-description-generated" class="text-2xs text-ink-muted mt-1">
              {{ t('cachedColumns.generatedDescription') }}
            </p>
          </div>
        </div>

        <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
          <template v-if="isDax">DAX <span class="text-ink-muted">— one EVALUATE against a Power BI semantic model</span></template>
          <template v-else-if="queryLanguage === 'soql'">SOQL <span class="text-ink-muted">— one Salesforce query</span></template>
          <template v-else-if="queryLanguage === 'sql'">SQL <span class="text-ink-muted">— in {{ activeConnectionType }} dialect</span></template>
          <template v-else>Query <span class="text-ink-muted">— in this connection's own format, as its agent would send it</span></template>
        </label>
        <SqlEditor
          v-model="form.definition_sql"
          :language="editorLanguage"
          :connection-id="activeConnection?.id"
          :min-height="120"
          :max-height="360"
          testid="cq-sql"
          @run="runPreview"
        />

        <div class="flex items-center gap-2 mt-2">
          <button
            data-testid="cq-run-preview"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :disabled="previewing || !form.definition_sql.trim()"
            @click="runPreview"
          >
            <Spinner v-if="previewing" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-play" class="w-3.5 h-3.5" />
            Run preview
          </button>
          <span class="text-2xs text-ink-muted">Preview is limited to {{ ROW_LIMIT }} rows.</span>
        </div>

        <!-- preview error -->
        <div v-if="previewError" data-testid="cq-preview-error"
             class="mt-3 text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-3 py-2 font-mono whitespace-pre-wrap">
          {{ previewError }}
        </div>

        <!-- budget refusal -->
        <div v-if="preview?.budget_error" data-testid="cq-budget-error"
             class="mt-3 text-xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-3 py-2">
          <b>Too large to cache.</b> {{ preview.budget_error }}
        </div>

        <!-- preview result -->
        <div v-if="preview && preview.columns.length" class="mt-3">
          <div class="flex items-center flex-wrap gap-x-4 gap-y-1 text-2xs text-gray-600 dark:text-gray-400 mb-1.5">
            <!-- A saved per-user table holds no rows of its own, so until a
                 preview is run there is no count to show. -->
            <span v-if="!(perUser && preview.saved)" data-testid="cq-preview-count">
              Showing <b>{{ preview.rows.length }}</b> of
              <b>{{ preview.estimated_rows != null ? approx(preview.estimated_rows) : 'unknown' }}</b> rows
            </span>
            <span>{{ preview.columns.length }} columns</span>
            <span v-if="preview.estimated_bytes">Est. size {{ humanBytes(preview.estimated_bytes) }}</span>
            <!-- On Snowflake and BigQuery this is the number that matters:
                 they bill for the scan, and the schedule repeats it. Showing
                 it next to the interval is what makes the refresh interval a
                 visible cost decision rather than a freshness preference. -->
            <span v-if="preview.scan_bytes" data-testid="cq-scan-cost"
                  class="text-amber-700 dark:text-amber-400">
              Scans {{ humanBytes(preview.scan_bytes) }} at the source per refresh
            </span>
          </div>
          <div class="border border-gray-200 dark:border-gray-800 rounded-md overflow-auto" style="max-height: 260px">
            <table class="min-w-full text-2xs">
              <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0">
                <tr>
                  <th v-for="c in preview.columns" :key="c.name"
                      class="text-start font-medium text-gray-600 dark:text-gray-300 px-2 py-1.5 whitespace-nowrap border-b border-gray-200 dark:border-gray-800">
                    {{ c.name }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in preview.rows" :key="i" class="odd:bg-white even:bg-gray-50/50 dark:odd:bg-gray-950 dark:even:bg-gray-900/40">
                  <td v-for="(cell, j) in row" :key="j" class="px-2 py-1 whitespace-nowrap text-gray-700 dark:text-gray-300 font-mono">
                    {{ cell === null ? '—' : cell }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- The source table carries the indexer's row-level security
               marker. The copy holds every row the connection can read and
               the source's roles are NOT carried over, so say so and point at
               the tab where the copy's own policy is set. -->
          <div v-if="rlsSources.length && !cq?.rls_enabled && !perUser" data-testid="cq-rls-source-warning"
               class="mt-3 flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 px-3 py-2">
            <UIcon name="heroicons-shield-exclamation" class="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
            <div class="text-xs text-amber-800 dark:text-amber-200">
              <p class="font-medium">{{ t('cachedColumns.rlsTitle') }}</p>
              <p class="mt-0.5">{{ t('cachedColumns.rlsBody', { tables: rlsSources.join(', ') }) }}</p>
              <button v-if="editing" type="button" data-testid="cq-rls-source-open"
                      class="mt-1 font-medium underline" @click="tab = 'rls'">{{ t('cachedColumns.rlsOpen') }}</button>
              <p v-else class="mt-1 text-amber-700 dark:text-amber-300">{{ t('cachedColumns.rlsSaveFirst') }}</p>
            </div>
          </div>

          <CachedColumnsEditor
            v-if="describedColumns.length"
            v-model:edits="columnEdits"
            :columns="describedColumns"
            :reviewed="reviewedDrift"
            @review="(name: string) => reviewedDrift = new Set([...reviewedDrift, name])"
          />
        </div>
      </div>

      <!-- ============ CACHE ============ -->
      <!-- Scrolls inside the modal: with the refresh options open this tab is
           taller than a laptop screen, and the footer must stay reachable. -->
      <div v-show="tab === 'settings'" class="max-h-[62vh] overflow-y-auto pe-1" data-testid="cq-settings">
        <!-- A per-user table has no clock-driven refresh: the same two fields
             say how old a person's copy may get before it is rebuilt at their
             next question. -->
        <template v-if="perUser">
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-3" data-testid="cq-age-intro">
            {{ t('customTableCopies.settingsIntro') }}
          </p>
          <label class="flex items-center flex-wrap gap-2 mb-4 text-sm dark:text-gray-200">
            <input type="radio" value="interval" v-model="form.refresh_schedule_mode" data-testid="cq-mode-interval" />
            <i18n-t scope="global" keypath="customTableCopies.ruleInterval" tag="span" class="flex items-center flex-wrap gap-2">
              <template #age>
                <select
                  v-model.number="form.refresh_interval_minutes" data-testid="cq-interval"
                  :disabled="form.refresh_schedule_mode !== 'interval'"
                  class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
                >
                  <option v-for="o in copyAgeOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
              </template>
            </i18n-t>
          </label>
          <label class="flex items-center flex-wrap gap-2 text-sm dark:text-gray-200">
            <input type="radio" value="time" v-model="form.refresh_schedule_mode" data-testid="cq-mode-time" />
            <i18n-t scope="global" keypath="customTableCopies.ruleTime" tag="span" class="flex items-center flex-wrap gap-2">
              <template #time>
                <input
                  type="time" v-model="form.refresh_at_time" data-testid="cq-at-time"
                  :disabled="form.refresh_schedule_mode !== 'time'"
                  class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
                />
              </template>
            </i18n-t>
            <span class="text-2xs text-ink-muted">{{ localTimezoneLabel }}</span>
          </label>
        </template>
        <template v-else>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          How often BOW re-runs this query against the source and refreshes the
          local copy. Between refreshes, agents read the cached data.
        </p>
        <div class="flex items-center gap-4 mb-4">
          <label class="flex items-center gap-2 text-sm dark:text-gray-200">
            <input type="radio" value="interval" v-model="form.refresh_schedule_mode" data-testid="cq-mode-interval" />
            Every
          </label>
          <select
            v-model.number="form.refresh_interval_minutes" data-testid="cq-interval"
            :disabled="form.refresh_schedule_mode !== 'interval'"
            class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
          >
            <option :value="15">15 minutes</option>
            <option :value="30">30 minutes</option>
            <option :value="60">hour</option>
            <option :value="360">6 hours</option>
            <option :value="720">12 hours</option>
            <option :value="1440">24 hours</option>
          </select>
        </div>
        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2 text-sm dark:text-gray-200">
            <input type="radio" value="time" v-model="form.refresh_schedule_mode" data-testid="cq-mode-time" />
            Daily at
          </label>
          <input
            type="time" v-model="form.refresh_at_time" data-testid="cq-at-time"
            :disabled="form.refresh_schedule_mode !== 'time'"
            class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white disabled:opacity-40"
          />
          <span class="text-2xs text-ink-muted">{{ localTimezoneLabel }}</span>
        </div>

        <p v-if="preview?.scan_bytes" data-testid="cq-scan-projection"
           class="mt-3 text-2xs text-amber-700 dark:text-amber-400">
          At this schedule this query reads
          <b>{{ humanBytes(preview.scan_bytes * refreshesPerDay) }}</b> from the
          source per day ({{ humanBytes(preview.scan_bytes) }} × {{ refreshesPerDay }}).
          Refreshing more often than the data is queried costs more than it saves.
        </p>

        <!-- What each refresh fetches. Full is the default and what every
             table created before this option does. -->
        <div class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800" data-testid="cq-refresh-mode">
          <div class="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">What each refresh fetches</div>
          <label class="flex items-start gap-2 mb-2 cursor-pointer">
            <input type="radio" :value="false" v-model="incr.enabled" data-testid="cq-mode-full" class="mt-0.5" />
            <span>
              <span class="text-sm dark:text-gray-200">Everything, every time</span>
              <span class="block text-2xs text-gray-500 dark:text-gray-400">Re-runs the whole query. Simple, and right for small or aggregated results.</span>
            </span>
          </label>
          <label class="flex items-start gap-2 cursor-pointer">
            <input type="radio" :value="true" v-model="incr.enabled" data-testid="cq-mode-incremental" class="mt-0.5" />
            <span>
              <span class="text-sm dark:text-gray-200">Only what is new</span>
              <span class="block text-2xs text-gray-500 dark:text-gray-400">
                The first load runs in chunks; after that each refresh reads only rows added or changed
                since the last one. Much lighter on {{ activeConnectionName }} for large tables.
              </span>
              <span v-if="preview?.filters_at_source === false" class="block text-2xs text-amber-700 dark:text-amber-400" data-testid="cq-incremental-local">
                {{ activeConnectionName }} cannot be asked for only the new rows, so each refresh still runs the
                whole query and takes what is new from the result. Rows are kept here even after they leave that result.
              </span>
            </span>
          </label>

          <div v-if="incr.enabled" class="mt-3 ms-6 space-y-3" data-testid="cq-incremental-settings">
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Cursor column</label>
                <select v-model="incr.cursor_column" data-testid="cq-cursor-column"
                        class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white">
                  <option value="">Choose a column…</option>
                  <option v-for="c in resultColumns" :key="c" :value="c">{{ c }}</option>
                </select>
                <p class="text-2xs text-ink-muted mt-1">
                  A date, timestamp or ever-increasing number that grows whenever a row is added or changed
                  (e.g. <span class="font-mono">updated_at</span>).
                </p>
              </div>
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Re-read the last</label>
                <div class="flex items-center gap-2">
                  <input type="number" min="0" v-model.number="incr.lookback_amount" data-testid="cq-lookback"
                         class="w-20 text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white" />
                  <select v-model="incr.lookback_unit" data-testid="cq-lookback-unit"
                          class="text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white">
                    <option value="minutes">minutes</option>
                    <option value="hours">hours</option>
                    <option value="days">days</option>
                    <option value="units">cursor values (numeric cursor)</option>
                  </select>
                </div>
                <p class="text-2xs text-ink-muted mt-1">
                  Each refresh goes back this far before where the last one ended, to catch rows that arrived late.
                </p>
              </div>
            </div>

            <div>
              <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Can rows change after they are created?</label>
              <label class="flex items-start gap-2 mb-1 cursor-pointer">
                <input type="radio" value="merge" v-model="incr.mode" data-testid="cq-incr-merge" class="mt-0.5" />
                <span class="text-sm dark:text-gray-200">Yes — match rows by a key, the newest version wins</span>
              </label>
              <div v-if="incr.mode === 'merge'" class="ms-6 mb-2">
                <div class="flex flex-wrap gap-1.5" data-testid="cq-unique-key">
                  <label v-for="c in resultColumns" :key="c"
                         class="flex items-center gap-1 text-2xs border rounded px-1.5 py-0.5 cursor-pointer"
                         :class="incr.unique_key.includes(c)
                           ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
                           : 'border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300'">
                    <input type="checkbox" class="hidden" :value="c" v-model="incr.unique_key" :data-testid="`cq-key-${c}`" />
                    <span class="font-mono">{{ c }}</span>
                  </label>
                </div>
                <p class="text-2xs text-ink-muted mt-1">The column(s) that identify one row, e.g. <span class="font-mono">id</span>.</p>
              </div>
              <label class="flex items-start gap-2 cursor-pointer">
                <input type="radio" value="append" v-model="incr.mode" data-testid="cq-incr-append" class="mt-0.5" />
                <span>
                  <span class="text-sm dark:text-gray-200">No — rows are only ever added, or the query groups by a period</span>
                  <span class="block text-2xs text-gray-500 dark:text-gray-400">No key needed: the re-read range is replaced as a whole.</span>
                </span>
              </label>
            </div>

            <button type="button" class="text-2xs text-blue-600 dark:text-blue-400 hover:underline"
                    data-testid="cq-incr-advanced-toggle" @click="incr.showAdvanced = !incr.showAdvanced">
              {{ incr.showAdvanced ? 'Hide' : 'Show' }} advanced options
            </button>
            <div v-if="incr.showAdvanced" class="grid grid-cols-2 gap-3" data-testid="cq-incr-advanced">
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Full refresh every</label>
                <div class="flex items-center gap-2">
                  <input type="number" min="0" v-model.number="incr.full_refresh_every_days" data-testid="cq-full-every"
                         class="w-20 text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white" />
                  <span class="text-sm text-gray-600 dark:text-gray-300">days</span>
                </div>
                <p class="text-2xs text-ink-muted mt-1">The only way rows deleted at the source disappear here. 0 turns it off.</p>
              </div>
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Start from <span class="text-ink-muted">(optional)</span></label>
                <input v-model="incr.initial_value" data-testid="cq-initial-value" placeholder="2024-01-01"
                       class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white font-mono" />
                <p class="text-2xs text-ink-muted mt-1">Leave older rows out of the first load.</p>
              </div>
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Cursor type</label>
                <select v-model="incr.cursor_kind" data-testid="cq-cursor-kind"
                        class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white">
                  <option value="">Detect from the data</option>
                  <option value="timestamp">Timestamp</option>
                  <option value="date">Date</option>
                  <option value="number">Number</option>
                  <option value="text">Text</option>
                </select>
                <p class="text-2xs text-ink-muted mt-1">
                  Required when the query places the filter itself with
                  <span class="font-mono" v-pre>{{since}}</span> / <span class="font-mono" v-pre>{{until}}</span>.
                </p>
              </div>
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Query groups by</label>
                <select v-model="incr.grain" data-testid="cq-grain"
                        class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1.5 dark:bg-gray-900 dark:text-white">
                  <option value="">Nothing — one row per source row</option>
                  <option value="hour">Hour</option>
                  <option value="day">Day</option>
                  <option value="month">Month</option>
                </select>
                <p class="text-2xs text-ink-muted mt-1">Starts each refresh on a whole period, so no total is built from part of one.</p>
              </div>
            </div>

            <p v-if="!isRequest && preview?.filters_at_source !== false" class="text-2xs text-ink-muted" data-testid="cq-placeholder-hint">
              By default the filter is added to your query for you. For a join or a large table, place it on the
              source column yourself so the source can use its index:
              <span class="font-mono" v-pre>WHERE updated_at &gt;= {{since}}</span>.
            </p>
            <p v-if="incrError" class="text-2xs text-red-600" data-testid="cq-incr-error">{{ incrError }}</p>
          </div>

          <label class="flex items-center gap-2 mt-4 text-sm dark:text-gray-200">
            <input type="checkbox" v-model="incr.guard_enabled" data-testid="cq-guard-enabled" />
            Keep the previous data if a refresh loses more than
            <input type="number" min="1" max="100" v-model.number="incr.row_drop_guard_pct" data-testid="cq-guard-pct"
                   :disabled="!incr.guard_enabled"
                   class="w-16 text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1 dark:bg-gray-900 dark:text-white disabled:opacity-40" />
            % of the rows
          </label>
        </div>
        </template>

        <!-- Per-user: nothing is cached on the table itself, so its status is
             who holds a copy and what those copies take on disk. -->
        <div v-if="editing && cq && perUser" data-testid="cq-copies-status"
             class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 space-y-1">
          <i18n-t scope="global" keypath="customTableCopies.statusChecked" tag="div">
            <template #when><b>{{ cq.last_refreshed_at ? copyWhen(cq.last_refreshed_at) : t('customTableCopies.statusNever') }}</b></template>
          </i18n-t>
          <i18n-t scope="global" keypath="customTableCopies.statusPeople" tag="div" data-testid="cq-copies-count">
            <template #count><b>{{ copyNum(copiesSummary.copies) }}</b></template>
          </i18n-t>
          <i18n-t scope="global" keypath="customTableCopies.statusSize" tag="div" data-testid="cq-copies-bytes">
            <template #size><b dir="ltr">{{ humanBytes(copiesSummary.bytes) }}</b></template>
          </i18n-t>
          <div v-if="cq.last_refresh_error" class="text-red-600 dark:text-red-400">
            {{ t('customTableCopies.statusError', { error: cq.last_refresh_error }) }}
          </div>
        </div>

        <div v-else-if="editing && cq" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 space-y-1">
          <div>Last refreshed: <b>{{ cq.last_refreshed_at ? new Date(cq.last_refreshed_at + 'Z').toLocaleString() : 'never' }}</b>
            <span v-if="cq.last_refresh_ms != null"> ({{ cq.last_refresh_ms }} ms)</span></div>
          <div>Rows cached: <b>{{ (cq.no_rows || 0).toLocaleString() }}</b></div>
          <div v-if="cq.next_run_at">Next refresh: <b>{{ new Date(cq.next_run_at).toLocaleString() }}</b></div>
          <div v-if="cq.artifact_bytes">Local size: <b>{{ humanBytes(cq.artifact_bytes) }}</b></div>
          <div v-if="cq.checkpoint" data-testid="cq-checkpoint">Loaded up to: <b>{{ cursorText(cq.checkpoint) }}</b>
            <span class="text-ink-muted"> — the next refresh reads from here</span></div>
          <div v-if="cq.last_full_refresh_at">Last full refresh: <b>{{ new Date(cq.last_full_refresh_at + 'Z').toLocaleString() }}</b></div>
          <div v-if="cq.load_progress" data-testid="cq-load-progress" class="text-amber-700 dark:text-amber-400">
            First load in progress: <b>{{ cq.load_progress.windows_done }} of {{ cq.load_progress.windows_total }}</b> chunks,
            {{ (cq.load_progress.rows || 0).toLocaleString() }}<span v-if="cq.load_progress.source_rows"> of {{ cq.load_progress.source_rows.toLocaleString() }}</span> rows.
            It continues on its own.
          </div>
          <div v-if="cq.last_refresh_error" class="text-red-600 dark:text-red-400">Last error: {{ cq.last_refresh_error }}</div>
        </div>

        <div v-if="editing" class="mt-6 pt-4 border-t border-gray-100 dark:border-gray-800">
          <p v-if="perUser" class="text-xs text-gray-500 dark:text-gray-400 mb-3">
            {{ t('customTableCopies.deleteTableHint', { count: cq?.active_agent_count ?? 0 }) }}
          </p>
          <p v-else class="text-xs text-gray-500 dark:text-gray-400 mb-3">
            Deleting removes the cached copy and takes this relation away from
            <b>{{ cq?.active_agent_count ?? 0 }}</b> agent(s) currently using it.
            The source data is not touched.
          </p>
          <button
            data-testid="cq-delete"
            class="flex items-center gap-1.5 border border-red-300 dark:border-red-800 text-red-700 dark:text-red-300 rounded-md px-3 py-1.5 text-xs hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
            :disabled="deleting"
            @click="onDelete"
          >
            <Spinner v-if="deleting" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-trash" class="w-3.5 h-3.5" />
            Delete custom table
          </button>
        </div>
      </div>

      <!-- ============ RUNS ============ -->
      <div v-show="tab === 'runs'" data-testid="cq-runs">
        <div class="flex items-center justify-between mb-2">
          <p class="text-xs text-gray-500 dark:text-gray-400">
            Every refresh of this table: what started it, what it read from
            {{ activeConnectionName }} and what that changed here. Kept for 30 days.
          </p>
          <button class="flex items-center gap-1 text-2xs text-gray-600 dark:text-gray-300 hover:underline"
                  data-testid="cq-runs-reload" :disabled="runsLoading" @click="loadRuns(true)">
            <Spinner v-if="runsLoading" class="w-3 h-3" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3 h-3" />
            Reload
          </button>
        </div>
        <div v-if="runsError" class="text-2xs text-red-600 mb-2">{{ runsError }}</div>
        <div v-if="!runs.length && !runsLoading" class="text-sm text-gray-500 dark:text-gray-400 py-8 text-center" data-testid="cq-runs-empty">
          No refreshes recorded yet.
        </div>
        <div v-else class="flex gap-4">
          <div class="w-1/2 border border-gray-200 dark:border-gray-800 rounded-md overflow-auto" style="max-height: 420px">
            <table class="min-w-full text-2xs">
              <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0">
                <tr class="text-start text-gray-600 dark:text-gray-300">
                  <th class="text-start font-medium px-2 py-1.5">Started</th>
                  <th class="text-start font-medium px-2 py-1.5">Why</th>
                  <th class="text-start font-medium px-2 py-1.5">Kind</th>
                  <th class="text-start font-medium px-2 py-1.5">Result</th>
                  <th class="text-end font-medium px-2 py-1.5">Read</th>
                  <th class="text-end font-medium px-2 py-1.5">Took</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="r in runs" :key="r.id" :data-testid="`cq-run-${r.status}`"
                    class="border-t border-gray-100 dark:border-gray-800 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/50"
                    :class="selectedRun?.id === r.id ? 'bg-blue-50 dark:bg-blue-900/20' : ''"
                    @click="openRun(r)">
                  <td class="px-2 py-1.5 whitespace-nowrap text-gray-700 dark:text-gray-300">{{ when(r.started_at) }}</td>
                  <td class="px-2 py-1.5 text-gray-700 dark:text-gray-300">{{ triggerLabel(r.trigger) }}</td>
                  <td class="px-2 py-1.5 text-gray-700 dark:text-gray-300">{{ r.mode === 'incremental' ? 'New rows' : 'Full' }}</td>
                  <td class="px-2 py-1.5">
                    <span class="px-1.5 py-0.5 rounded" :class="statusClass(r.status)">{{ statusLabel(r) }}</span>
                  </td>
                  <td class="px-2 py-1.5 text-end font-mono text-gray-700 dark:text-gray-300">{{ r.rows_read != null ? r.rows_read.toLocaleString() : '—' }}</td>
                  <td class="px-2 py-1.5 text-end text-gray-700 dark:text-gray-300 whitespace-nowrap">{{ took(r.elapsed_ms) }}</td>
                </tr>
              </tbody>
            </table>
            <button v-if="runsHasMore" class="w-full text-2xs text-blue-600 dark:text-blue-400 py-1.5 hover:underline"
                    data-testid="cq-runs-more" @click="loadRuns(false)">Show older</button>
          </div>

          <div class="w-1/2 text-xs text-gray-700 dark:text-gray-300" data-testid="cq-run-detail">
            <div v-if="!selectedRun" class="text-gray-500 dark:text-gray-400 py-8 text-center">Select a run to see what it did.</div>
            <div v-else class="space-y-2">
              <div class="flex items-center gap-2">
                <span class="px-1.5 py-0.5 rounded text-2xs" :class="statusClass(selectedRun.status)">{{ statusLabel(selectedRun) }}</span>
                <span>{{ when(selectedRun.started_at) }}</span>
                <span v-if="selectedRun.triggered_by" class="text-ink-muted">by {{ selectedRun.triggered_by.name }}</span>
              </div>
              <div v-if="selectedRun.error" class="text-2xs text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md px-2 py-1.5"
                   data-testid="cq-run-error">{{ selectedRun.error }}</div>
              <div class="grid grid-cols-2 gap-x-4 gap-y-1 text-2xs">
                <div>Read from source: <b>{{ num(selectedRun.rows_read) }}</b></div>
                <div>Rows in table after: <b>{{ num(selectedRun.row_count) }}</b></div>
                <div>Added: <b>{{ num(selectedRun.rows_inserted) }}</b></div>
                <div>Changed: <b>{{ num(selectedRun.rows_updated) }}</b></div>
                <div>Removed: <b>{{ num(selectedRun.rows_deleted) }}</b></div>
                <div>Size: <b>{{ humanBytes(selectedRun.artifact_bytes) }}</b></div>
                <div v-if="selectedRun.window_from" class="col-span-2">Read from: <b>{{ cursorText(selectedRun.window_from) }}</b></div>
                <div v-if="selectedRun.window_to" class="col-span-2">Loaded up to: <b>{{ cursorText(selectedRun.window_to) }}</b></div>
              </div>
              <div v-if="selectedRun.chunks?.length" data-testid="cq-run-chunks">
                <div class="text-2xs font-medium mb-1">Chunks ({{ selectedRun.chunks_done }} of {{ selectedRun.chunks_total }} done)</div>
                <div class="flex flex-wrap gap-0.5">
                  <span v-for="(c, i) in selectedRun.chunks" :key="i"
                        :title="`${cursorText(c.lo) || 'start'} → ${cursorText(c.hi) || 'end'}: ${c.rows != null ? c.rows.toLocaleString() + ' rows' : 'not loaded yet'}`"
                        class="w-3 h-3 rounded-sm"
                        :class="c.status === 'done' ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-700'"></span>
                </div>
              </div>
              <div v-if="selectedRun.events?.length" data-testid="cq-run-events">
                <div class="text-2xs font-medium mb-1">Log</div>
                <ul class="text-2xs space-y-0.5 max-h-32 overflow-auto">
                  <li v-for="(e, i) in selectedRun.events" :key="i"
                      :class="e.level === 'warning' ? 'text-amber-700 dark:text-amber-400' : e.level === 'error' ? 'text-red-600' : ''">
                    <span class="text-ink-muted font-mono">{{ (e.at || '').slice(11, 19) }}</span> {{ e.message }}
                  </li>
                </ul>
              </div>
              <div v-if="selectedRun.rendered_query" data-testid="cq-run-query">
                <div class="text-2xs font-medium mb-1">Query sent to the source</div>
                <!-- A query reads left to right in every UI language. -->
                <pre dir="ltr" class="text-start text-2xs font-mono bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-md p-2 overflow-auto max-h-32 whitespace-pre-wrap">{{ selectedRun.rendered_query }}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ============ COPIES (per-user tables) ============ -->
      <div v-show="tab === 'copies'" data-testid="cq-copies">
        <div class="flex items-start justify-between gap-4 mb-2">
          <p class="text-xs text-gray-500 dark:text-gray-400">{{ t('customTableCopies.intro') }}</p>
          <div class="flex items-center gap-3 flex-shrink-0">
            <button class="flex items-center gap-1 text-2xs text-gray-600 dark:text-gray-300 hover:underline"
                    data-testid="cq-copies-reload" :disabled="copiesLoading" @click="loadCopies">
              <Spinner v-if="copiesLoading" class="w-3 h-3" />
              <UIcon v-else name="heroicons-arrow-path" class="w-3 h-3" />
              {{ t('customTableCopies.reload') }}
            </button>
            <button v-if="copiesSummary.copies"
                    class="flex items-center gap-1 text-2xs text-red-700 dark:text-red-300 hover:underline disabled:opacity-50"
                    data-testid="cq-copies-delete-all" :disabled="copyDeleting" @click="copyToDelete = 'all'">
              <UIcon name="heroicons-trash" class="w-3 h-3" />
              {{ t('customTableCopies.deleteAll') }}
            </button>
          </div>
        </div>
        <div v-if="copiesError" class="text-2xs text-red-600 mb-2" data-testid="cq-copies-error">{{ copiesError }}</div>

        <!-- Asked in place rather than in a second dialog on top of this one. -->
        <div v-if="copyToDelete" data-testid="cq-copies-confirm"
             class="mb-2 flex items-center justify-between gap-3 rounded-md bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 px-3 py-2">
          <p class="text-xs text-red-800 dark:text-red-200">
            {{ copyToDelete === 'all'
              ? t('customTableCopies.confirmAll')
              : t('customTableCopies.confirmOne', { name: copyPerson(copyToDelete) }) }}
          </p>
          <div class="flex items-center gap-2 flex-shrink-0">
            <button class="px-2 py-1 text-xs text-gray-600 dark:text-gray-300 hover:underline"
                    data-testid="cq-copies-confirm-cancel" :disabled="copyDeleting" @click="copyToDelete = null">
              {{ t('common.cancel') }}
            </button>
            <button class="flex items-center gap-1.5 bg-red-600 hover:bg-red-700 text-white rounded-md px-3 py-1 text-xs disabled:opacity-50"
                    data-testid="cq-copies-confirm-delete" :disabled="copyDeleting" @click="onDeleteCopies">
              <Spinner v-if="copyDeleting" class="w-3.5 h-3.5" />
              {{ copyToDelete === 'all' ? t('customTableCopies.deleteAll') : t('customTableCopies.deleteCopy') }}
            </button>
          </div>
        </div>

        <div v-if="!copies.length && !copiesLoading && !copiesError"
             class="text-sm text-gray-500 dark:text-gray-400 py-8 text-center" data-testid="cq-copies-empty">
          {{ t('customTableCopies.empty') }}
        </div>
        <div v-else-if="copies.length" class="border border-gray-200 dark:border-gray-800 rounded-md overflow-auto" style="max-height: 420px">
          <table class="min-w-full text-2xs">
            <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0">
              <tr class="text-gray-600 dark:text-gray-300">
                <th class="text-start font-medium px-2 py-1.5">{{ t('customTableCopies.person') }}</th>
                <th class="text-start font-medium px-2 py-1.5">{{ t('customTableCopies.status') }}</th>
                <th class="text-end font-medium px-2 py-1.5">{{ t('customTableCopies.rows') }}</th>
                <th class="text-end font-medium px-2 py-1.5">{{ t('customTableCopies.size') }}</th>
                <th class="text-start font-medium px-2 py-1.5">{{ t('customTableCopies.built') }}</th>
                <th class="text-start font-medium px-2 py-1.5">{{ t('customTableCopies.lastUsed') }}</th>
                <th class="px-2 py-1.5"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in copies" :key="c.id" :data-testid="`cq-copy-row-${c.status}`"
                  class="border-t border-gray-100 dark:border-gray-800 align-top"
                  :class="copyToDelete === c ? 'bg-red-50/60 dark:bg-red-900/10' : ''">
                <td class="px-2 py-1.5 text-gray-700 dark:text-gray-300">
                  <div class="font-medium" v-text-dir>{{ c.user_name || c.user_email || c.user_id }}</div>
                  <div v-if="c.user_name && c.user_email" class="text-ink-muted" dir="ltr">{{ c.user_email }}</div>
                </td>
                <td class="px-2 py-1.5">
                  <span class="px-1.5 py-0.5 rounded whitespace-nowrap" :class="copyStatusClass(c.status)">{{ copyStatusLabel(c.status) }}</span>
                  <div v-if="c.status === 'error' && c.error" data-testid="cq-copy-error"
                       class="mt-1 text-red-700 dark:text-red-300 break-words max-w-xs">{{ c.error }}</div>
                </td>
                <td class="px-2 py-1.5 text-end nums text-gray-700 dark:text-gray-300">{{ c.status === 'ok' ? copyNum(c.row_count) : '—' }}</td>
                <td class="px-2 py-1.5 text-end nums text-gray-700 dark:text-gray-300 whitespace-nowrap"><span dir="ltr">{{ humanBytes(c.artifact_bytes) }}</span></td>
                <td class="px-2 py-1.5 whitespace-nowrap text-gray-700 dark:text-gray-300">{{ copyWhen(c.refreshed_at) }}</td>
                <td class="px-2 py-1.5 whitespace-nowrap text-gray-700 dark:text-gray-300">{{ copyWhen(c.last_used_at) }}</td>
                <td class="px-2 py-1.5 text-end whitespace-nowrap">
                  <!-- A removed copy has nothing left to delete; it comes
                       back the next time its owner asks. -->
                  <button v-if="c.status !== 'evicted'"
                          class="text-red-700 dark:text-red-300 hover:underline disabled:opacity-50"
                          :data-testid="`cq-copy-delete-${c.user_email || c.id}`"
                          :disabled="copyDeleting" @click="copyToDelete = c">
                    {{ t('customTableCopies.deleteCopy') }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- ============ RLS ============ -->
      <div v-show="tab === 'rls'">
        <!-- Enterprise gate: acceleration is community, row policies are not. -->
        <div v-if="!rlsLicensed" data-testid="cq-rls-upgrade"
             class="max-w-xl mx-auto my-8 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 dark:border-amber-800 p-5">
          <div class="flex items-center gap-2 font-medium text-amber-800 dark:text-amber-300">
            <UIcon name="heroicons-sparkles" class="w-5 h-5" />
            Row-level security is an Enterprise feature
          </div>
          <p class="text-sm text-amber-700 dark:text-amber-400 mt-2">
            Filter the cached copy per user — by profile attribute, group or role —
            so each person sees only their own rows. Available with an enterprise
            license.
          </p>
        </div>
        <div v-else-if="!editing" class="text-sm text-gray-500 dark:text-gray-400 py-8 text-center">
          Save the query first — a row policy filters on its result columns.
        </div>
        <div v-else class="flex gap-5">
          <!-- Left: the rule -->
          <div class="w-1/2 space-y-3">
            <label class="flex items-start gap-2 cursor-pointer">
              <input data-testid="cq-rls-enabled" type="checkbox" v-model="rls.enabled" class="mt-0.5" />
              <span>
                <span class="text-xs font-medium text-gray-700 dark:text-gray-200">Filter rows per user</span>
                <span class="block text-2xs text-gray-500 dark:text-gray-400">
                  The cached copy holds every row. With this on, each person sees
                  only the rows their identity allows.
                </span>
              </span>
            </label>

            <template v-if="rls.enabled">
              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Filter on column</label>
                <select data-testid="cq-rls-column" v-model="rls.column"
                        class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                  <option value="">Choose a column…</option>
                  <option v-for="c in resultColumns" :key="c" :value="c">{{ c }}</option>
                </select>
              </div>

              <div>
                <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">Match against</label>
                <select data-testid="cq-rls-source" v-model="rls.source"
                        class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                  <option value="">Nothing — use grants only</option>
                  <option v-for="k in rlsOptions.attribute_keys" :key="k" :value="`profile.${k}`">
                    Profile: {{ k }}
                  </option>
                  <option value="user.email">Email address</option>
                  <option value="groups">Their groups</option>
                  <option value="roles">Their roles</option>
                </select>
                <p v-if="!rlsOptions.attribute_keys.length" class="text-2xs text-ink-muted mt-1">
                  No profile attributes have synced for this org yet.
                </p>
              </div>

              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="text-2xs text-gray-500 dark:text-gray-400">Grants</label>
                  <button data-testid="cq-rls-add-grant" class="text-2xs text-blue-600 hover:underline" @click="addGrant">
                    + Add grant
                  </button>
                </div>
                <p class="text-2xs text-ink-muted mb-1.5">
                  Give a group or role extra values, or <code>*</code> to see everything.
                </p>
                <div v-for="(g, i) in rls.grants" :key="i" class="flex items-center gap-1.5 mb-1.5">
                  <select :data-testid="`cq-rls-grant-type-${i}`" v-model="g.principal_type"
                          class="border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-2xs bg-white dark:bg-gray-900">
                    <option value="group">Group</option>
                    <option value="role">Role</option>
                    <option value="user">User</option>
                  </select>
                  <select :data-testid="`cq-rls-grant-principal-${i}`" v-model="g.principal_id"
                          class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-2xs bg-white dark:bg-gray-900">
                    <option value="">Choose…</option>
                    <option v-for="p in principalsFor(g.principal_type)" :key="p.id" :value="p.id">{{ p.name }}</option>
                  </select>
                  <input :data-testid="`cq-rls-grant-values-${i}`" v-model="g.valuesText" placeholder="EMEA, APAC or *"
                         class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-2xs bg-white dark:bg-gray-900" />
                  <button :data-testid="`cq-rls-grant-remove-${i}`" class="text-ink-muted hover:text-red-600" @click="rls.grants.splice(i, 1)">
                    <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              <label class="flex items-start gap-2 cursor-pointer">
                <input data-testid="cq-rls-default-deny" type="checkbox" v-model="rls.default_deny" class="mt-0.5" />
                <span class="text-2xs text-gray-600 dark:text-gray-300">
                  Show no rows when the value can't be resolved
                  <span class="block text-ink-muted">
                    Recommended. Turning this off shows <b>every</b> row to anyone the
                    policy can't place.
                  </span>
                </span>
              </label>
            </template>

            <p v-if="rlsError" data-testid="cq-rls-error" class="text-2xs text-red-600">{{ rlsError }}</p>
            <button
              data-testid="cq-rls-save"
              class="border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
              :disabled="savingRls" @click="onSaveRls">
              <Spinner v-if="savingRls" class="w-3.5 h-3.5 inline" />
              Save policy
            </button>
          </div>

          <!-- Right: preview as a specific person -->
          <div class="w-1/2 border-l border-gray-100 dark:border-gray-800 pl-5">
            <label class="block text-2xs text-gray-500 dark:text-gray-400 mb-1">
              Preview as
            </label>
            <div class="flex items-center gap-1.5">
              <select data-testid="cq-rls-as-user" v-model="rlsPreviewUserId"
                      class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                <option value="">Choose a member…</option>
                <option v-for="m in rlsOptions.members" :key="m.id" :value="m.id">{{ m.name }}</option>
              </select>
              <button
                data-testid="cq-rls-preview"
                class="border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
                :disabled="!rlsPreviewUserId || rlsPreviewing" @click="onPreviewAsUser">
                <Spinner v-if="rlsPreviewing" class="w-3.5 h-3.5 inline" />
                Run
              </button>
            </div>
            <p class="text-2xs text-ink-muted mt-1">
              Uses the rule above, saved or not.
            </p>

            <p v-if="rlsPreviewError" class="text-2xs text-red-600 mt-2">{{ rlsPreviewError }}</p>

            <div v-if="rlsPreview" class="mt-3">
              <div data-testid="cq-rls-verdict"
                   class="text-2xs rounded-md px-2 py-1.5 mb-2"
                   :class="rlsPreview.denied
                     ? 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'
                     : rlsPreview.filtered
                       ? 'bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-300'
                       : 'bg-gray-50 dark:bg-gray-800/50 text-gray-600 dark:text-gray-300'">
                <template v-if="rlsPreview.denied">
                  Sees <b>no rows</b> — {{ rlsPreview.reason }}.
                </template>
                <template v-else-if="rlsPreview.filtered">
                  Sees rows where <b>{{ rlsPreview.column }}</b> is
                  <b>{{ rlsPreview.allowed_values.join(', ') }}</b>.
                </template>
                <template v-else>
                  Sees <b>every row</b> — {{ rlsPreview.reason }}.
                </template>
              </div>
              <div class="overflow-auto max-h-64 border border-gray-100 dark:border-gray-800 rounded-md">
                <table class="min-w-full text-2xs">
                  <thead class="bg-gray-50 dark:bg-gray-800/50 sticky top-0">
                    <tr>
                      <th v-for="c in rlsPreview.columns" :key="c.name"
                          class="text-left px-2 py-1 font-medium text-gray-600 dark:text-gray-300">{{ c.name }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="(r, i) in rlsPreview.rows" :key="i" class="border-t border-gray-100 dark:border-gray-800">
                      <td v-for="(v, j) in r" :key="j" class="px-2 py-1 text-gray-700 dark:text-gray-300">{{ v }}</td>
                    </tr>
                    <tr v-if="!rlsPreview.rows.length">
                      <td :colspan="Math.max(rlsPreview.columns.length, 1)"
                          class="px-2 py-3 text-center text-ink-muted">No rows.</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- First load of a new table: rows arriving from the source. Starts at
           zero (the row is created before the source is asked anything) and
           fills as batches land; a source that cannot be counted shows the
           running row total with no fraction. -->
      <div
        v-if="saving && !editing && !perUser"
        data-testid="cq-save-progress"
        :data-pct="savePct === null ? '' : savePct"
        class="mt-4 rounded-md border border-blue-200 dark:border-blue-900 bg-blue-50/60 dark:bg-blue-900/20 px-3 py-2.5"
      >
        <div class="flex items-center justify-between text-xs text-blue-900 dark:text-blue-200">
          <span class="font-medium" data-testid="cq-save-progress-label">{{ saveProgressLabel }}</span>
          <span v-if="savePct !== null" class="tabular-nums" data-testid="cq-save-progress-pct">{{ savePct }}%</span>
        </div>
        <div
          class="mt-2 h-1.5 w-full rounded-full bg-blue-100 dark:bg-blue-950 overflow-hidden"
          role="progressbar" aria-valuemin="0" aria-valuemax="100" :aria-valuenow="savePct ?? undefined"
        >
          <div
            data-testid="cq-save-progress-bar"
            class="h-full rounded-full bg-blue-600 transition-all duration-500"
            :class="{ 'animate-pulse': savePct === null && !!saveProgress }"
            :style="{ width: (savePct === null ? (saveProgress ? 100 : 0) : savePct) + '%' }"
          ></div>
        </div>
        <p class="mt-1.5 text-2xs text-blue-800/80 dark:text-blue-300/80">
          Reading from <span class="font-medium">{{ activeConnectionName }}</span>. Keep this window open until it finishes.
        </p>
      </div>

      <!-- Footer -->
      <div class="flex items-center justify-between mt-5 pt-4 border-t border-gray-100 dark:border-gray-800">
        <div class="text-2xs text-ink-muted">
          <span v-if="!canSave && tab === 'query'">Run a preview before saving.</span>
        </div>
        <div class="flex items-center gap-2">
          <button class="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:underline" @click="close">Cancel</button>
          <button
            v-if="editing"
            data-testid="cq-refresh-now"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :title="perUser ? t('customTableCopies.rebuildAllHint') : undefined"
            :disabled="refreshingNow" @click="onRefreshNow(false)"
          >
            <Spinner v-if="refreshingNow" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
            <template v-if="perUser">{{ t('customTableCopies.rebuildAll') }}</template>
            <template v-else>Refresh now</template>
          </button>
          <button
            v-if="editing && !perUser && cq?.refresh_mode && cq.refresh_mode !== 'full'"
            data-testid="cq-full-refresh"
            title="Rebuild the whole table from the source: picks up deleted rows and changes older than the lookback"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :disabled="refreshingNow" @click="onRefreshNow(true)"
          >
            <UIcon name="heroicons-arrow-path-rounded-square" class="w-3.5 h-3.5" />
            Full refresh
          </button>
          <button
            data-testid="cq-save"
            class="bg-blue-600 hover:bg-blue-700 text-white rounded-md px-4 py-1.5 text-xs disabled:opacity-50"
            :disabled="!canSave || saving"
            @click="onSave"
          >
            <Spinner v-if="saving" class="w-3.5 h-3.5" />
            <span v-else>{{ editing ? 'Save changes' : perUser ? t('customTableCopies.create') : 'Create & cache' }}</span>
          </button>
        </div>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import CachedColumnsEditor from '@/components/datasources/CachedColumnsEditor.vue'
import {
  columnEditsPayload, descriptionAfterSuggestion, descriptionToSave, editsFromColumns, hasColumnMetadata,
  type ColumnEdit,
} from '~/utils/columnMetadata'
import { useEnterprise } from '~/ee/composables/useEnterprise'
const ROW_LIMIT = 100

const props = defineProps<{
  modelValue: boolean
  // The connection to default to. An agent can have several, so the modal
  // owns the actual choice via `selectedConnectionId` below.
  connectionId: string
  connectionName: string
  connectionType?: string
  // Every accelerable connection on this agent, so a new query can be created
  // against any of them without leaving the modal.
  // `auth_policy` decides whether a table here is cached once per person;
  // a caller that does not have it leaves it out and the modal asks.
  connections?: Array<{ id: string; name: string; type?: string; auth_policy?: string }>
  cq?: any | null
  activateForDatasourceId?: string
  // Prefill for a NEW custom table, e.g. from a table's data preview. Ignored
  // when editing (`cq` set).
  initialSql?: string
  initialName?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved'): void
  (e: 'deleted'): void
}>()

const toast = useToast()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const editing = computed(() => !!props.cq?.id)

// Which connection this query runs against. Fixed once created — moving an
// existing query would mean re-extracting from a different source under the
// same name, so editing shows it read-only.
const selectedConnectionId = ref<string>('')
const availableConnections = computed(() => {
  const list = props.connections || []
  if (list.length) return list
  return props.connectionId ? [{ id: props.connectionId, name: props.connectionName, type: props.connectionType }] : []
})
const activeConnection = computed(() =>
  availableConnections.value.find((c) => c.id === selectedConnectionId.value)
  || availableConnections.value[0]
  || { id: props.connectionId, name: props.connectionName, type: props.connectionType })
const activeConnectionName = computed(() => activeConnection.value?.name || props.connectionName)
const activeConnectionType = computed(() => activeConnection.value?.type || props.connectionType)
const connectionOptions = computed(() =>
  availableConnections.value.map((c) => ({ id: c.id, name: c.name, type: c.type })))

// ---------------------------------------------------------------------------
// Per-user copies
// ---------------------------------------------------------------------------
// On a connection where each person signs in with their own credentials
// (`auth_policy: user_required`) a custom table is cached once per person,
// built with that person's credentials the first time they ask. A saved table
// says so itself (`per_user`); before it exists the connection decides. Not
// every caller knows the connection's policy (a result card only has its id,
// name and type), so it is looked up once per connection when missing.
const fetchedAuthPolicy = ref<Record<string, string>>({})
function authPolicyOf(connection: any): string | undefined {
  return connection?.auth_policy || fetchedAuthPolicy.value[String(connection?.id || '')]
}
async function ensureAuthPolicy() {
  const connection: any = activeConnection.value
  const id = String(connection?.id || '')
  if (!id || editing.value || authPolicyOf(connection) !== undefined) return
  try {
    const { data, error } = await useMyFetch(`/connections/${id}`, { method: 'GET' })
    if (error.value || !data.value) return
    fetchedAuthPolicy.value = { ...fetchedAuthPolicy.value, [id]: (data.value as any).auth_policy || 'system_only' }
  } catch { /* stays unknown: the table is then described as a shared one */ }
}
const perUser = computed(() =>
  editing.value ? !!props.cq?.per_user : authPolicyOf(activeConnection.value) === 'user_required')

// The query is written in the source's dialect against its schema, so a preview
// taken on one connection says nothing about another — drop it on switch
// rather than letting a stale result satisfy the save gate.
watch(selectedConnectionId, (next, prev) => {
  if (!prev || next === prev) return
  preview.value = null
  previewError.value = ''
  targetDatasetId.value = ''
  loadTargets()
  ensureAuthPolicy()
})

// ---------------------------------------------------------------------------
// Targets (Power BI semantic models)
// ---------------------------------------------------------------------------
// A DAX custom table runs against one semantic model. The backend resolves it
// from the tables the DAX references; this picker is the explicit override,
// sent along with preview and save so both address the same model.
const isDax = computed(() => (activeConnectionType.value || '').toLowerCase() === 'powerbi')
// Any connector that can run a query can be cached, and they do not all speak
// SQL. The language only decides how the editor highlights and what the label
// says; the query is sent to the connector as written.
const SQL_TYPES = [
  'aws_athena', 'aws_redshift', 'bigquery', 'clickhouse', 'databricks_sql', 'druid', 'duckdb', 'mariadb',
  'ms_fabric', 'mssql', 'mysql', 'oracledb', 'postgresql', 'postgresql_multi', 'snowflake', 'spark_connect',
  'sqlite', 'starburst', 'sybase', 'teradata', 'trino',
]
const queryLanguage = computed(() => {
  const type = (activeConnectionType.value || '').toLowerCase()
  if (type === 'powerbi') return 'dax'
  if (type === 'posthog') return 'hogql'
  if (type === 'salesforce') return 'soql'
  return SQL_TYPES.includes(type) ? 'sql' : 'other'
})
const isRequest = computed(() => queryLanguage.value === 'other')
const editorLanguage = computed(() => {
  if (queryLanguage.value === 'other') return /^\s*[{[]/.test(form.definition_sql) ? 'json' : 'text'
  return queryLanguage.value === 'soql' ? 'sql' : queryLanguage.value
})
const targets = ref<any[]>([])
const targetDatasetId = ref('')
const currentTarget = computed(() => targets.value.find((t) => t.datasetId === targetDatasetId.value) || null)
function targetPayload() {
  const t = currentTarget.value
  return t ? { datasetId: t.datasetId, workspaceId: t.workspaceId, datasetName: t.datasetName } : null
}
async function loadTargets() {
  targets.value = []
  if (!isDax.value || !activeConnection.value?.id) return
  try {
    const { data, error } = await useMyFetch(`/connections/${activeConnection.value.id}/custom-queries/targets`, { method: 'GET' })
    if (!error.value) targets.value = (data.value as any[]) || []
  } catch { /* the picker simply stays empty; detection still works */ }
}

// A per-user table has no refresh runs and no BOW-side row policy (the
// source's own row-level security decides what each copy holds): it lists
// people's copies instead.
const tabs = computed(() => perUser.value ? [
  { key: 'query', label: 'Query', disabled: false },
  { key: 'settings', label: 'Settings', disabled: false },
  { key: 'copies', label: t('customTableCopies.tab'), disabled: !editing.value, hint: 'save first' },
] : [
  { key: 'query', label: 'Query', disabled: false },
  { key: 'settings', label: 'Settings', disabled: false },
  { key: 'runs', label: 'Runs', disabled: !editing.value, hint: 'save first' },
  {
    key: 'rls',
    label: 'Row-level security',
    disabled: !editing.value,
    hint: 'save first',
  },
])

const tab = ref('query')
const previewing = ref(false)
const saving = ref(false)
const deleting = ref(false)
const refreshingNow = ref(false)
const preview = ref<any>(null)
const previewError = ref('')

// ---------------------------------------------------------------------------
// What the columns mean
// ---------------------------------------------------------------------------
// The preview (or the saved table) returns each column with what the source
// catalog knows about it. The admin's edits are kept apart from that, by
// column name, so running the preview again does not lose them.
const { t, locale } = useI18n()
const columnEdits = ref<Record<string, ColumnEdit>>({})
const reviewedDrift = ref<Set<string>>(new Set())
// The description the backend generated from the columns, when the field is
// showing it. Sent as empty on save, so it stays generated.
const generatedDescription = ref('')
const describedColumns = computed<any[]>(() =>
  (preview.value?.columns || []).filter((c: any) => c?.name).some(hasColumnMetadata)
    ? (preview.value?.columns || []).filter((c: any) => c?.name)
    : [])
const rlsSources = computed<string[]>(() => preview.value?.rls_sources || [])
const descriptionIsGenerated = computed(() =>
  !!generatedDescription.value && form.description.trim() === generatedDescription.value.trim())

function suggestDescription(text: string | null | undefined) {
  form.description = descriptionAfterSuggestion(form.description, generatedDescription.value, text)
  generatedDescription.value = text || ''
}

const form = reactive({
  name: '',
  description: '',
  definition_sql: '',
  refresh_schedule_mode: 'interval',
  refresh_interval_minutes: 60,
  refresh_at_time: '03:00',
})

// ---------------------------------------------------------------------------
// What each refresh fetches
// ---------------------------------------------------------------------------
const DEFAULT_GUARD_PCT = 50
const incr = reactive({
  enabled: false,
  mode: 'merge' as 'merge' | 'append',
  cursor_column: '',
  unique_key: [] as string[],
  lookback_amount: 1,
  lookback_unit: 'days',
  full_refresh_every_days: 7,
  initial_value: '',
  cursor_kind: '',
  grain: '',
  guard_enabled: true,
  row_drop_guard_pct: DEFAULT_GUARD_PCT,
  showAdvanced: false,
})

function loadIncrFromCq() {
  const cq: any = props.cq || null
  const c: any = cq?.refresh_config || {}
  incr.enabled = !!cq && cq.refresh_mode && cq.refresh_mode !== 'full'
  incr.mode = cq?.refresh_mode === 'append' ? 'append' : 'merge'
  incr.cursor_column = c.cursor_column || ''
  incr.unique_key = Array.isArray(c.unique_key) ? [...c.unique_key] : []
  if (c.lookback_units != null) {
    incr.lookback_amount = c.lookback_units
    incr.lookback_unit = 'units'
  } else {
    const secs = c.lookback_seconds != null ? Number(c.lookback_seconds) : 86400
    if (secs % 86400 === 0) { incr.lookback_amount = secs / 86400; incr.lookback_unit = 'days' }
    else if (secs % 3600 === 0) { incr.lookback_amount = secs / 3600; incr.lookback_unit = 'hours' }
    else { incr.lookback_amount = Math.round(secs / 60); incr.lookback_unit = 'minutes' }
  }
  incr.full_refresh_every_days = c.full_refresh_every_days != null ? c.full_refresh_every_days : 7
  incr.initial_value = c.initial_value || ''
  incr.cursor_kind = c.cursor_kind || ''
  incr.grain = c.grain || ''
  // A table created before the guard existed has none; a new one starts with it.
  incr.guard_enabled = cq ? c.row_drop_guard_pct != null : true
  incr.row_drop_guard_pct = c.row_drop_guard_pct != null ? c.row_drop_guard_pct : DEFAULT_GUARD_PCT
  incr.showAdvanced = !!(c.initial_value || c.cursor_kind || c.grain)
}

const usesPlaceholders = computed(() => /\{\{\s*(since|until)\s*\}\}/i.test(form.definition_sql))

// Why the incremental settings cannot be saved yet, in the admin's terms.
const incrError = computed(() => {
  if (!incr.enabled || perUser.value) return ''
  if (!incr.cursor_column) return 'Choose the cursor column.'
  if (incr.mode === 'merge' && !incr.unique_key.length) return 'Choose the key column(s), or say rows are only ever added.'
  if (usesPlaceholders.value && !incr.cursor_kind) return 'This query uses {{since}} — choose the cursor type under advanced options.'
  return ''
})

function refreshPayload() {
  const seconds: Record<string, number> = { minutes: 60, hours: 3600, days: 86400 }
  const amount = Math.max(0, Number(incr.lookback_amount) || 0)
  const config: any = {
    row_drop_guard_pct: incr.guard_enabled ? (Number(incr.row_drop_guard_pct) || DEFAULT_GUARD_PCT) : null,
  }
  // A person's copy is always rebuilt in full: every copy holds different
  // rows, so there is no one checkpoint to continue from.
  const incremental = incr.enabled && !perUser.value
  if (incremental) {
    Object.assign(config, {
      cursor_column: incr.cursor_column,
      cursor_kind: incr.cursor_kind || null,
      unique_key: incr.mode === 'merge' ? incr.unique_key : [],
      lookback_seconds: incr.lookback_unit === 'units' ? null : amount * seconds[incr.lookback_unit],
      lookback_units: incr.lookback_unit === 'units' ? amount : null,
      initial_value: incr.initial_value.trim() || null,
      grain: incr.grain || null,
      full_refresh_every_days: Number(incr.full_refresh_every_days) || 0,
    })
  }
  return { refresh_mode: incremental ? incr.mode : 'full', refresh_config: config }
}

// ---------------------------------------------------------------------------
// Run history
// ---------------------------------------------------------------------------
const runs = ref<any[]>([])
const runsLoading = ref(false)
const runsError = ref('')
const runsHasMore = ref(false)
const runsNextBefore = ref<string | null>(null)
const selectedRun = ref<any>(null)

async function loadRuns(reset: boolean) {
  if (!editing.value || !activeConnection.value?.id) return
  runsLoading.value = true
  runsError.value = ''
  try {
    const query: any = { limit: 20 }
    if (!reset && runsNextBefore.value) query.before = runsNextBefore.value
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/runs`, { method: 'GET', query })
    if (error.value) {
      runsError.value = error.value?.data?.detail || 'Could not load the run history'
      return
    }
    const page: any = data.value || {}
    runs.value = reset ? (page.runs || []) : [...runs.value, ...(page.runs || [])]
    runsHasMore.value = !!page.has_more
    runsNextBefore.value = page.next_before || null
    if (reset && runs.value.length) await openRun(selectedRun.value ? (runs.value.find((r) => r.id === selectedRun.value.id) || runs.value[0]) : runs.value[0])
  } finally {
    runsLoading.value = false
  }
}

async function openRun(run: any) {
  selectedRun.value = run
  const { data, error } = await useMyFetch(
    `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/runs/${run.id}`, { method: 'GET' })
  if (!error.value && data.value && selectedRun.value?.id === run.id) selectedRun.value = data.value
}

// ---------------------------------------------------------------------------
// People's copies (per-user tables)
// ---------------------------------------------------------------------------
const copies = ref<any[]>([])
const copiesLoaded = ref(false)
const copiesLoading = ref(false)
const copiesError = ref('')
// What the confirmation is asking about: one person's copy, or all of them.
const copyToDelete = ref<any | 'all' | null>(null)
const copyDeleting = ref(false)

async function loadCopies() {
  if (!editing.value || !activeConnection.value?.id) return
  copiesLoading.value = true
  copiesError.value = ''
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/user-copies`, { method: 'GET' })
    if (error.value) {
      copiesError.value = error.value?.data?.detail || t('customTableCopies.loadError')
      return
    }
    copies.value = Array.isArray(data.value) ? (data.value as any[]) : []
    copiesLoaded.value = true
  } catch (e: any) {
    copiesError.value = e?.message || t('customTableCopies.loadError')
  } finally {
    copiesLoading.value = false
  }
}

async function onDeleteCopies() {
  const target = copyToDelete.value
  if (!target) return
  copyDeleting.value = true
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/user-copies`,
      { method: 'DELETE', query: target === 'all' ? {} : { copy_id: target.id } })
    if (error.value) {
      toast.add({ title: t('customTableCopies.deleteFailed'), description: error.value?.data?.detail || undefined, color: 'red' })
      return
    }
    const freed = Number((data.value as any)?.reclaimed_bytes || 0)
    toast.add({
      title: target === 'all' ? t('customTableCopies.deletedAll') : t('customTableCopies.deleted'),
      description: freed ? t('customTableCopies.deletedDetail', { size: humanBytes(freed) }) : undefined,
      color: 'green',
    })
    copyToDelete.value = null
    await loadCopies()
    // The table's own row carries the count and size shown outside this modal.
    emit('saved')
  } finally {
    copyDeleting.value = false
  }
}

// How many people hold a copy and what the copies take on disk. The table row
// carries it; once the list has been loaded here it is the fresher of the two.
const copiesSummary = computed(() => {
  if (copiesLoaded.value) {
    const held = copies.value.filter((c) => c.status !== 'evicted')
    return { copies: held.length, bytes: held.reduce((sum, c) => sum + (Number(c.artifact_bytes) || 0), 0) }
  }
  return { copies: Number(props.cq?.user_copies?.copies || 0), bytes: Number(props.cq?.user_copies?.bytes || 0) }
})

const copyAgeOptions = computed(() => [
  { value: 15, label: t('customTableCopies.age15m') },
  { value: 30, label: t('customTableCopies.age30m') },
  { value: 60, label: t('customTableCopies.age1h') },
  { value: 360, label: t('customTableCopies.age6h') },
  { value: 720, label: t('customTableCopies.age12h') },
  { value: 1440, label: t('customTableCopies.age24h') },
])

function copyPerson(c: any) { return c?.user_name || c?.user_email || c?.user_id || '' }
function copyNum(v: any) { return v == null ? '—' : new Intl.NumberFormat(locale.value).format(Number(v)) }
function copyWhen(v: any) {
  if (!v) return '—'
  const s = String(v)
  return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z'))
}
// Keyed on the status the backend sends, never on its label.
function copyStatusLabel(status: string) {
  const key = ({ building: 'stateBuilding', ok: 'stateOk', error: 'stateError', evicted: 'stateEvicted' } as any)[status]
  return key ? t(`customTableCopies.${key}`) : status
}
function copyStatusClass(status: string) {
  if (status === 'ok') return 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
  if (status === 'error') return 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
  if (status === 'building') return 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
  return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
}

function when(v: any) {
  if (!v) return '—'
  const s = String(v)
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z').toLocaleString()
}
function took(ms: any) {
  if (ms == null) return '—'
  return ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`
}
function num(v: any) { return v == null ? '—' : Number(v).toLocaleString() }
function triggerLabel(t: string) {
  return ({ create: 'Created', schedule: 'Schedule', manual: 'Manual', definition: 'Query changed', resume: 'Continued' } as any)[t] || t
}
function statusLabel(r: any) {
  if (r.status === 'partial') return r.chunks_total ? `Paused ${r.chunks_done}/${r.chunks_total}` : 'Paused'
  return ({ success: 'OK', failed: 'Failed', running: 'Running', cancelled: 'Cancelled', interrupted: 'Interrupted' } as any)[r.status] || r.status
}
function statusClass(status: string) {
  if (status === 'success') return 'bg-green-100 text-green-800 dark:bg-green-900/40 dark:text-green-300'
  if (status === 'failed' || status === 'interrupted') return 'bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-300'
  if (status === 'running' || status === 'partial') return 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300'
  return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
}
// A typed cursor value ({kind, value, tz}) as text. Epoch-millisecond numbers
// (monitoring APIs) are shown as the moment they are.
function cursorText(c: any) {
  if (!c || c.value == null) return ''
  if (c.kind === 'timestamp') return String(c.value).replace('T', ' ').replace(/\.000000$/, '') + (c.tz ? ' UTC' : '')
  if (c.kind === 'number' && /^\d{13}$/.test(String(c.value))) {
    return `${new Date(Number(c.value)).toLocaleString()} (${c.value})`
  }
  return String(c.value)
}

// A successful preview is what supplies the column list, so saving without one
// would create a relation with no known shape.
const canSave = computed(() =>
  !!form.name.trim()
  && !!form.definition_sql.trim()
  && !!preview.value
  && !preview.value?.budget_error
  && !incrError.value
)

watch(() => props.modelValue, (open) => {
  if (!open) return
  tab.value = 'query'
  preview.value = null
  previewError.value = ''
  columnEdits.value = editsFromColumns(props.cq?.columns || [])
  reviewedDrift.value = new Set()
  generatedDescription.value = ''
  rlsPreviewUserId.value = ''
  loadRlsFromCq()
  loadIncrFromCq()
  runs.value = []
  selectedRun.value = null
  copies.value = []
  copiesLoaded.value = false
  copiesError.value = ''
  copyToDelete.value = null
  selectedConnectionId.value = props.cq?.connection_id || props.connectionId || (availableConnections.value[0]?.id || '')
  ensureAuthPolicy()
  targetDatasetId.value = props.cq?.target?.datasetId || ''
  loadTargets()
  if (props.cq) {
    form.name = props.cq.name || ''
    form.description = props.cq.description || ''
    form.definition_sql = props.cq.definition_sql || ''
    form.refresh_schedule_mode = props.cq.refresh_schedule_mode || 'interval'
    form.refresh_interval_minutes = props.cq.refresh_interval_minutes || 60
    form.refresh_at_time = props.cq.refresh_at_time || '03:00'
    // Editing an existing relation: its columns are already known, so the
    // preview gate does not apply until the SQL is changed.
    preview.value = {
      columns: props.cq.columns || [], rows: [], budget_error: null, estimated_rows: props.cq.no_rows,
      rls_sources: props.cq.rls_sources || [], saved: true,
    }
    // No description of the admin's own: show the one built from the columns.
    suggestDescription(props.cq.generated_description)
  } else {
    form.name = props.initialName || ''
    form.description = ''
    form.definition_sql = props.initialSql || ''
    form.refresh_schedule_mode = 'interval'
    form.refresh_interval_minutes = 60
    form.refresh_at_time = '03:00'
  }
})

function close() { isOpen.value = false }

// ---------------------------------------------------------------------------
// Row-level security
// ---------------------------------------------------------------------------
// The cached copy holds every row, so a policy here is the only thing standing
// between one person's slice and everybody else's. "Preview as" is therefore
// not a nicety — it is how an admin confirms the rule does what they meant
// before anyone depends on it, and it runs the same code path an agent does.

const { hasFeature } = useEnterprise()
const rlsLicensed = computed(() => hasFeature('rls'))

const rls = reactive({
  enabled: false,
  column: '',
  source: '',
  grants: [] as any[],
  default_deny: true,
})
const rlsOptions = reactive({ attribute_keys: [] as string[], groups: [] as any[], roles: [] as any[], members: [] as any[] })
const rlsError = ref('')
const savingRls = ref(false)
const rlsPreviewUserId = ref('')
const rlsPreview = ref<any>(null)
const rlsPreviewError = ref('')
const rlsPreviewing = ref(false)

// A policy filters on the query's RESULT columns, which is what the preview (or
// the saved relation) knows — not the source table's columns.
const resultColumns = computed(() =>
  (preview.value?.columns || props.cq?.columns || []).map((c: any) => c?.name).filter(Boolean))

function principalsFor(type: string) {
  if (type === 'role') return rlsOptions.roles
  if (type === 'user') return rlsOptions.members
  return rlsOptions.groups
}

function addGrant() {
  rls.grants.push({ principal_type: 'group', principal_id: '', valuesText: '' })
}

function rlsPayload() {
  return {
    rls_enabled: rls.enabled,
    rls_mode: 'attribute',
    rls_default_deny: rls.default_deny,
    rls_policy: rls.enabled ? {
      column: rls.column,
      source: rls.source,
      op: 'in',
      grants: rls.grants
        .filter((g: any) => g.principal_id)
        .map((g: any) => ({
          principal_type: g.principal_type,
          principal_id: g.principal_id,
          values: String(g.valuesText || '').split(',').map((v: string) => v.trim()).filter(Boolean),
        })),
    } : null,
  }
}

function loadRlsFromCq() {
  const cq: any = props.cq || {}
  rls.enabled = !!cq.rls_enabled
  rls.default_deny = cq.rls_default_deny !== false
  const p = cq.rls_policy || {}
  rls.column = p.column || ''
  rls.source = p.source || ''
  rls.grants = (p.grants || []).map((g: any) => ({
    principal_type: g.principal_type || 'group',
    principal_id: g.principal_id || '',
    valuesText: (g.values || []).join(', '),
  }))
  rlsPreview.value = null
  rlsPreviewError.value = ''
  rlsError.value = ''
}

async function loadRlsOptions() {
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/rls-options`, { method: 'GET' })
    if (error.value || !data.value) return
    Object.assign(rlsOptions, data.value)
  } catch { /* the editor degrades to typed values, not to a broken tab */ }
}

async function onSaveRls() {
  savingRls.value = true
  rlsError.value = ''
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/rls`,
      { method: 'PUT', body: rlsPayload() })
    if (error.value) {
      rlsError.value = error.value?.data?.detail || 'Could not save the policy'
    } else {
      emit('saved')
    }
  } catch (e: any) {
    rlsError.value = e?.message || String(e)
  } finally {
    savingRls.value = false
  }
}

async function onPreviewAsUser() {
  rlsPreviewing.value = true
  rlsPreviewError.value = ''
  rlsPreview.value = null
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/rls/preview`,
      { method: 'POST', body: { user_id: rlsPreviewUserId.value, ...rlsPayload() } })
    if (error.value) {
      rlsPreviewError.value = error.value?.data?.detail || 'Preview failed'
    } else {
      rlsPreview.value = data.value
    }
  } catch (e: any) {
    rlsPreviewError.value = e?.message || String(e)
  } finally {
    rlsPreviewing.value = false
  }
}

watch(tab, (t) => {
  if (t === 'runs' && editing.value) loadRuns(true)
  if (t === 'copies' && editing.value) loadCopies()
  if (t !== 'rls' || !editing.value || !rlsLicensed.value) return
  if (!rlsOptions.members.length) loadRlsOptions()
})

// Schedules run in the organization's timezone (same as scheduled reports and
// prompts), so label the field with a zone rather than a bare "UTC".
const localTimezoneLabel = computed(() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time'
  } catch {
    return 'local time'
  }
})

// How many times a day the current schedule fires — used to turn a per-refresh
// scan size into the daily figure an admin is actually choosing.
const refreshesPerDay = computed(() => {
  if (form.refresh_schedule_mode === 'time') return 1
  const mins = Number(form.refresh_interval_minutes) || 60
  return Math.max(1, Math.round(1440 / mins))
})

function approx(n: number) {
  if (n == null) return '—'
  return n >= 1000 ? `~${n.toLocaleString()}` : String(n)
}

function humanBytes(b: number) {
  if (!b) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0, v = b
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`
}

async function runPreview() {
  // Also reached by the editor's Ctrl/⌘ + Enter, which the button's own
  // disabled state does not guard.
  if (previewing.value || !form.definition_sql.trim()) return
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/preview`,
      {
        method: 'POST',
        body: {
          definition_sql: form.definition_sql,
          target: targetPayload(),
          refresh_config: { cursor_column: incr.cursor_column || null, cursor_kind: incr.cursor_kind || null },
          refresh_schedule_mode: form.refresh_schedule_mode,
          refresh_interval_minutes: form.refresh_interval_minutes,
          refresh_at_time: form.refresh_schedule_mode === 'time' ? form.refresh_at_time : null,
        },
      },
    )
    if (error.value) {
      previewError.value = error.value?.data?.detail || 'Query failed'
    } else {
      preview.value = data.value
      suggestDescription((data.value as any)?.suggested_description)
    }
  } catch (e: any) {
    previewError.value = e?.message || String(e)
  } finally {
    previewing.value = false
  }
}

// --- First-load progress ----------------------------------------------------
// Creating a table answers only when its first load has finished, which for a
// large result is minutes of one pending request. The row exists from the
// start, so the list route can say how many rows have landed meanwhile.
const saveProgress = ref<{ rows: number; source_rows: number | null } | null>(null)
let saveProgressTimer: ReturnType<typeof setInterval> | null = null
let saveProgressBusy = false

const savePct = computed<number | null>(() => {
  const p = saveProgress.value
  if (!p) return 0
  if (!p.source_rows) return p.rows > 0 ? null : 0
  // Never 100 before the request itself returns: the file is still being
  // sealed and published after the last row arrives.
  return Math.max(0, Math.min(99, Math.floor((p.rows / p.source_rows) * 100)))
})

const saveProgressLabel = computed(() => {
  const p = saveProgress.value
  if (!p) return 'Starting…'
  if (p.source_rows) {
    if (p.rows >= p.source_rows) return `Finishing — ${p.rows.toLocaleString()} rows read`
    return `Caching ${p.rows.toLocaleString()} of ${p.source_rows.toLocaleString()} rows`
  }
  return p.rows > 0 ? `Caching — ${p.rows.toLocaleString()} rows so far` : 'Counting rows at the source…'
})

async function pollSaveProgress(connectionId: string, name: string) {
  if (saveProgressBusy) return
  saveProgressBusy = true
  try {
    const row = await customTableNamed(connectionId, name)
    if (!row) return
    const live = row.refresh_progress
    const load = row.load_progress
    if (live) {
      saveProgress.value = { rows: Number(live.rows || 0), source_rows: live.source_rows ? Number(live.source_rows) : null }
    } else if (load) {
      saveProgress.value = { rows: Number(load.rows || 0), source_rows: load.source_rows ? Number(load.source_rows) : null }
    }
  } catch {
    /* progress is a courtesy: the save itself reports success or failure */
  } finally {
    saveProgressBusy = false
  }
}

// A create that failed during its first load leaves the table behind (with
// the error on it) so it can be fixed and retried; a create refused before
// that (bad name, name taken) leaves nothing.
// One table's state by name. Scoped to that table on purpose: this is polled
// every second while a load runs, and the full list costs a query per table.
async function customTableNamed(connectionId: string, name: string): Promise<any | null> {
  try {
    const { data, error } = await useMyFetch(
      `/connections/${connectionId}/custom-queries/progress`, { method: 'GET', query: { name } })
    const row: any = data.value
    if (error.value || !row?.exists) return null
    return row
  } catch {
    return null
  }
}

function startSaveProgress(connectionId: string, name: string) {
  stopSaveProgress()
  saveProgress.value = null
  saveProgressTimer = setInterval(() => { pollSaveProgress(connectionId, name) }, 1000)
}

function stopSaveProgress() {
  if (saveProgressTimer) clearInterval(saveProgressTimer)
  saveProgressTimer = null
}

onBeforeUnmount(stopSaveProgress)

async function onSave() {
  saving.value = true
  // Whether this name was already taken before we tried: a failed create is
  // only "created, but not loaded" if this attempt is what created the row.
  let existedBefore = false
  if (!editing.value) {
    existedBefore = !!(await customTableNamed(String(activeConnection.value.id), form.name.trim()))
    // Nothing is loaded when a per-user table is created, so there is no
    // first load to follow.
    if (!perUser.value) startSaveProgress(String(activeConnection.value.id), form.name.trim())
  }
  try {
    const body: any = {
      name: form.name.trim(),
      definition_sql: form.definition_sql,
      // Always a string: an emptied description must be able to clear a
      // stored one, and an untouched generated one is stored as empty.
      description: descriptionToSave(form.description, generatedDescription.value),
      // The complete set of column edits; a column left out follows its source.
      columns: columnEditsPayload(describedColumns.value, columnEdits.value, reviewedDrift.value),
      refresh_schedule_mode: form.refresh_schedule_mode,
      refresh_interval_minutes: form.refresh_interval_minutes,
      refresh_at_time: form.refresh_schedule_mode === 'time' ? form.refresh_at_time : null,
      ...refreshPayload(),
    }
    if (isDax.value) {
      // Always sent on a DAX table: an empty object clears a previous pin
      // (the update route treats an omitted field as "keep").
      body.target = targetPayload() || {}
    }
    if (!editing.value && props.activateForDatasourceId) {
      // Enabled on the agent it was created from; every other agent on the
      // connection gets it inactive, same as a regular table.
      body.activate_for_datasource_id = props.activateForDatasourceId
    }
    const url = editing.value
      ? `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}`
      : `/connections/${activeConnection.value.id}/custom-queries`
    const { data, error } = await useMyFetch(url, { method: editing.value ? 'PUT' : 'POST', body })
    if (error.value) {
      const detail = error.value?.data?.detail || 'Failed'
      const left = (!editing.value && !existedBefore)
        ? await customTableNamed(String(activeConnection.value.id), body.name) : null
      if (left && left.last_refresh_status === 'error') {
        // The table exists; only its first load failed. Leaving this form
        // open would invite a second "Create", which can only fail on the
        // name. Say where the table is and let the list show its error.
        toast.add({
          title: 'Table created, but its first load failed',
          description: `${detail} Open "${body.name}" from the agent's Tables page to retry or change it.`,
          color: 'red',
          timeout: 0,
        })
        emit('saved')
        close()
        return
      }
      toast.add({ title: 'Could not save', description: detail, color: 'red' })
      return
    }
    const saved: any = data.value
    if (saved?.per_user ?? perUser.value) {
      toast.add({
        title: editing.value ? t('customTableCopies.updated') : t('customTableCopies.created'),
        description: t('customTableCopies.savedDetail', { name: saved.name }),
        color: 'green',
      })
      emit('saved')
      close()
      return
    }
    toast.add({
      title: editing.value ? 'Custom table updated' : 'Custom table cached',
      description: `${saved.name} — ${(saved.no_rows || 0).toLocaleString()} rows cached locally`,
      color: 'green',
    })
    emit('saved')
    close()
  } finally {
    stopSaveProgress()
    saving.value = false
  }
}

async function onRefreshNow(full = false) {
  refreshingNow.value = true
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/refresh`,
      { method: 'POST', query: full ? { full: true } : {} },
    )
    if (error.value) {
      toast.add({ title: perUser.value ? t('customTableCopies.rebuildFailed') : 'Refresh failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const r: any = data.value
    if (perUser.value) {
      // Nothing was built: every copy is marked to be rebuilt at its owner's
      // next question.
      toast.add({ title: t('customTableCopies.rebuildQueued'), description: t('customTableCopies.rebuildAllHint'), color: 'green' })
    } else if (r.load_progress) {
      toast.add({
        title: 'Loading in chunks',
        description: `${r.load_progress.windows_done} of ${r.load_progress.windows_total} done — it continues on its own`,
        color: 'amber',
      })
    } else {
      toast.add({ title: full ? 'Rebuilt in full' : 'Refreshed', description: `${(r.no_rows || 0).toLocaleString()} rows in ${r.last_refresh_ms} ms`, color: 'green' })
    }
    emit('saved')
  } finally {
    refreshingNow.value = false
    if (tab.value === 'runs') loadRuns(true)
    if (tab.value === 'copies') loadCopies()
  }
}

async function onDelete() {
  deleting.value = true
  try {
    const { error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}`,
      { method: 'DELETE' },
    )
    if (error.value) {
      toast.add({ title: 'Delete failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    toast.add({ title: 'Custom table deleted', color: 'green' })
    emit('deleted')
    close()
  } finally {
    deleting.value = false
  }
}
</script>
