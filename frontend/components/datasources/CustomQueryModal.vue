<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }" prevent-close>
    <div class="p-5" data-testid="custom-query-modal">
      <!-- Header -->
      <div class="flex items-start justify-between mb-3">
        <div>
          <h2 class="text-lg font-semibold dark:text-white">
            {{ editing ? 'Edit custom query' : 'New custom query' }}
          </h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Runs on a schedule and is stored locally, so agents answer from a
            cached copy instead of querying
            <span class="font-medium">{{ activeConnectionName }}</span> every time.
          </p>
        </div>
        <button class="text-gray-400 hover:text-gray-600" @click="close">
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
                class="ms-1 text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-400">{{ t.hint }}</span>
        </button>
      </div>

      <!-- ============ QUERY ============ -->
      <div v-show="tab === 'query'">
        <!-- Connection picker. An agent can have several; the query runs against
             exactly one, and the SQL below is written in that source's dialect. -->
        <div class="mb-3">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Connection</label>
          <div v-if="editing" class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <DataSourceIcon :type="activeConnectionType" class="h-3.5" />
            <span class="font-medium">{{ activeConnectionName }}</span>
            <span class="text-[11px] text-gray-400">
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
                <span v-if="activeConnectionType" class="text-[11px] text-gray-400">{{ activeConnectionType }}</span>
              </span>
            </template>
            <template #option="{ option }">
              <span class="flex items-center gap-2 min-w-0">
                <DataSourceIcon :type="option.type" class="h-3.5 flex-shrink-0" />
                <span class="truncate">{{ option.name }}</span>
                <span class="text-[11px] text-gray-400">{{ option.type }}</span>
              </span>
            </template>
          </USelectMenu>
          <p v-if="!editing && availableConnections.length > 1" class="text-[10px] text-gray-400 mt-1">
            Switching connections clears the preview — the SQL is dialect- and schema-specific.
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
            <p class="text-[10px] text-gray-400 mt-1">Agents query this as a table name.</p>
          </div>
          <div class="col-span-2">
            <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Description <span class="text-gray-400">(optional)</span></label>
            <input
              v-model="form.description" data-testid="cq-description"
              placeholder="What this data represents — shown to the agent"
              class="w-full text-sm border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-1.5 dark:bg-gray-900 dark:text-white"
            />
          </div>
        </div>

        <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
          SQL <span class="text-gray-400">— in {{ activeConnectionType }} dialect</span>
        </label>
        <textarea
          v-model="form.definition_sql" data-testid="cq-sql" rows="8" spellcheck="false"
          placeholder="SELECT ..."
          class="w-full text-xs font-mono border border-gray-300 dark:border-gray-700 rounded-md px-2.5 py-2 dark:bg-gray-900 dark:text-white"
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
          <span class="text-[11px] text-gray-400">Preview is limited to {{ ROW_LIMIT }} rows.</span>
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
          <div class="flex items-center flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-600 dark:text-gray-400 mb-1.5">
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
            <table class="min-w-full text-[11px]">
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
      <div v-show="tab === 'settings'">
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
          <span class="text-[11px] text-gray-400">{{ localTimezoneLabel }}</span>
        </div>

        <p v-if="preview?.scan_bytes" data-testid="cq-scan-projection"
           class="mt-3 text-[11px] text-amber-700 dark:text-amber-400">
          At this schedule this query reads
          <b>{{ humanBytes(preview.scan_bytes * refreshesPerDay) }}</b> from the
          source per day ({{ humanBytes(preview.scan_bytes) }} × {{ refreshesPerDay }}).
          Refreshing more often than the data is queried costs more than it saves.
        </p>

        <div v-if="editing && cq" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 space-y-1">
          <div>Last refreshed: <b>{{ cq.last_refreshed_at ? new Date(cq.last_refreshed_at + 'Z').toLocaleString() : 'never' }}</b>
            <span v-if="cq.last_refresh_ms != null"> ({{ cq.last_refresh_ms }} ms)</span></div>
          <div>Rows cached: <b>{{ (cq.no_rows || 0).toLocaleString() }}</b></div>
          <div v-if="cq.next_run_at">Next refresh: <b>{{ new Date(cq.next_run_at).toLocaleString() }}</b></div>
          <div v-if="cq.artifact_bytes">Local size: <b>{{ humanBytes(cq.artifact_bytes) }}</b></div>
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
            Delete custom query
          </button>
        </div>
      </div>

      <!-- ============ RLS ============ -->
      <div v-show="tab === 'rls'">
        <div v-if="!editing" class="text-sm text-gray-500 dark:text-gray-400 py-8 text-center">
          Save the query first — a row policy filters on its result columns.
        </div>
        <div v-else class="flex gap-5">
          <!-- Left: the rule -->
          <div class="w-1/2 space-y-3">
            <label class="flex items-start gap-2 cursor-pointer">
              <input data-testid="cq-rls-enabled" type="checkbox" v-model="rls.enabled" class="mt-0.5" />
              <span>
                <span class="text-xs font-medium text-gray-700 dark:text-gray-200">Filter rows per user</span>
                <span class="block text-[11px] text-gray-500 dark:text-gray-400">
                  The cached copy holds every row. With this on, each person sees
                  only the rows their identity allows.
                </span>
              </span>
            </label>

            <template v-if="rls.enabled">
              <div>
                <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-1">Filter on column</label>
                <select data-testid="cq-rls-column" v-model="rls.column"
                        class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                  <option value="">Choose a column…</option>
                  <option v-for="c in resultColumns" :key="c" :value="c">{{ c }}</option>
                </select>
              </div>

              <div>
                <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-1">Match against</label>
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
                <p v-if="!rlsOptions.attribute_keys.length" class="text-[10px] text-gray-400 mt-1">
                  No profile attributes have synced for this org yet.
                </p>
              </div>

              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="text-[11px] text-gray-500 dark:text-gray-400">Grants</label>
                  <button data-testid="cq-rls-add-grant" class="text-[11px] text-blue-600 hover:underline" @click="addGrant">
                    + Add grant
                  </button>
                </div>
                <p class="text-[10px] text-gray-400 mb-1.5">
                  Give a group or role extra values, or <code>*</code> to see everything.
                </p>
                <div v-for="(g, i) in rls.grants" :key="i" class="flex items-center gap-1.5 mb-1.5">
                  <select :data-testid="`cq-rls-grant-type-${i}`" v-model="g.principal_type"
                          class="border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                    <option value="group">Group</option>
                    <option value="role">Role</option>
                    <option value="user">User</option>
                  </select>
                  <select :data-testid="`cq-rls-grant-principal-${i}`" v-model="g.principal_id"
                          class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                    <option value="">Choose…</option>
                    <option v-for="p in principalsFor(g.principal_type)" :key="p.id" :value="p.id">{{ p.name }}</option>
                  </select>
                  <input :data-testid="`cq-rls-grant-values-${i}`" v-model="g.valuesText" placeholder="EMEA, APAC or *"
                         class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900" />
                  <button :data-testid="`cq-rls-grant-remove-${i}`" class="text-gray-400 hover:text-red-600" @click="rls.grants.splice(i, 1)">
                    <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              <label class="flex items-start gap-2 cursor-pointer">
                <input data-testid="cq-rls-default-deny" type="checkbox" v-model="rls.default_deny" class="mt-0.5" />
                <span class="text-[11px] text-gray-600 dark:text-gray-300">
                  Show no rows when the value can't be resolved
                  <span class="block text-gray-400">
                    Recommended. Turning this off shows <b>every</b> row to anyone the
                    policy can't place.
                  </span>
                </span>
              </label>
            </template>

            <p v-if="rlsError" data-testid="cq-rls-error" class="text-[11px] text-red-600">{{ rlsError }}</p>
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
            <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-1">
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
            <p class="text-[10px] text-gray-400 mt-1">
              Uses the rule above, saved or not.
            </p>

            <p v-if="rlsPreviewError" class="text-[11px] text-red-600 mt-2">{{ rlsPreviewError }}</p>

            <div v-if="rlsPreview" class="mt-3">
              <div data-testid="cq-rls-verdict"
                   class="text-[11px] rounded-md px-2 py-1.5 mb-2"
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
                <table class="min-w-full text-[11px]">
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
                          class="px-2 py-3 text-center text-gray-400">No rows.</td>
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
        <div class="text-[11px] text-gray-400">
          <span v-if="!canSave && tab === 'query'">Run a preview before saving.</span>
        </div>
        <div class="flex items-center gap-2">
          <button class="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:underline" @click="close">Cancel</button>
          <button
            v-if="editing"
            data-testid="cq-refresh-now"
            class="flex items-center gap-1.5 border border-gray-300 dark:border-gray-700 rounded-md px-3 py-1.5 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
            :disabled="refreshingNow" @click="onRefreshNow"
          >
            <Spinner v-if="refreshingNow" class="w-3.5 h-3.5" />
            <UIcon v-else name="heroicons-arrow-path" class="w-3.5 h-3.5" />
            Refresh now
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

// The SQL is written in the source's dialect against its schema, so a preview
// taken on one connection says nothing about another — drop it on switch
// rather than letting a stale result satisfy the save gate.
watch(selectedConnectionId, (next, prev) => {
  if (!prev || next === prev) return
  preview.value = null
  previewError.value = ''
})

const tabs = computed(() => [
  { key: 'query', label: 'Query', disabled: false },
  { key: 'settings', label: 'Settings', disabled: false },
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

// A successful preview is what supplies the column list, so saving without one
// would create a relation with no known shape.
const canSave = computed(() =>
  !!form.name.trim()
  && !!form.definition_sql.trim()
  && !!preview.value
  && !preview.value?.budget_error
)

watch(() => props.modelValue, (open) => {
  if (!open) return
  tab.value = 'query'
  preview.value = null
  previewError.value = ''
  rlsPreviewUserId.value = ''
  loadRlsFromCq()
  selectedConnectionId.value = props.cq?.connection_id || props.connectionId || (availableConnections.value[0]?.id || '')
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
    form.name = ''
    form.description = ''
    form.definition_sql = ''
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
  if (t !== 'rls' || !editing.value) return
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
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/preview`,
      { method: 'POST', body: { definition_sql: form.definition_sql } },
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
      title: editing.value ? 'Custom query updated' : 'Custom query cached',
      description: `${saved.name} — ${(saved.no_rows || 0).toLocaleString()} rows cached locally`,
      color: 'green',
    })
    emit('saved')
    close()
  } finally {
    saving.value = false
  }
}

async function onRefreshNow() {
  refreshingNow.value = true
  try {
    const { data, error } = await useMyFetch(
      `/connections/${activeConnection.value.id}/custom-queries/${props.cq.id}/refresh`,
      { method: 'POST' },
    )
    if (error.value) {
      toast.add({ title: 'Refresh failed', description: error.value?.data?.detail || 'Failed', color: 'red' })
      return
    }
    const r: any = data.value
    toast.add({ title: 'Refreshed', description: `${(r.no_rows || 0).toLocaleString()} rows in ${r.last_refresh_ms} ms`, color: 'green' })
    emit('saved')
  } finally {
    refreshingNow.value = false
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
    toast.add({ title: 'Custom query deleted', color: 'green' })
    emit('deleted')
    close()
  } finally {
    deleting.value = false
  }
}
</script>
