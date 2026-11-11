<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }" prevent-close>
    <div class="p-5" data-testid="custom-query-modal">
      <!-- Header -->
      <div class="flex items-start justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold dark:text-white">
            {{ editing ? 'Edit custom table' : 'New custom table' }}
          </h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Runs on a schedule and is stored locally, so agents answer from a
            cached copy instead of querying
            <span class="font-medium">{{ activeConnectionName }}</span> every time.
          </p>
        </div>
        <button class="text-ink-muted hover:text-gray-600" @click="close">
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
            <input
              v-model="form.description" data-testid="cq-description"
              placeholder="What this data represents — shown to the agent"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white"
            />
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
            <span data-testid="cq-preview-count">
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
        </div>
      </div>

      <!-- ============ CACHE ============ -->
      <!-- Scrolls inside the modal: with the refresh options open this tab is
           taller than a laptop screen, and the footer must stay reachable. -->
      <div v-show="tab === 'settings'" class="max-h-[62vh] overflow-y-auto pe-1" data-testid="cq-settings">
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

        <div v-if="editing && cq" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 space-y-1">
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
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
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
            :disabled="refreshingNow" @click="onRefreshNow(false)"
          >
            <Spinner v-if="refreshingNow" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
            Refresh now
          </button>
          <button
            v-if="editing && cq?.refresh_mode && cq.refresh_mode !== 'full'"
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
            <span v-else>{{ editing ? 'Save changes' : 'Create & cache' }}</span>
          </button>
        </div>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'
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
  connections?: Array<{ id: string; name: string; type?: string }>
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

// The query is written in the source's dialect against its schema, so a preview
// taken on one connection says nothing about another — drop it on switch
// rather than letting a stale result satisfy the save gate.
watch(selectedConnectionId, (next, prev) => {
  if (!prev || next === prev) return
  preview.value = null
  previewError.value = ''
  targetDatasetId.value = ''
  loadTargets()
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
  'ms_fabric', 'mssql', 'mysql', 'oracledb', 'postgresql', 'snowflake', 'spark_connect', 'sqlite',
  'starburst', 'sybase', 'teradata', 'trino',
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

const tabs = computed(() => [
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
  if (!incr.enabled) return ''
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
  if (incr.enabled) {
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
  return { refresh_mode: incr.enabled ? incr.mode : 'full', refresh_config: config }
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
  rlsPreviewUserId.value = ''
  loadRlsFromCq()
  loadIncrFromCq()
  runs.value = []
  selectedRun.value = null
  selectedConnectionId.value = props.cq?.connection_id || props.connectionId || (availableConnections.value[0]?.id || '')
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
    preview.value = { columns: props.cq.columns || [], rows: [], budget_error: null, estimated_rows: props.cq.no_rows }
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
        },
      },
    )
    if (error.value) {
      previewError.value = error.value?.data?.detail || 'Query failed'
    } else {
      preview.value = data.value
    }
  } catch (e: any) {
    previewError.value = e?.message || String(e)
  } finally {
    previewing.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    const body: any = {
      name: form.name.trim(),
      definition_sql: form.definition_sql,
      description: form.description || null,
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
      toast.add({ title: 'Could not save', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const saved: any = data.value
    toast.add({
      title: editing.value ? 'Custom table updated' : 'Custom table cached',
      description: `${saved.name} — ${(saved.no_rows || 0).toLocaleString()} rows cached locally`,
      color: 'green',
    })
    emit('saved')
    close()
  } finally {
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
      toast.add({ title: 'Refresh failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const r: any = data.value
    if (r.load_progress) {
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
