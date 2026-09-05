<template>
  <Teleport to="body" :disabled="!fullscreen || fullscreenInPlace">
  <div @keydown.esc.capture="onEmbeddedEscape" ref="selectorElement" :role="fullscreen && !fullscreenInPlace ? 'dialog' : undefined" :aria-modal="(fullscreen && !fullscreenInPlace) || undefined" :aria-label="fullscreen ? t('tableErd.erd') : undefined" tabindex="-1" class="w-full" :class="fullscreen && !fullscreenInPlace ? 'fixed inset-0 z-40 overflow-auto bg-white dark:bg-gray-900 p-4' : ''">
    <div v-if="showHeader" class="mb-3">
      <h1 class="text-lg font-semibold dark:text-white">{{ headerTitle }}</h1>
      <p class="text-gray-500 dark:text-gray-400 text-sm">{{ headerSubtitle }}</p>
    </div>
    <div class="mb-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2" data-testid="table-view-toolbar">
      <div class="flex items-center gap-4">
        <slot name="reload-left" />
        <div class="flex items-center gap-4" :aria-label="t('tableErd.view')" role="group">
          <button v-for="view in ['table', 'erd'] as const" :key="view" type="button"
            :aria-pressed="tableView === view" @click="setTableView(view)"
            class="py-1.5 text-xs border-b-2 transition-colors"
            :class="tableView === view ? 'border-gray-800 dark:border-gray-200 text-gray-900 dark:text-white font-medium' : 'border-transparent text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'">
            {{ t(`tableErd.${view}`) }}
          </button>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span v-if="hasPendingChanges" class="text-[10px] text-gray-400">{{ t('tableErd.unsaved') }}</span>
        <button v-if="customQueriesEnabled && canAuthorCustomQueries" data-testid="add-custom-query" @click="openNewCustomQuery()"
          class="inline-flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-800 dark:hover:text-gray-200">
          <UIcon name="i-heroicons-plus" class="w-3 h-3" />{{ t('tableErd.customQuery') }}
        </button>
        <NuxtLink v-else-if="!customQueriesEnabled && canAuthorCustomQueries" data-testid="custom-queries-disabled-hint"
          to="/settings/ai_settings" :title="t('tableErd.enableCustomQueries')"
          class="text-[11px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 whitespace-nowrap">
          {{ t('tableErd.customQueriesOff') }}
        </NuxtLink>
        <button v-if="showRefresh" @click="onRefresh" :disabled="loading || refreshing"
          :aria-label="t('tableErd.reload')"
          class="inline-flex items-center gap-1.5 rounded-md border border-gray-200 dark:border-gray-700 px-2.5 py-1.5 text-[11px] text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
          <Spinner v-if="loading || refreshing" class="w-3 h-3" />
          <UIcon v-else name="i-heroicons-arrow-path" class="w-3 h-3" />
          <span v-if="!refreshIconOnly">{{ t('tableErd.reload') }}</span>
        </button>
      </div>
    </div>

    <!-- Kept mounted rather than v-if'd on the connection: creating the
         component with modelValue already true skips UModal's open transition
         and the dialog never appears. -->
    <CustomQueryModal
      v-model="cqModalOpen"
      :connection-id="cqModalConnection?.id || ''"
      :connection-name="cqModalConnection?.name || ''"
      :connection-type="cqModalConnection?.type || ''"
      :connections="manageableConnections"
      :cq="cqEditing"
      @saved="onCustomQuerySaved"
      @deleted="onCustomQuerySaved"
    />

    <!-- Search and filters row -->
    <div>
      <div class="relative flex items-center gap-1.5">
        <input 
          v-model="searchInput" 
          @input="onSearchInput"
          type="text" 
          :placeholder="`Search ${props.itemNoun.plural}...`"
          class="border border-gray-300 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded px-2 py-1.5 w-full h-7 text-xs focus:outline-none focus:border-blue-500"
        />
        
        <!-- Filter button (contains both status and schema filters) -->
        <button
          ref="filterButtonRef"
          type="button"
          @click="toggleFilterMenu"
          class="h-7 w-7 inline-flex items-center justify-center rounded border"
          :class="hasActiveFilters ? 'border-blue-500 bg-blue-50 text-blue-700' : 'border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'"
          :aria-label="`Filter ${props.itemNoun.plural}`"
        >
          <UIcon name="heroicons-funnel" class="w-4 h-4" />
        </button>
        
        <!-- Sort -->
        <button
          ref="sortButtonRef"
          type="button"
          @click="toggleSortMenu"
          class="h-7 w-7 inline-flex items-center justify-center rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50"
          :aria-label="`Sort ${props.itemNoun.plural}`"
        >
          <UIcon name="heroicons-arrows-up-down" class="w-4 h-4" />
        </button>
        
        <!-- Filter menu (multi-level with status and schema) -->
        <div
          v-if="filterMenuOpen"
          ref="filterMenuRef"
          class="absolute end-8 top-full mt-1 z-20 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded shadow-lg w-48"
        >
          <!-- Status filter section. Only a manager sees both states — a reader
               is served the selected tables and nothing else, so filtering by
               selection would offer one no-op and one guaranteed-empty view. -->
          <div v-if="canUpdate" class="py-1 border-b border-gray-100 dark:border-gray-800">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider">Status</div>
            <button
              type="button"
              class="w-full text-start px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 flex items-center justify-between"
              @click="setSelectedFilter('selected')"
            >
              <span>Selected</span>
              <UIcon v-if="filters.selectedState === 'selected'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              type="button"
              class="w-full text-start px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 flex items-center justify-between"
              @click="setSelectedFilter('unselected')"
            >
              <span>Unselected</span>
              <UIcon v-if="filters.selectedState === 'unselected'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
          </div>
          
          <!-- Schema filter section -->
          <div class="py-1">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider flex items-center justify-between">
              <span>Schema</span>
              <button
                v-if="selectedSchemas.length > 0"
                type="button"
                @click.stop="clearSchemaFilter"
                class="text-[9px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400"
              >
                Clear
              </button>
            </div>
            <div v-if="availableSchemas.length === 0" class="px-2 py-1 text-xs text-gray-400 dark:text-gray-500">No schemas</div>
            <div v-else class="max-h-40 overflow-y-auto">
              <template v-for="(group, connName) in groupedSchemas" :key="connName">
                <div v-if="connName !== '_default'" class="px-2 pt-1.5 pb-0.5 text-[9px] font-medium text-gray-400 dark:text-gray-500 truncate">{{ connName }}</div>
                <label
                  v-for="item in group"
                  :key="item.value"
                  class="flex items-center px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer"
                  :class="connName !== '_default' ? 'ps-4' : ''"
                >
                  <input
                    type="checkbox"
                    :checked="selectedSchemas.includes(item.value)"
                    @change="toggleSchemaFilter(item.value)"
                    class="me-1.5 h-3 w-3 rounded border-gray-300 dark:border-gray-700 text-blue-600 focus:ring-blue-500"
                  />
                  <span class="truncate">{{ item.label }}</span>
                </label>
              </template>
            </div>
          </div>

          <!-- Connection filter section -->
          <div v-if="availableConnections.length >= 1" class="py-1 border-t border-gray-100 dark:border-gray-800">
            <div class="px-2 py-1 text-[10px] font-medium text-gray-400 dark:text-gray-500 uppercase tracking-wider flex items-center justify-between">
              <span>Connection</span>
              <button
                v-if="selectedConnections.length > 0"
                type="button"
                @click.stop="clearConnectionFilter"
                class="text-[9px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400"
              >
                Clear
              </button>
            </div>
            <div class="max-h-32 overflow-y-auto">
              <label
                v-for="conn in availableConnections"
                :key="conn.id"
                class="flex items-center px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  :checked="selectedConnections.includes(conn.id)"
                  @change="toggleConnectionFilter(conn.id)"
                  class="me-1.5 h-3 w-3 rounded border-gray-300 dark:border-gray-700 text-blue-600 focus:ring-blue-500"
                />
                <span class="truncate">{{ conn.name }}</span>
                <span class="ms-1 text-[9px] text-gray-400 dark:text-gray-500">({{ conn.type }})</span>
              </label>
            </div>
          </div>

          <!-- Clear all filters -->
          <div v-if="hasActiveFilters" class="border-t border-gray-100 dark:border-gray-800 p-1.5">
            <button
              type="button"
              @click="clearAllFilters"
              class="w-full text-[10px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 py-0.5"
            >
              Clear all filters
            </button>
          </div>
        </div>
        
        <!-- Sort menu -->
        <div
          v-if="sortMenuOpen"
          ref="sortMenuRef"
          class="absolute end-0 top-full mt-1 z-20 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded shadow-lg w-32"
        >
          <div class="py-1">
            <button
              type="button"
              class="w-full text-start px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 flex items-center justify-between"
              @click="setSort('name')"
            >
              <span>Name</span>
              <UIcon v-if="sort.key === 'name'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              v-if="canUpdate"
              type="button"
              class="w-full text-start px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 flex items-center justify-between"
              @click="setSort('is_active')"
            >
              <span>Selected</span>
              <UIcon v-if="sort.key === 'is_active'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
            <button
              v-if="props.showStats"
              type="button"
              class="w-full text-start px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800/50 flex items-center justify-between"
              @click="setSort('usage')"
            >
              <span>Usage</span>
              <UIcon v-if="sort.key === 'usage'" name="heroicons-check" class="w-3 h-3 text-blue-600" />
            </button>
          </div>
        </div>
      </div>
      
      <!-- Stats row -->
      <div class="mt-1 text-[10px] text-gray-500 dark:text-gray-400 flex items-center justify-between">
        <span v-if="isPaginated && hasActiveFilters">
          {{ totalMatching }} matching · Showing {{ paginationStart }}-{{ paginationEnd }}
        </span>
        <span v-else-if="isPaginated && tableView === 'table'">
          Showing {{ paginationStart }}-{{ paginationEnd }} of {{ totalTables }}
        </span>
        <span v-else-if="canUpdate">{{ draftSelectedCount }}/{{ totalTables }} active</span>
        
        <!-- Right side: bulk actions -->
        <div v-if="canUpdate" class="flex items-center gap-2">
          <button
            @click="selectAllMatching"
            :disabled="loading || refreshing || bulkUpdating"
            class="px-2 py-0.5 text-[10px] rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
          >
            <span v-if="bulkUpdating">...</span>
            <span v-else-if="hasActiveFilters">Select all ({{ totalMatching }})</span>
            <span v-else>Select all</span>
          </button>
          <button
            @click="deselectAllMatching"
            :disabled="loading || refreshing || bulkUpdating"
            class="px-2 py-0.5 text-[10px] rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50"
          >
            <span v-if="bulkUpdating">...</span>
            <span v-else-if="hasActiveFilters">Deselect all ({{ totalMatching }})</span>
            <span v-else>Deselect all</span>
          </button>
        </div>
      </div>
      
      <!-- Active count row. A reader only ever sees active tables, so the
           ratio is always N/N — it reads as a stat but carries no information. -->
      <div v-if="canUpdate && (hasActiveFilters || tableView === 'table')" class="text-[10px] text-gray-500 dark:text-gray-400">
        {{ draftSelectedCount }}/{{ totalTables }} active
      </div>
    </div>

    <div v-if="erdOpened" v-show="tableView === 'erd'" class="relative isolate" :aria-busy="!diagramReady && !catalogError">
      <div v-if="!catalogLoaded && !catalogError" class="h-[460px]" />
      <div v-if="!diagramReady && !catalogError" class="absolute inset-0 z-20 flex items-center justify-center rounded-lg border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950" role="status" :aria-label="t('tableErd.loading')" data-testid="erd-loading">
        <Spinner class="w-6 h-6 text-gray-400" aria-hidden="true" />
      </div>
      <div v-if="catalogError" class="py-4 text-xs text-gray-500" role="alert">{{ t('tableErd.loadError') }} <button class="text-blue-600 underline" @click="loadCatalog()">{{ t('tableErd.retry') }}</button></div>
      <TablesCanvas v-if="catalogLoaded" :class="{ 'opacity-0 pointer-events-none': !diagramReady }" @ready="diagramReady = true" :agent-id="dsId" :can-view-prompts="canUpdate" :tables="catalog" :active-ids="canvasActiveIds" :match-ids="canvasMatchIds"
        :filtering="hasActiveFilters" :can-update="canUpdate && !saving" :show-stats="showStats"
        :bottom-inset="fullscreenInPlace ? 120 : 72" :fullscreen="fullscreen" @toggle-fullscreen="toggleFullscreen" :editable-query-ids="editableQueryIds" @toggle="onTableToggle" @edit-query="editQueryTable" />
    </div>

    <div v-show="tableView === 'table'">
    <!-- Loading state -->
    <div v-if="loading" class="text-sm text-gray-500 dark:text-gray-400 py-10 flex items-center justify-center">
      <Spinner class="w-4 h-4 me-2" />
      Loading schema...
    </div>

    <!-- Tables list -->
    <div v-else class="flex-1 flex flex-col h-full">
      <!-- Delegated (OBO) connection, caller not signed in yet: explain instead
           of an unexplained empty list, and offer the sign-in right here. -->
      <div v-if="tables.length === 0 && connectRequiredConn" class="py-8 flex flex-col items-center gap-1.5 text-center">
        <UIcon name="i-heroicons-key" class="w-5 h-5 text-blue-500" />
        <p class="text-sm text-gray-700 dark:text-gray-200">This connection runs with your own {{ connectRequiredConn.name }} credentials.</p>
        <p class="text-xs text-gray-400 dark:text-gray-500">Connect your account to see the {{ props.itemNoun.plural }} you can access.</p>
        <button
          type="button"
          @click="onConnectAccount"
          :disabled="signingIn"
          class="mt-2 inline-flex items-center gap-1.5 bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
        >
          <Spinner v-if="signingIn" class="w-3.5 h-3.5" />
          Connect your account
        </button>
      </div>
      <!-- Just signed in and the catalog is still being built. The sync used to
           run inside the OAuth redirect, so the list was populated (eventually)
           by the time we got here; it is now a background job, so say so and
           reload when it lands instead of showing a bare "none found". -->
      <div v-else-if="tables.length === 0 && catalogSyncing" class="py-8 flex flex-col items-center gap-1.5 text-center">
        <Spinner class="w-4 h-4" />
        <p class="text-sm text-gray-700 dark:text-gray-200">Building your {{ props.itemNoun.plural }} list…</p>
        <p class="text-xs text-gray-400 dark:text-gray-500">
          {{ catalogSyncPhase || 'This runs in the background — it can take a moment on large drives.' }}
        </p>
      </div>
      <div v-else-if="tables.length === 0" class="text-sm text-gray-500 dark:text-gray-400 py-4">No {{ props.itemNoun.plural }} found.</div>
      <div v-else class="flex-1 flex flex-col min-h-full">
        <!-- Admin/owner viewing the canonical catalog without a personal token:
             selection works, but queries need their own sign-in. -->
        <div v-if="connectRequiredConn" class="mt-2 flex items-center justify-between gap-3 rounded-md bg-blue-50 dark:bg-blue-500/10 px-3 py-2">
          <span class="text-xs text-blue-700 dark:text-blue-300">
            You're viewing the full catalog as an admin. Queries run with your own credentials —
            connect your account to see the {{ props.itemNoun.plural }} you can query.
          </span>
          <button
            type="button"
            @click="onConnectAccount"
            :disabled="signingIn"
            class="shrink-0 inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400"
          >
            <Spinner v-if="signingIn" class="w-3 h-3" />
            Connect
          </button>
        </div>
        <div class="flex-1 overflow-y-auto min-h-0 mt-2" :style="{ maxHeight }">
          <ul class="divide-y divide-gray-100 dark:divide-gray-800">
            <li v-for="table in tables" :key="tableKey(table)" class="py-2 px-2" :data-testid="table.custom_query_id ? `cq-row-${table.name}` : undefined">
              <div class="flex items-center">
                <UCheckbox
                  v-if="canUpdate"
                  color="blue"
                  :model-value="isTableActive(tableKey(table))"
                  :disabled="saving"
                  :data-testid="table.custom_query_id ? `cq-toggle-${table.name}` : undefined"
                  @update:model-value="(val: boolean) => onTableToggle(tableKey(table), val)"
                  class="me-3"
                />
                <button type="button" class="flex items-center justify-between text-start flex-1" @click="toggleTableExpand(table)">
                  <div class="flex items-center min-w-0">
                    <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 me-1 text-gray-500 dark:text-gray-400 rtl-flip" />
                    <template v-if="availableConnections.length > 1">
                      <DataSourceIcon :type="table.connection_type" class="h-3.5 me-1 flex-shrink-0" />
                      <span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 me-1.5 flex-shrink-0 truncate max-w-[120px]">{{ table.connection_name || table.connection_type }}</span>
                    </template>
                    <UIcon v-if="table.custom_query_id" name="i-heroicons-bolt" class="w-3.5 h-3.5 me-1 text-gray-400" :title="t('tableErd.customQuery')" />
                    <span class="text-sm text-gray-800 dark:text-gray-200 truncate">{{ table.name }}</span>
                    <span v-if="table.rls_enabled" class="ms-2 text-[10px] text-blue-500">{{ t('tableErd.rowFiltered') }}</span>
                    <span v-if="table.last_refresh_status === 'error'" class="ms-2 text-[10px] text-red-500">{{ t('tableErd.refreshFailed') }}</span>
                    <span v-if="!isTableActive(tableKey(table)) && canUpdate" class="ms-2 text-[10px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">inactive</span>
                    <span v-if="isTableDirty(tableKey(table))" class="ms-1 text-[10px] px-1 py-0.5 rounded bg-yellow-100 text-yellow-700">modified</span>
                  </div>
                  <span v-if="props.showStats && (table.usage_count !== undefined)" class="ms-2 text-[11px] text-gray-500 dark:text-gray-400 whitespace-nowrap flex items-center gap-2">
                    <span>usage {{ table.usage_count }}</span>
                    <UTooltip text="Successful executed queries">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-check-circle" class="w-3 h-3 text-green-600" />
                        <span>{{ table.success_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Failed executed queries">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-x-circle" class="w-3 h-3 text-red-600" />
                        <span>{{ table.failure_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Positive feedback">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-hand-thumb-up" class="w-3 h-3 text-green-600" />
                        <span>{{ table.pos_feedback_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                    <UTooltip text="Negative feedback">
                      <span class="inline-flex items-center gap-1">
                        <UIcon name="heroicons-hand-thumb-down" class="w-3 h-3 text-red-600" />
                        <span>{{ table.neg_feedback_count ?? 0 }}</span>
                      </span>
                    </UTooltip>
                  </span>
                </button>
                <button v-if="table.custom_query_id && editableQueryIds.has(table.custom_query_id)" type="button" @click="editQueryTable(table.custom_query_id)"
                  :aria-label="t('tableErd.editQuery', { name: table.name })" class="ms-2 shrink-0 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
                  <UIcon name="i-heroicons-pencil-square" class="w-3.5 h-3.5" />
                </button>
              </div>
              <div v-if="expandedTables[table.name]" class="mt-2 ms-7">
                <!-- Columns -->
                <div v-if="table.columns?.length" class="border border-gray-100 dark:border-gray-800 rounded">
                  <div class="grid grid-cols-2 text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded-t">
                    <div>Name</div>
                    <div>Type</div>
                  </div>
                  <div class="divide-y divide-gray-100 dark:divide-gray-800">
                    <div v-for="col in table.columns" :key="col.name" class="grid grid-cols-2 text-xs px-2 py-1">
                      <div class="text-gray-700 dark:text-gray-300">{{ col.name }}</div>
                      <div class="text-gray-500 dark:text-gray-400">{{ col.dtype || col.type }}</div>
                    </div>
                  </div>
                </div>

                <!-- Relationships -->
                <div v-if="table.fks?.length" class="mt-2 border border-gray-100 dark:border-gray-800 rounded">
                  <div class="text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded-t">Relationships</div>
                  <div class="divide-y divide-gray-100 dark:divide-gray-800">
                    <div v-for="(fk, idx) in table.fks" :key="idx" class="text-xs px-2 py-1 text-gray-600 dark:text-gray-400">
                      <span class="text-gray-700 dark:text-gray-300">{{ fk.column?.name }}</span>
                      <span class="text-gray-400 dark:text-gray-500 mx-1">→</span>
                      <span class="text-blue-600">{{ fk.references_name }}</span>
                      <span class="text-gray-400 dark:text-gray-500">.</span>
                      <span class="text-gray-700 dark:text-gray-300">{{ fk.references_column?.name }}</span>
                    </div>
                  </div>
                </div>

                <!-- Power BI Metadata -->
                <div v-if="table.metadata_json?.powerbi" class="mt-2 text-xs text-gray-500 dark:text-gray-400 space-y-0.5">
                  <div v-if="table.metadata_json.powerbi.datasetName">
                    <span class="text-gray-400 dark:text-gray-500">Dataset:</span> {{ table.metadata_json.powerbi.datasetName }}
                  </div>
                  <div v-if="table.metadata_json.powerbi.workspaceName">
                    <span class="text-gray-400 dark:text-gray-500">Workspace:</span> {{ table.metadata_json.powerbi.workspaceName }}
                  </div>
                  <div v-if="table.metadata_json.powerbi.reports?.length">
                    <span class="text-gray-400 dark:text-gray-500">Reports:</span> {{ table.metadata_json.powerbi.reports.map((r: any) => r.name).join(', ') }}
                  </div>
                </div>

                <!-- Power BI Report Server Metadata -->
                <div v-if="table.metadata_json?.powerbi_report_server" class="mt-2 space-y-2">
                  <div class="border border-gray-100 dark:border-gray-800 rounded">
                    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded-t">Report details</div>
                    <div class="text-xs text-gray-600 dark:text-gray-400 px-2 py-1 space-y-0.5">
                      <div v-if="table.metadata_json.powerbi_report_server.report_type">
                        <span class="text-gray-400 dark:text-gray-500">Type:</span> {{ table.metadata_json.powerbi_report_server.report_type }}
                      </div>
                      <div v-if="table.metadata_json.powerbi_report_server.path">
                        <span class="text-gray-400 dark:text-gray-500">Path:</span> {{ table.metadata_json.powerbi_report_server.path }}
                      </div>
                      <div v-if="table.metadata_json.powerbi_report_server.modified_by">
                        <span class="text-gray-400 dark:text-gray-500">Modified by:</span> {{ table.metadata_json.powerbi_report_server.modified_by }}
                      </div>
                      <div v-if="table.metadata_json.powerbi_report_server.modified_date">
                        <span class="text-gray-400 dark:text-gray-500">Modified:</span> {{ table.metadata_json.powerbi_report_server.modified_date }}
                      </div>
                      <div>
                        <span class="text-gray-400 dark:text-gray-500">Queryable:</span>
                        <span :class="table.metadata_json.powerbi_report_server.queryable ? 'text-green-600' : 'text-gray-500 dark:text-gray-400'">
                          {{ table.metadata_json.powerbi_report_server.queryable ? 'yes' : 'no (metadata only)' }}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div v-if="table.metadata_json.powerbi_report_server.upstream_source" class="border border-blue-100 bg-blue-50 rounded">
                    <div class="text-xs font-medium text-blue-700 px-2 py-1 rounded-t">Upstream source</div>
                    <div class="text-xs text-blue-900 px-2 py-1 break-all">
                      {{ table.metadata_json.powerbi_report_server.upstream_source }}
                    </div>
                  </div>

                  <div v-if="table.metadata_json.powerbi_report_server.data_sources?.length" class="border border-gray-100 dark:border-gray-800 rounded">
                    <div class="grid grid-cols-[80px_1fr_80px] text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded-t gap-2">
                      <div>Kind</div>
                      <div>Connection</div>
                      <div>Auth</div>
                    </div>
                    <div class="divide-y divide-gray-100 dark:divide-gray-800">
                      <div
                        v-for="(ds, idx) in table.metadata_json.powerbi_report_server.data_sources"
                        :key="idx"
                        class="grid grid-cols-[80px_1fr_80px] text-xs px-2 py-1 gap-2"
                      >
                        <div class="text-gray-700 dark:text-gray-300">{{ ds.kind || ds.type || '—' }}</div>
                        <div class="text-gray-600 dark:text-gray-400 break-all">{{ ds.connection_string || '—' }}</div>
                        <div class="text-gray-500 dark:text-gray-400">{{ ds.auth_type || '—' }}</div>
                      </div>
                    </div>
                  </div>

                  <div v-if="table.metadata_json.powerbi_report_server.parameters?.length" class="text-xs text-gray-600 dark:text-gray-400">
                    <span class="text-gray-400 dark:text-gray-500">Parameters:</span>
                    <span
                      v-for="(p, idx) in table.metadata_json.powerbi_report_server.parameters"
                      :key="idx"
                      class="ms-1 inline-block px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300"
                    >{{ p.name }}</span>
                  </div>

                  <div v-if="table.metadata_json.powerbi_report_server.command_text" class="border border-gray-100 dark:border-gray-800 rounded">
                    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 px-2 py-1 rounded-t">Command text</div>
                    <pre class="text-xs text-gray-700 dark:text-gray-300 px-2 py-1 whitespace-pre-wrap break-all">{{ table.metadata_json.powerbi_report_server.command_text }}</pre>
                  </div>

                  <div v-if="table.metadata_json.powerbi_report_server.query_note" class="border border-yellow-200 bg-yellow-50 rounded text-xs text-yellow-800 px-2 py-1">
                    {{ table.metadata_json.powerbi_report_server.query_note }}
                  </div>
                </div>
              </div>
            </li>
          </ul>
        </div>
        
        <!-- Pagination controls -->
        <div v-if="isPaginated && totalPages > 1" class="mt-3 flex items-center justify-center gap-2">
          <button
            @click="goToPage(1)"
            :disabled="page === 1 || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-double-left" class="w-3 h-3" />
          </button>
          <button
            @click="goToPage(page - 1)"
            :disabled="page === 1 || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-left" class="w-3 h-3" />
          </button>
          <span class="text-xs text-gray-600 dark:text-gray-400 px-2">
            Page {{ page }} of {{ totalPages }}
          </span>
          <button
            @click="goToPage(page + 1)"
            :disabled="page >= totalPages || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-right" class="w-3 h-3" />
          </button>
          <button
            @click="goToPage(totalPages)"
            :disabled="page >= totalPages || loading"
            class="px-2 py-1 text-xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <UIcon name="heroicons-chevron-double-right" class="w-3 h-3" />
          </button>
        </div>
      </div>
    </div>

    </div>

    <!-- Save button -->
    <div v-if="(showSave || (fullscreen && !fullscreenInPlace)) && canUpdate" class="sticky bottom-0 z-10 mt-3 flex items-center justify-start rtl:justify-end border-t border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 py-2">
      <button 
        @click="onSave" 
        :disabled="saving || bulkUpdating || catalogLoading"
        class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50"
      >
        <span v-if="saving">Saving...</span>
        <span v-else>{{ saveLabel }}</span>
      </button>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { useScrollLock, useEventListener } from '@vueuse/core'
import Spinner from '@/components/Spinner.vue'
const TablesCanvas = defineAsyncComponent(() => import('./TablesCanvas.vue'))
import { tableId, matchesTable } from '~/utils/tableGraph'
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import CustomQueryModal from '@/components/datasources/CustomQueryModal.vue'
import { useConnectionSignIn } from '~/composables/useConnectionSignIn'

type Column = { name: string; dtype?: string; type?: string }
type ForeignKey = {
  column?: { name: string; dtype?: string };
  references_name: string;
  references_column?: { name: string; dtype?: string };
}
type Table = {
  id?: string;
  name: string;
  is_active: boolean;
  columns?: Column[];
  pks?: any[];
  fks?: ForeignKey[];
  last_used_at?: string;
  custom_query_id?: string;
  last_refreshed_at?: string;
  last_refresh_status?: string;
  rls_enabled?: boolean;
  usage_count?: number;
  success_count?: number;
  failure_count?: number;
  pos_feedback_count?: number;
  neg_feedback_count?: number;
  metadata_json?: {
    schema?: string;
    powerbi?: {
      datasetId?: string;
      datasetName?: string;
      workspaceId?: string;
      workspaceName?: string;
      tableName?: string;
      reports?: { id: string; name: string; webUrl?: string }[];
    };
    powerbi_report_server?: {
      report_type?: string;
      report_id?: string;
      report_name?: string;
      path?: string;
      parent_folder_id?: string;
      size?: number;
      created_by?: string;
      modified_by?: string;
      modified_date?: string;
      queryable?: boolean;
      upstream_source?: string;
      query_note?: string;
      command_text?: string;
      data_sources?: {
        type?: string;
        kind?: string;
        auth_type?: string;
        connection_string?: string;
        model_connection_name?: string;
      }[];
      parameters?: { name?: string; value_type?: string | null; is_required?: boolean | null; current_value?: string | null }[];
      roles?: { name?: string; model_permissions?: string[] }[];
    };
  };
  connection_id?: string;
  connection_name?: string;
  connection_type?: string;
}

type ConnectionInfo = {
  id: string;
  name: string;
  type: string;
}

type PaginatedResponse = {
  tables: Table[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  schemas: string[];
  connections: ConnectionInfo[];
  selected_count: number;
  total_tables: number;
  has_more: boolean;
}

const props = withDefaults(defineProps<{
  dsId: string;
  fullscreenInPlace?: boolean;
  schema: 'full' | 'user';
  canUpdate?: boolean;
  showRefresh?: boolean;
  refreshIconOnly?: boolean;
  showSave?: boolean;
  saveLabel?: string;
  maxHeight?: string;
  showHeader?: boolean;
  headerTitle?: string;
  headerSubtitle?: string;
  showStats?: boolean;
  pageSize?: number;
  skipRefreshOnSave?: boolean;
  // Restrict the grid to a single connection's tables (comma-separated
  // connection IDs also accepted). Used by the per-connection CatalogSelector
  // so a mixed agent shows each SQL connection's tables in its own section.
  connectionFilter?: string;
  // Noun used in micro-copy ("Reload {plural}", "No {plural} found", etc.).
  // Defaults to tables. For file-shaped data sources (OneDrive, SharePoint,
  // Google Drive) the parent passes {sing: 'file', plural: 'files'}.
  itemNoun?: { sing: string; plural: string };
}>(), {
  canUpdate: true,
  showRefresh: true,
  refreshIconOnly: false,
  showSave: true,
  saveLabel: 'Save',
  maxHeight: '50vh',
  showHeader: false,
  headerTitle: 'Select tables',
  headerSubtitle: 'Choose which tables to enable',
  itemNoun: () => ({ sing: 'table', plural: 'tables' }),
  showStats: false,
  pageSize: 100,
  skipRefreshOnSave: false,
  connectionFilter: '',
})

const emit = defineEmits<{ (e: 'saved', tables: Table[]): void; (e: 'error', err: any): void; (e: 'fullscreen-change', expanded: boolean): void }>()

// Let a parent composite (CatalogSelector) drive save across several
// per-connection grids with one button. onSave is a hoisted declaration below.
defineExpose({ save: () => onSave() })

const { t } = useI18n()
const fullscreen = ref(false)
const selectorElement = ref<HTMLElement | null>(null)
let fullscreenTrigger: HTMLElement | null = null
let appSurface: HTMLElement | null = null
let previousVisibility = ''
let previousInert = false
function restoreAppSurface() {
  if (appSurface) { appSurface.style.visibility = previousVisibility; appSurface.inert = previousInert; appSurface = null }
}
const bodyLocked = useScrollLock(typeof document === 'undefined' ? null : document.body)
function onEmbeddedEscape(event: KeyboardEvent) {
  if (!props.fullscreenInPlace || !fullscreen.value || cqModalOpen.value) return
  if (selectorElement.value?.querySelector('[aria-expanded="true"]')) return
  event.preventDefault()
  event.stopPropagation()
  toggleFullscreen()
}
function toggleFullscreen() {
  if (props.fullscreenInPlace) {
    fullscreen.value = !fullscreen.value
    emit('fullscreen-change', fullscreen.value)
    return
  }
  if (!fullscreen.value) {
    fullscreenTrigger = document.activeElement as HTMLElement
    appSurface = document.getElementById('__nuxt')
    if (appSurface) {
      previousVisibility = appSurface.style.visibility
      previousInert = appSurface.inert
      appSurface.style.visibility = 'hidden'
      appSurface.inert = true
    }
  } else restoreAppSurface()
  fullscreen.value = !fullscreen.value
  bodyLocked.value = fullscreen.value
  nextTick(() => fullscreen.value ? selectorElement.value?.focus() : fullscreenTrigger?.focus())
}
useEventListener('keydown', (event: KeyboardEvent) => {
  if (props.fullscreenInPlace) return
  if (event.key === 'Tab' && fullscreen.value && !props.fullscreenInPlace && !cqModalOpen.value) {
    const controls = [...selectorElement.value?.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], summary, [tabindex="0"]') || []].filter(element => !element.hasAttribute('disabled') && element.getClientRects().length)
    const first = controls[0], last = controls[controls.length - 1]
    if (event.shiftKey && (document.activeElement === first || document.activeElement === selectorElement.value)) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }
  if (event.key === 'Escape' && fullscreen.value && !cqModalOpen.value && !event.defaultPrevented) {
    event.preventDefault()
    toggleFullscreen()
  }
})
onBeforeUnmount(() => { bodyLocked.value = false; restoreAppSurface() })
const toast = useToast()
const route = useRoute()
const { triggerUserSignIn } = useConnectionSignIn()

// Loading states
const loading = ref(false)
const refreshing = ref(false)
const saving = ref(false)
const bulkUpdating = ref(false)

// Delegated-auth awareness: the agent's connections with the caller's
// per-connection auth status (auth_policy / allowed_user_auth_modes /
// user_status). Lets the grid prompt "Connect your account" for OBO
// connections instead of an unexplained empty list.
const authConnections = ref<any[]>([])
const signingIn = ref(false)

const connectRequiredConn = computed(() => {
  return authConnections.value.find((c: any) =>
    c?.auth_policy === 'user_required'
    && Array.isArray(c?.allowed_user_auth_modes)
    && c.allowed_user_auth_modes.length === 1
    && c.allowed_user_auth_modes[0] === 'oauth'
    && !c?.user_status?.has_user_credentials
    && c?.user_status?.effective_auth !== 'system'
  ) || null
})

async function loadAuthConnections() {
  if (!props.dsId) { authConnections.value = []; return }
  try {
    const { data, error } = await useMyFetch(`/data_sources/${props.dsId}/connections`, { method: 'GET' })
    authConnections.value = error.value ? [] : ((data.value as any[]) || [])
  } catch {
    authConnections.value = []
  }
  await loadCustomQueries()
}

// ---- Custom queries (BOW-managed materialized relations) -------------------
// Connection-scoped objects, so they are fetched per connection and only for
// connections the caller can administer. A non-admin simply gets no rows back
// and never sees the Add Custom button.

const customQueries = ref<any[]>([])
const cqModalOpen = ref(false)
const cqEditing = ref<any>(null)
const cqModalConnection = ref<any>(null)

const accelerableConnections = computed(() =>
  authConnections.value.filter((c: any) => c?.custom_queries_supported)
)

// Authoring a custom query (and its RLS policy) is a CONNECTION-admin act: it
// runs SQL with the connection's own credential and decides what every agent
// on that connection can read. Managing the *agent* is a different, lesser
// right — it lets you activate an existing relation for this agent, which is
// the checkbox below. The backend enforces both separately
// (`manage_connection` on all nine custom-query routes, `data_source:manage`
// on activation); this mirrors it so an agent manager isn't shown a button
// that can only 403.
const manageableConnections = computed(() =>
  accelerableConnections.value.filter(
    (c: any) => useCan('manage_connection', { type: 'connection', id: String(c.id) }))
)
const canAuthorCustomQueries = computed(() => manageableConnections.value.length > 0)

// Per relation, not per page: a mixed agent can hold connection-admin on one
// of its connections and not another.
function canEditCustomQuery(cq: any): boolean {
  return useCan('manage_connection', { type: 'connection', id: String(cq?.connection_id || '') })
}

const { isCustomQueriesEnabled: customQueriesEnabled } = useOrgSettings()

async function loadCustomQueries() {
  const conns = accelerableConnections.value
  if (!conns.length) { customQueries.value = []; return }
  const all: any[] = []
  for (const c of conns) {
    try {
      const { data, error } = await useMyFetch(`/connections/${c.id}/custom-queries`, { method: 'GET' })
      if (!error.value && Array.isArray(data.value)) {
        for (const q of data.value as any[]) {
          all.push({ ...q, _connection: c })
        }
      }
    } catch { /* a connection the caller cannot administer simply contributes none */ }
  }
  customQueries.value = all
}

const editableQueryIds = computed(() => new Set(customQueries.value.filter(canEditCustomQuery).map(query => query.id)))
async function editQueryTable(id: string) {
  const query = customQueries.value.find(query => query.id === id)
  if (query && canEditCustomQuery(query)) await openEditCustomQuery(query)
}

async function openNewCustomQuery() {
  cqModalConnection.value = manageableConnections.value[0] || null
  cqEditing.value = null
  if (!cqModalConnection.value) return
  await nextTick()
  cqModalOpen.value = true
}

async function openEditCustomQuery(cq: any) {
  cqModalConnection.value = cq._connection
  cqEditing.value = cq
  await nextTick()
  cqModalOpen.value = true
}

async function onCustomQuerySaved() {
  await loadCustomQueries()
  // Refresh cached metadata; activation remains in the shared table draft.
  await reloadKnownCatalog()
}

async function onConnectAccount() {
  const conn = connectRequiredConn.value
  if (!conn || signingIn.value) return
  signingIn.value = true
  try {
    const res = await triggerUserSignIn(conn, { returnTo: route.fullPath })
    if (!res.redirecting) {
      toast.add({ title: 'Sign-in failed', description: res.error || 'Could not start the sign-in flow', color: 'red' })
      signingIn.value = false
    }
    // On success the page navigates away to the provider — leave the spinner on.
  } catch (e: any) {
    toast.add({ title: 'Sign-in failed', description: e?.message || String(e), color: 'red' })
    signingIn.value = false
  }
}

// Post-sign-in catalog build. The OAuth callback returns immediately now and
// builds the user's catalog in a background job (`indexing=1` on the redirect),
// so landing back here can find an empty list that is about to fill. Follow the
// job to its end and reload once, rather than reporting "none found".
const catalogSyncing = ref(false)
const catalogSyncPhase = ref('')
let catalogSyncCancelled = false

async function followCatalogSyncAfterSignIn() {
  if (route.query.oauth !== 'success' || route.query.indexing !== '1') return
  const connectionId = route.query.connection_id as string | undefined
  if (!connectionId) return

  catalogSyncing.value = true
  const deadline = Date.now() + 5 * 60 * 1000
  try {
    while (!catalogSyncCancelled && Date.now() < deadline) {
      const { data, error } = await useMyFetch(
        `/connections/${connectionId}/indexing?scope=user`, { method: 'GET' },
      )
      const row = error.value ? null : (data.value as any)
      if (row?.phase) {
        const total = row.progress_total || 0
        catalogSyncPhase.value = total
          ? `${row.phase} ${row.progress_done || 0}/${total}`
          : row.phase
      }
      if (row && !['pending', 'running'].includes(String(row.status))) break
      await new Promise((r) => setTimeout(r, 1500))
    }
  } finally {
    catalogSyncing.value = false
    catalogSyncPhase.value = ''
  }
  if (!catalogSyncCancelled) {
    await reloadKnownCatalog()
    await loadAuthConnections()
  }
}

// Data
const tables = ref<Table[]>([])
const expandedTables = ref<Record<string, boolean>>({})

// Pagination state
const isPaginated = ref(false)
const page = ref(1)
const totalPages = ref(1)
const totalMatching = ref(0)
const totalTables = ref(0)
const selectedCount = ref(0)
const availableSchemas = ref<string[]>([])
const availableConnections = ref<ConnectionInfo[]>([])
const selectedConnections = ref<string[]>([])

// Filter state
const searchInput = ref('')
const searchDebounced = ref('')
const selectedSchemas = ref<string[]>([])
const filters = ref<{ selectedState: 'selected' | 'unselected' | null }>({
  selectedState: null,
})
// Managers lead with the selected tables. A reader is served active rows only,
// so sorting by is_active would tie every row and leave the paginated order at
// the database's discretion — rows could then repeat or vanish between pages.
const sort = reactive<{ key: 'name' | 'is_active' | 'usage' | null; direction: 'asc' | 'desc' }>(
  props.canUpdate ? { key: 'is_active', direction: 'desc' } : { key: 'name', direction: 'asc' }
)

// Dirty tracking - track changes from original state
const originalActiveState = ref<Map<string, boolean>>(new Map())
const currentActiveState = ref<Map<string, boolean>>(new Map())

// The catalog is loaded only for ERD or bulk selection. It uses the same
// permission-scoped endpoint as the list, across every page.
const tableView = ref<'table' | 'erd'>('table')
const erdOpened = ref(false)
const diagramReady = ref(false)
const catalog = ref<Table[]>([])
const catalogLoaded = ref(false)
const catalogLoading = ref(false)
const catalogError = ref(false)
let catalogGeneration = 0
let catalogRequest: Promise<boolean> | null = null
let listGeneration = 0
const discoveryFilter = computed(() => ({
  search: searchDebounced.value,
  schema: selectedSchemas.value,
  connection: selectedConnections.value.length ? selectedConnections.value : props.connectionFilter.split(',').filter(Boolean),
  selected_state: filters.value.selectedState,
}))
const catalogMatches = computed(() => catalog.value.filter(table => matchesTable(table, discoveryFilter.value, isTableActive(tableKey(table)))))
const canvasActiveIds = computed(() => new Set(catalog.value.filter(table => isTableActive(tableKey(table))).map(tableId)))
const canvasMatchIds = computed(() => new Set(catalogMatches.value.map(tableId)))
const draftSelectedCount = computed(() => catalogLoaded.value ? canvasActiveIds.value.size : selectedCount.value + [...currentActiveState.value].reduce((n, [key, value]) => n + Number(value) - Number(originalActiveState.value.get(key)), 0))

function registerTables(rows: Table[]) {
  for (const table of rows) {
    const key = tableKey(table)
    // Refresh clean rows while keeping unsaved choices made in either view.
    if (!currentActiveState.value.has(key) || currentActiveState.value.get(key) === originalActiveState.value.get(key)) {
      originalActiveState.value.set(key, table.is_active)
      currentActiveState.value.set(key, table.is_active)
    }
  }
}
function renderCatalogPage() {
  if (!catalogLoaded.value) return
  const rows = [...catalogMatches.value].sort((a, b) => {
    const key = sort.key
    const av = key === 'is_active' ? Number(isTableActive(tableKey(a))) : key === 'usage' ? a.usage_count ?? -1 : a.name
    const bv = key === 'is_active' ? Number(isTableActive(tableKey(b))) : key === 'usage' ? b.usage_count ?? -1 : b.name
    const order = typeof av === 'string' ? av.localeCompare(String(bv)) : Number(av) - Number(bv)
    return order * (sort.direction === 'desc' ? -1 : 1) || tableKey(a).localeCompare(tableKey(b))
  })
  isPaginated.value = true
  totalMatching.value = rows.length
  totalTables.value = catalog.value.length
  totalPages.value = Math.max(1, Math.ceil(rows.length / props.pageSize))
  page.value = Math.min(page.value, totalPages.value)
  tables.value = rows.slice((page.value - 1) * props.pageSize, page.value * props.pageSize)
}
watch([catalogMatches, sort, page], renderCatalogPage, { deep: true })
async function setTableView(view: 'table' | 'erd') {
  tableView.value = view
  if (view === 'erd') { erdOpened.value = true; await loadCatalog() }
}
function invalidateCatalog() {
  diagramReady.value = false
  catalogGeneration++
  catalogRequest = null
  catalogLoaded.value = false
  catalogLoading.value = false
  catalog.value = []
}
async function reloadKnownCatalog() {
  const reloadGraph = catalogLoaded.value || erdOpened.value
  invalidateCatalog()
  await fetchTables()
  if (reloadGraph) await loadCatalog()
}
async function loadCatalog(): Promise<boolean> {
  if (catalogLoaded.value) return true
  if (catalogRequest) return catalogRequest
  const generation = ++catalogGeneration
  // Ignore any older list fetch once the complete catalog is authoritative.
  listGeneration++
  loading.value = false
  catalogLoading.value = true
  catalogError.value = false
  const dsId = props.dsId
  const endpoint = endpointForSchema()
  catalogRequest = (async () => {
    try {
      const rows: Table[] = []
      let nextPage = 1
      let more = true
      while (more) {
        const params = new URLSearchParams({ page: String(nextPage), page_size: '500', sort_by: 'name', sort_dir: 'asc' })
        if (props.connectionFilter) params.set('connection_filter', props.connectionFilter)
        if (props.showStats) params.set('with_stats', 'true')
        const res: any = await useMyFetch(`/data_sources/${dsId}/${endpoint}?${params}`, { method: 'GET' })
        if (generation !== catalogGeneration) return false
        if (res.status?.value !== 'success') throw new Error('catalog')
        const data = res.data.value
        const batch = Array.isArray(data) ? data : data.tables
        rows.push(...batch)
        more = !Array.isArray(data) && data.has_more
        if (more && !batch.length) throw new Error('empty catalog page')
        if (!Array.isArray(data)) {
          availableSchemas.value = data.schemas
          availableConnections.value = data.connections.filter((connection: { id: string }) => !props.connectionFilter || props.connectionFilter.split(',').includes(connection.id))
        }
        nextPage++
      }
      const scope = props.connectionFilter.split(',').filter(Boolean)
      catalog.value = [...new Map(rows.filter(row => !scope.length || scope.includes(row.connection_id || '')).map(row => [tableKey(row), row])).values()]
      registerTables(catalog.value)
      catalogLoaded.value = true
      renderCatalogPage()
      return true
    } catch {
      if (generation === catalogGeneration) catalogError.value = true
      return false
    } finally {
      if (generation === catalogGeneration) { catalogLoading.value = false; catalogRequest = null }
    }
  })()
  return catalogRequest
}

// Menu state
const filterMenuOpen = ref(false)
const filterMenuRef = ref<HTMLElement | null>(null)
const filterButtonRef = ref<HTMLElement | null>(null)
const sortMenuOpen = ref(false)
const sortMenuRef = ref<HTMLElement | null>(null)
const sortButtonRef = ref<HTMLElement | null>(null)

// Search debounce
let searchTimeout: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
  if (searchTimeout) clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    searchDebounced.value = searchInput.value
    page.value = 1
    fetchTables()
  }, 300)
}

// Computed
const paginationStart = computed(() => ((page.value - 1) * props.pageSize) + 1)
const paginationEnd = computed(() => Math.min(page.value * props.pageSize, totalMatching.value))

// Group schemas by connection prefix for display
// "conn:schema" → grouped under conn header; plain "schema" → under _default
const groupedSchemas = computed(() => {
  const groups: Record<string, { value: string; label: string }[]> = {}
  for (const s of availableSchemas.value) {
    const colonIdx = s.indexOf(':')
    if (colonIdx > 0) {
      const connName = s.substring(0, colonIdx)
      const schemaName = s.substring(colonIdx + 1)
      if (!groups[connName]) groups[connName] = []
      groups[connName].push({ value: s, label: schemaName })
    } else {
      if (!groups['_default']) groups['_default'] = []
      groups['_default'].push({ value: s, label: s })
    }
  }
  return groups
})

const hasActiveFilters = computed(() => {
  return searchDebounced.value.trim() !== '' || selectedSchemas.value.length > 0 || selectedConnections.value.length > 0 || filters.value.selectedState !== null
})

const hasPendingChanges = computed(() => {
  for (const [name, currentVal] of currentActiveState.value) {
    const originalVal = originalActiveState.value.get(name)
    if (originalVal !== currentVal) return true
  }
  return false
})

// Helper functions
function tableKey(table: Table): string {
  return tableId(table)
}

function isTableActive(key: string): boolean {
  return currentActiveState.value.get(key) ?? false
}

function isTableDirty(key: string): boolean {
  const original = originalActiveState.value.get(key)
  const current = currentActiveState.value.get(key)
  return original !== current
}

function onTableToggle(key: string, newValue: boolean) {
  if (!props.canUpdate || saving.value) return
  currentActiveState.value.set(key, newValue)
}

function endpointForSchema(): string {
  return props.schema === 'user' ? 'schema' : 'full_schema'
}

// Menu toggles
function toggleFilterMenu() {
  filterMenuOpen.value = !filterMenuOpen.value
  sortMenuOpen.value = false
}

function toggleSortMenu() {
  sortMenuOpen.value = !sortMenuOpen.value
  filterMenuOpen.value = false
}

function setSelectedFilter(state: 'selected' | 'unselected') {
  filters.value.selectedState = filters.value.selectedState === state ? null : state
  page.value = 1
  fetchTables()
}

function setSort(key: 'name' | 'is_active' | 'usage') {
  if (sort.key === key) {
    sort.direction = sort.direction === 'asc' ? 'desc' : 'asc'
  } else {
    sort.key = key
    sort.direction = key === 'name' ? 'asc' : 'desc'
  }
  sortMenuOpen.value = false
  page.value = 1
  fetchTables()
}

function toggleSchemaFilter(schema: string) {
  const idx = selectedSchemas.value.indexOf(schema)
  if (idx >= 0) {
    selectedSchemas.value.splice(idx, 1)
  } else {
    selectedSchemas.value.push(schema)
  }
  page.value = 1
  fetchTables()
}

function clearSchemaFilter() {
  selectedSchemas.value = []
  page.value = 1
  fetchTables()
}

function toggleConnectionFilter(connectionId: string) {
  const idx = selectedConnections.value.indexOf(connectionId)
  if (idx >= 0) {
    selectedConnections.value.splice(idx, 1)
  } else {
    selectedConnections.value.push(connectionId)
  }
  page.value = 1
  fetchTables()
}

function clearConnectionFilter() {
  selectedConnections.value = []
  page.value = 1
  fetchTables()
}

function clearAllFilters() {
  filters.value.selectedState = null
  selectedSchemas.value = []
  selectedConnections.value = []
  filterMenuOpen.value = false
  page.value = 1
  fetchTables()
}

function onGlobalClick(e: MouseEvent) {
  const target = e.target as Node
  if (filterMenuOpen.value) {
    const inside = (filterMenuRef.value?.contains(target)) || (filterButtonRef.value?.contains(target))
    if (!inside) filterMenuOpen.value = false
  }
  if (sortMenuOpen.value) {
    const inside = (sortMenuRef.value?.contains(target)) || (sortButtonRef.value?.contains(target))
    if (!inside) sortMenuOpen.value = false
  }
}

// Data fetching
async function fetchTables() {
  if (catalogLoaded.value) { renderCatalogPage(); return }
  const generation = ++listGeneration
  loading.value = true
  try {
    const endpoint = endpointForSchema()
    
    // For full_schema, use paginated endpoint
    if (props.schema === 'full') {
      const params = new URLSearchParams()
      params.set('page', String(page.value))
      params.set('page_size', String(props.pageSize))
      if (searchDebounced.value.trim()) {
        params.set('search', searchDebounced.value.trim())
      }
      if (selectedSchemas.value.length > 0) {
        params.set('schema_filter', selectedSchemas.value.join(','))
      }
      // User filters narrow the connection scope of the combined selector.
      const connFilter = discoveryFilter.value.connection.join(',')
      if (connFilter) {
        params.set('connection_filter', connFilter)
      }
      if (sort.key) {
        // Map frontend sort keys to backend
        let sortBy = sort.key
        if (sort.key === 'usage') sortBy = 'centrality_score' // or usage_count if available
        params.set('sort_by', sortBy)
        params.set('sort_dir', sort.direction)
      }
      if (filters.value.selectedState) {
        params.set('selected_state', filters.value.selectedState)
      }
      if (props.showStats) {
        params.set('with_stats', 'true')
      }

      const res = await useMyFetch(`/data_sources/${props.dsId}/${endpoint}?${params.toString()}`, { method: 'GET' })
      
      if (generation !== listGeneration || catalogLoaded.value) return
      if ((res as any)?.status?.value === 'success') {
        const data = (res as any).data?.value
        
        // Check if paginated response
        if (data && typeof data === 'object' && 'tables' in data) {
          const paginatedData = data as PaginatedResponse
          isPaginated.value = true
          tables.value = paginatedData.tables
          totalMatching.value = paginatedData.total
          totalPages.value = paginatedData.total_pages
          selectedCount.value = paginatedData.selected_count
          // When scoped to one connection, "of N" / "N active" should reflect
          // that connection's own total, not the whole data source.
          totalTables.value = (props.connectionFilter || '').trim()
            ? paginatedData.total
            : paginatedData.total_tables
          
          // Update available schemas (only on first load or refresh)
          if (paginatedData.schemas && paginatedData.schemas.length > 0) {
            availableSchemas.value = paginatedData.schemas
          }
          // Update available connections
          if (paginatedData.connections && paginatedData.connections.length > 0) {
            availableConnections.value = paginatedData.connections.filter(connection => !props.connectionFilter || props.connectionFilter.split(',').includes(connection.id))
          }
          
          registerTables(paginatedData.tables)
        } else if (Array.isArray(data)) {
          // Legacy list response
          isPaginated.value = false
          tables.value = data as Table[]
          totalMatching.value = tables.value.length
          totalTables.value = tables.value.length
          selectedCount.value = tables.value.filter(t => t.is_active).length
          totalPages.value = 1
          
          // Extract schemas from metadata_json
          const schemas = new Set<string>()
          for (const t of tables.value) {
            const s = t.metadata_json?.schema
            if (s) schemas.add(s)
          }
          availableSchemas.value = Array.from(schemas).sort()
          
          registerTables(tables.value)
        }
      } else {
        tables.value = []
      }
    } else {
      // User schema - non-paginated
      const url = `/data_sources/${props.dsId}/${endpoint}${props.showStats ? '?with_stats=true' : ''}`
      const res = await useMyFetch(url, { method: 'GET' })

      if (generation !== listGeneration || catalogLoaded.value) return
      if ((res as any)?.status?.value === 'success') {
        isPaginated.value = false
        tables.value = ((res as any).data?.value || []) as Table[]
        totalMatching.value = tables.value.length
        totalTables.value = tables.value.length
        selectedCount.value = tables.value.filter(t => t.is_active).length
        totalPages.value = 1

        registerTables(tables.value)
      } else {
        tables.value = []
      }
    }
  } catch (e) {
    emit('error', e)
  } finally {
    if (generation === listGeneration) loading.value = false
    refreshing.value = false
  }
}

function goToPage(newPage: number) {
  if (newPage < 1 || newPage > totalPages.value) return
  page.value = newPage
  fetchTables()
}

function toggleTableExpand(table: Table) {
  expandedTables.value[table.name] = !expandedTables.value[table.name]
}

// Resolve the same discovery filters against the complete permission-scoped
// catalog, then save one delta. Bulk selection and individual overrides share
// the exact same draft, including rows never visited in the list.
async function setAllMatching(active: boolean) {
  if (bulkUpdating.value) return
  bulkUpdating.value = true
  const filter = JSON.parse(JSON.stringify(discoveryFilter.value))
  try {
    if (!await loadCatalog()) return
    const matching = catalog.value.filter(table => matchesTable(table, filter, isTableActive(tableKey(table))))
    for (const table of matching) onTableToggle(tableKey(table), active)
  } finally { bulkUpdating.value = false }
}
function selectAllMatching() { return setAllMatching(true) }
function deselectAllMatching() { return setAllMatching(false) }

// Both views save the same effective draft through the existing delta endpoint.
async function onSave() {
  if (saving.value || bulkUpdating.value || catalogLoading.value) return false
  if (!hasPendingChanges.value) { emit('saved', tables.value); return true }
  saving.value = true
  
  try {
    // Persist the effective draft once, including bulk actions and overrides.
    const toActivate: string[] = []
    const toDeactivate: string[] = []
    const knownTables = new Map([...tables.value, ...catalog.value].map(table => [tableKey(table), table]))

    for (const [key, currentVal] of currentActiveState.value) {
      const originalVal = originalActiveState.value.get(key)
      if (originalVal !== currentVal) {
        if (currentVal) {
          toActivate.push(knownTables.get(key)?.id || knownTables.get(key)?.name || key)
        } else {
          toDeactivate.push(knownTables.get(key)?.id || knownTables.get(key)?.name || key)
        }
      }
    }

    if (toActivate.length > 0 || toDeactivate.length > 0) {
      const result: any = await useMyFetch(`/data_sources/${props.dsId}/update_tables_status`, {
        method: 'PUT',
        body: {
          activate: toActivate,
          deactivate: toDeactivate
        }
      })
      if (result.status?.value !== 'success') throw new Error(t('tableErd.saveError'))
    }
    
    // Commit the local baseline before optional refresh, including onboarding.
    const savedRows = catalogLoaded.value ? catalog.value : tables.value
    selectedCount.value = draftSelectedCount.value
    for (const table of savedRows) table.is_active = isTableActive(tableKey(table))
    // Refresh the baseline without losing selection in parent-owned Save flows.
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    registerTables(savedRows)
    if (!props.skipRefreshOnSave) {
      await fetchTables()
    }

    toast.add({
      title: 'Tables updated',
      description: 'Table selection saved successfully',
      color: 'green'
    })
    emit('saved', tables.value)
    return true
  } catch (e: any) {
    const errorMsg = e?.message || 'Failed to save table selection'
    toast.add({
      title: 'Save failed',
      description: errorMsg,
      color: 'red'
    })
    emit('error', e)
    return false
  } finally {
    saving.value = false
  }
}

async function onRefresh() {
  if (loading.value || refreshing.value) return
  refreshing.value = true

  try {
    if (endpointForSchema() === 'full_schema') {
      const res = await useMyFetch(`/data_sources/${props.dsId}/refresh_schema`, { method: 'GET' })
      if (res.error?.value) {
        // Surface the real reason (e.g. 403 "Connect required: this connection
        // runs queries with your own credentials…") — a silent no-op here left
        // users staring at an empty list with no explanation.
        const err: any = res.error.value
        const detail = err?.data?.detail || err?.message || 'Failed to reload'
        toast.add({ title: `Reload ${props.itemNoun.plural} failed`, description: String(detail), color: 'red' })
      }
    }

    invalidateCatalog()
    // Clear all tracking on refresh
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    selectedConnections.value = []
    page.value = 1

    await fetchTables()
    await loadAuthConnections()
    if (tableView.value === 'erd') await loadCatalog()
  } catch (e: any) {
    toast.add({ title: `Reload ${props.itemNoun.plural} failed`, description: e?.message || String(e), color: 'red' })
  } finally {
    refreshing.value = false
  }
}

// Lifecycle
watch(() => [props.dsId, props.schema, props.connectionFilter, props.canUpdate], () => {
  if (props.dsId) {
    invalidateCatalog()
    tableView.value = 'table'
    erdOpened.value = false
    // Reset all state on datasource change
    page.value = 1
    searchInput.value = ''
    searchDebounced.value = ''
    selectedSchemas.value = []
    selectedConnections.value = []
    filters.value.selectedState = null
    originalActiveState.value.clear()
    currentActiveState.value.clear()
    fetchTables()
    loadAuthConnections()
  }
}, { immediate: true })

onMounted(() => {
  document.addEventListener('click', onGlobalClick)
  followCatalogSyncAfterSignIn()
})

onBeforeUnmount(() => {
  catalogGeneration++
  listGeneration++
  document.removeEventListener('click', onGlobalClick)
  if (searchTimeout) clearTimeout(searchTimeout)
  catalogSyncCancelled = true
})
</script>

<style scoped>
</style>
