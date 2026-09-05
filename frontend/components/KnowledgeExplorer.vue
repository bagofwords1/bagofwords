<template>
  <div class="flex flex-col text-sm ke-viewport" :style="{ '--ke-banner': showTopBanner ? bannerHeight : '0px' }">
    <!-- Header -->
    <!-- flex-wrap + basis-60: on narrow (mobile) widths the actions drop to
         their own row instead of crushing the title/subtitle column. -->
    <div class="flex flex-wrap items-center gap-x-3 gap-y-2 ps-3 pe-4 py-3 shrink-0">
      <div class="min-w-0 grow basis-60">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-white">{{ $t('agentsPage.title') }}</h1>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ $t('agentsPage.subtitle') }}</p>
      </div>
      <div class="flex flex-wrap items-center gap-2.5 ms-auto">
        <button v-if="canApprove && pendingCount > 0" class="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border text-xs font-medium whitespace-nowrap transition-colors" :class="pendingView ? 'border-amber-300 dark:border-amber-500/50 bg-amber-100 dark:bg-amber-500/20 text-amber-800 dark:text-amber-300' : 'border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 hover:bg-amber-100 dark:hover:bg-amber-500/20'" :title="pendingView ? $t('agentsPage.pendingChangesExit') : $t('agentsPage.pendingChangesHint')" @click="pendingView ? exitPendingView() : enterPendingView()">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>{{ $t('agentsPage.pendingChangesCount', { n: pendingCount }) }}
          <UIcon v-if="pendingView" name="i-heroicons-x-mark" class="w-3.5 h-3.5 opacity-70" />
        </button>
        <GitConnectionButton v-if="canManageGit" :has-connection="gitRepos.length > 0" :connected-repos="gitRepos" :last-indexed-at="gitLastIndexed" @click="showGitModal = true" />
        <button
          class="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-xs font-medium whitespace-nowrap text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors"
          @click="openAllInstructions()"
        >
          {{ $t('allInstructions.title') }}
          <span class="font-mono tabular-nums text-gray-500 dark:text-gray-400">{{ totalInstructionCount }}</span>
        </button>
        <UPopover v-if="canCreateInstruction || canCreateAgent" :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-lg' }">
          <button class="inline-flex items-center gap-1.5 h-8 ps-2.5 pe-2 rounded-lg bg-blue-600 text-white text-xs font-medium whitespace-nowrap hover:bg-blue-700 transition-colors">
            <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" /> {{ $t('agentsPage.new') }}
            <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 opacity-70" />
          </button>
          <template #panel="{ close }">
            <div class="p-1 w-52">
              <button v-if="canCreateInstruction" class="w-full flex items-start gap-2.5 px-2 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start" @click="openCreate(); close()">
                <UIcon name="i-heroicons-document-text" class="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 shrink-0" />
                <span><span class="block text-xs font-medium text-gray-800 dark:text-gray-200">{{ $t('agentsPage.newInstruction') }}</span><span class="block text-[10px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.newInstructionDesc') }}</span></span>
              </button>
              <button v-if="canCreateAgent" class="w-full flex items-start gap-2.5 px-2 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start" @click="showNewAgent = true; close()">
                <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-400 dark:text-gray-500 mt-0.5 shrink-0" />
                <span><span class="block text-xs font-medium text-gray-800 dark:text-gray-200">{{ $t('agentsPage.newAgent') }}</span><span class="block text-[10px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.newAgentDesc') }}</span></span>
              </button>
            </div>
          </template>
        </UPopover>
      </div>
    </div>

    <!-- Body: tree → detail → versions -->
    <div class="relative flex-1 min-h-0 flex border-t border-gray-200 dark:border-gray-800">
      <!-- ── Pane 1: Tree ───────────────────────────────── -->
      <!-- Desktop: fixed-width resizable pane. Mobile: full-width, and hidden
           once a detail is open (single-column master → detail). -->
      <aside
        class="border-e border-gray-200 dark:border-gray-800 flex flex-col relative"
        :class="isMobile ? (detailOpen ? 'hidden' : 'w-full') : 'shrink-0'"
        :style="isMobile ? {} : { width: treeWidth + 'px' }">
        <div class="px-2 pt-2.5 pb-2 flex items-center gap-1.5">
          <div class="relative flex-1">
            <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />
            <input v-model="search" type="text" :placeholder="$t('agentsPage.searchPlaceholder')" class="w-full h-9 ps-8 pe-2 text-[13px] bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 focus:bg-white dark:focus:bg-gray-800 placeholder:text-gray-400 dark:placeholder:text-gray-500" />
          </div>
          <UPopover :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-md' }">
            <button type="button" class="relative h-8 w-8 flex items-center justify-center rounded-md border border-gray-200 dark:border-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50" :title="$t('agentsPage.filters')">
              <UIcon name="i-heroicons-adjustments-horizontal" class="w-4 h-4" />
              <span v-if="activeFilterCount" class="absolute -top-1 -end-1 w-3.5 h-3.5 rounded-full bg-gray-900 dark:bg-gray-700 text-white text-[8px] font-semibold flex items-center justify-center">{{ activeFilterCount }}</span>
            </button>
            <template #panel="{ close }">
              <div class="p-3 w-56 space-y-3">
                <FilterSection :label="$t('agentsPage.filterStatus')" :options="statusOpts" v-model="fStatus" />
                <FilterSection :label="$t('agentsPage.filterLoading')" :options="loadOpts" v-model="fLoad" />
                <FilterSection :label="$t('agentsPage.filterSource')" :options="sourceOpts" v-model="fSource" />
                <FilterSection v-if="categoryOpts.length" :label="$t('agentsPage.filterCategory')" :options="categoryOpts" v-model="fCategory" />
                <div class="flex items-center justify-between pt-1 border-t border-gray-100 dark:border-gray-800">
                  <button class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200" @click="clearFilters">{{ $t('agentsPage.clearAll') }}</button>
                  <button class="text-[11px] font-medium text-gray-900 dark:text-white" @click="close && close()">{{ $t('agentsPage.done') }}</button>
                </div>
              </div>
            </template>
          </UPopover>
        </div>

        <!-- "Pending changes" view: flat list of instructions with a live pending
             change, grouped by agent. Reuses the InstrLeaf row + the same
             openInstruction() click as the tree. Computed server-side (cheap,
             access-scoped) so we don't lazy-load every agent. -->
        <div v-if="pendingView" class="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-2">
          <div class="px-2 pt-1 pb-1 flex items-center justify-between">
            <span class="text-[11px] font-semibold uppercase tracking-wider text-amber-600 dark:text-amber-400">{{ $t('agentsPage.pendingChanges') }}</span>
            <button type="button" class="text-[11px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 inline-flex items-center gap-0.5" @click="exitPendingView()">
              <UIcon name="i-heroicons-arrow-uturn-left" class="w-3 h-3 rtl:scale-x-[-1]" />{{ $t('agentsPage.pendingChangesBack') }}
            </button>
          </div>
          <div v-if="pendingLoading" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500 px-2"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>
          <template v-else>
            <div v-for="grp in pendingGroups" :key="grp.id">
              <div class="px-2 py-1 flex items-center gap-1.5 text-[11px] font-semibold text-gray-500 dark:text-gray-400">
                <DataSourceIcon v-if="grp.type || grp.icon" :type="grp.type" :connector-key="grp.connector_key" :icon="grp.icon" class="w-3.5 h-3.5 shrink-0" />
                <UIcon v-else name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
                <span class="flex-1 truncate">{{ grp.name }}</span>
                <span class="text-gray-400 dark:text-gray-500 tabular-nums">{{ grp.rows.length }}</span>
              </div>
              <div v-for="ins in grp.rows" :key="ins.id">
                <InstrLeaf :ins="ins" />
                <div class="flex items-center gap-1.5 -mt-0.5 mb-0.5 text-[10px] text-gray-400 dark:text-gray-500" style="padding-inline-start:34px;padding-inline-end:8px">
                  <UIcon :name="pendingSourceIcon(ins)" class="w-3 h-3 shrink-0" />
                  <span class="truncate">{{ pendingSourceLabel(ins) }}</span>
                  <span v-if="pendingDate(ins)" class="opacity-60">·</span>
                  <span v-if="pendingDate(ins)" class="shrink-0">{{ pendingDate(ins) }}</span>
                  <span v-if="ins.source_type === 'git' && ins.source_file_path" class="ms-auto font-mono truncate opacity-80" :title="ins.source_file_path">{{ ins.source_file_path }}</span>
                </div>
              </div>
            </div>
            <EmptyHint v-if="!pendingGroups.length" :text="$t('agentsPage.pendingChangesEmpty')" />
          </template>
        </div>

        <!-- Server-side "Search everything" results (agents + instructions). -->
        <div v-if="!pendingView && searchResults" class="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-2">
          <div v-if="searching" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500 px-2"><Spinner class="w-3.5 h-3.5" /><span>Searching…</span></div>
          <template v-else>
            <div v-if="searchResults.agents.length">
              <div class="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Agents</div>
              <button v-for="a in searchResults.agents" :key="a.id" type="button" class="w-full flex items-center gap-2 h-8 rounded-md text-[13px] text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70 px-2" @click="onAgentClick(a)">
                <DataSourceIcon :type="a.type" :connector-key="a.connector_key" :icon="a.icon" class="w-4 h-4 shrink-0" />
                <span class="flex-1 text-start truncate">{{ a.name }}</span>
              </button>
            </div>
            <div v-if="searchResults.instructions.length">
              <div class="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">Instructions</div>
              <InstrLeaf v-for="ins in searchResults.instructions" :key="ins.id" :ins="ins" />
            </div>
            <EmptyHint v-if="!searchResults.agents.length && !searchResults.instructions.length" text="No results." />
          </template>
        </div>

        <div v-show="!pendingView && !searchResults" class="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-0.5">
          <TreeGroup :label="$t('agentsPage.globalInstructions')" icon="i-heroicons-globe-alt" v-bind="rootDropzoneAttrs(GLOBAL_SCOPE)" :count="globalCount" :addable="canAddInstrFor()" :folderable="canAddInstrFor()" :open="isOpen('global')" @toggle="expand('global')" @add="openCreate({ global: true })" @folder="newDirectory(GLOBAL_SCOPE)">
            <div v-if="groupLoading('global')" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500" style="padding-inline-start:32px"><Spinner class="w-3.5 h-3.5" /><span>Loading…</span></div>
            <template v-else>
              <div>
                <DirNode v-for="d in childDirs(GLOBAL_SCOPE, null)" :key="d.id" :dir="d" :scope="GLOBAL_SCOPE" :list="listFor('global')" :indent="0" :can-manage="canAddInstrFor()" />
                <InstrLeaf v-for="ins in rootInstrs(GLOBAL_SCOPE, listFor('global'))" :key="ins.id" :ins="ins" :drag-scope="GLOBAL_SCOPE" :draggable="canAddInstrFor()" />
                <EmptyHint v-if="loadedGroups.has('global') && listFor('global').length === 0 && !hasDirs(GLOBAL_SCOPE)" :text="$t('agentsPage.noGlobalRules')" :add="canAddInstrFor()" @add="openCreate({ global: true })" />
              </div>
            </template>
          </TreeGroup>
          <TreeGroup :label="$t('agentsPage.skills')" icon="i-heroicons-sparkles" :count="skillCount" :open="isOpen('skills')" @toggle="expand('skills')">
            <div v-if="groupLoading('skills')" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500" style="padding-inline-start:32px"><Spinner class="w-3.5 h-3.5" /><span>Loading…</span></div>
            <template v-else>
              <EmptyHint v-if="skillCount === 0" :text="$t('agentsPage.noSkills')" />
              <InstrLeaf v-for="ins in listFor('skills')" :key="ins.id" :ins="ins" />
            </template>
            <NuxtLink to="/skills" data-testid="browse-skill-catalog" class="group w-full flex items-center gap-1.5 h-7 rounded-md text-[11px] text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors" style="padding-inline-start:32px;padding-inline-end:8px" @click.stop>
              <UIcon name="i-heroicons-squares-plus" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
              <span class="truncate">{{ $t('skillCatalog.browse') }}</span>
            </NuxtLink>
          </TreeGroup>
          <!-- Org-wide evals (apply to all agents). Admin-gated via manage_evals. -->
          <!-- Global Evals mirrors an agent's Evals group: chevron expands the
               org-wide shelves, label opens the runs panel. Gated on ORG-LEVEL
               manage_evals — an org-wide suite holds cases that run against
               every agent, so a per-agent grant is not authority over it. -->
          <TreeGroup
            v-if="canManageEvals"
            :label="$t('agentsPage.globalEvals')"
            icon="i-heroicons-check-circle"
            :indent="0"
            :active="panelView?.kind === 'global-evals'"
            :count="evalTree['global']?.loaded ? evalCount('global') : undefined"
            :addable="canManageEvalScope('global')"
            :folderable="canManageEvalScope('global')"
            :open="isOpen('evals:global')"
            @toggle="onEvalsRowClick('global')"
            @folder="createSuiteIn('global')"
            @add="openNewEvalCase('global', suitesForScope('global')[0]?.id || '')"
          >
            <div v-if="evalTree['global']?.loading" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500" style="padding-inline-start:34px"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>
            <template v-else>
              <SuiteNode v-for="su in suitesForScope('global')" :key="su.id" :suite="su" scope="global" :indent="1" :can-manage="canManageEvalScope('global')" />
                <!-- Cases whose target is this scope but whose suite lives
                     elsewhere (the org-wide Drafts bucket, mostly). Not a drop
                     target — it is derived, so you drag OUT of it into a real
                     suite and it empties itself. -->
                <TreeGroup
                  v-if="unfiledForScope('global').length"
                  :label="$t('agentsPage.unfiledTests')"
                  icon="i-heroicons-inbox"
                  :indent="1"
                  :count="unfiledForScope('global').length"
                  :open="isOpen('evals-unfiled:' + 'global')"
                  @toggle="expand('evals-unfiled:' + 'global')"
                >
                  <CaseLeaf v-for="c in unfiledForScope('global')" :key="c.id" :case="c" :scope="'global'" :indent="2" :draggable="canManageEvalScope('global')" />
                </TreeGroup>

              <EmptyHint v-if="evalTree['global']?.loaded && suitesForScope('global').length === 0" :text="$t('agentsPage.noSuites')" :add="canManageEvalScope('global')" @add="createSuiteIn('global')" :pad="34" />
            </template>
          </TreeGroup>
          <div class="h-px bg-gray-100 dark:bg-gray-800 my-2 mx-1"></div>

          <div class="px-2 pt-1 pb-1 flex items-center justify-between">
            <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">{{ $t('agentsPage.agentsSection') }}</span>
            <UTooltip v-if="canViewAllAgents" :text="$t('data.showAllAgentsHint')">
              <label class="flex items-center gap-1 text-[10px] text-gray-400 dark:text-gray-500 cursor-pointer hover:text-gray-600 dark:hover:text-gray-400 select-none">
                <UToggle v-model="showAllAgents" size="2xs" />
                <span>{{ $t('data.showAllAgents') }}</span>
              </label>
            </UTooltip>
          </div>

          <div v-if="!agentsLoaded" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500 px-2"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>

          <template v-for="agent in agents" :key="agent.id">
            <TreeGroup :label="agent.name" :count="agentCount(agent.id) || undefined" :pending="agentPending(agent.id)" :status-dot="agentStatusDot(agent)" :lock="agent.is_public === false" :badge="needsSignIn(agent) ? $t('agentsPage.signInBadge') : (agent.publish_status === 'disabled' ? $t('agentsPage.disabledBadge') : (agent.is_connector ? $t('agentsPage.connectorBadge') : ''))" :badge-interactive="needsSignIn(agent)" :disabled="needsSignIn(agent) && !canManageAgent(agent.id)" :active="agentView?.agentId === agent.id" :open="isOpen('agent:' + agent.id)" @toggle="onAgentClick(agent)" @badge="openAgentTab(agent.id)">
              <template #icon><DataSourceIcon :type="agent.type" :connector-key="agent.connector_key" :icon="agent.icon" class="w-4 h-4 shrink-0" /></template>

              <!-- Content sections need a queryable agent, so they stay hidden while
                   the viewer hasn't signed in with their own credentials. Settings
                   (below) stays reachable for managers — deleting or reconfiguring
                   an agent must not require personal credentials to it. -->
              <TreeGroup v-if="!needsSignIn(agent)" :label="$t('agentsPage.tables')" icon="i-heroicons-table-cells" :count="agentTables[agent.id] ? ((agentTableTotals[agent.id] ?? activeTables(agent.id).length) || undefined) : undefined" :indent="1" reloadable :active="panelView?.kind === 'tables' && panelView?.agentId === agent.id" :open="isOpen('tables:' + agent.id)" @toggle="onPanelRowClick('tables', agent.id)" @reload="reloadTables(agent.id)">
                <TreeGroup v-for="t in activeTables(agent.id)" :key="t.id" :label="t.name" icon="i-heroicons-table-cells" :count="listForTable(agent.id, t.id).length || undefined" mono :addable="canAddInstrFor(agent.id)" :indent="2" :open="isOpen('table:' + agent.id + ':' + t.id)" @toggle="expand('table:' + agent.id + ':' + t.id)" @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })">
                  <InstrLeaf v-for="ins in listForTable(agent.id, t.id)" :key="ins.id" :ins="ins" :indent="3" />
                  <EmptyHint v-if="loadedGroups.has(agent.id) && listForTable(agent.id, t.id).length === 0" :text="$t('agentsPage.noRulesAttached')" :add="canAddInstrFor(agent.id)" @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })" :pad="62" />
                </TreeGroup>
                <EmptyHint v-if="agentTables[agent.id] && activeTables(agent.id).length === 0" :text="$t('agentsPage.noActiveTables')" :pad="48" />
              </TreeGroup>

              <TreeGroup v-if="!needsSignIn(agent)" :label="$t('agentsPage.tools')" icon="i-heroicons-wrench-screwdriver" :count="agentTools[agent.id]?.length" :indent="1" reloadable :active="panelView?.kind === 'tools' && panelView?.agentId === agent.id" :open="isOpen('tools:' + agent.id)" @toggle="onPanelRowClick('tools', agent.id)" @reload="reloadTools(agent.id)">
                <!-- Grouped by connection (MCP / custom API). Click a group to expand its tools. -->
                <TreeGroup v-for="grp in toolGroups(agent.id)" :key="grp.connId" :label="grp.name" :count="grp.tools.length" :indent="2" :open="isOpen('toolconn:' + agent.id + ':' + grp.connId)" @toggle="expand('toolconn:' + agent.id + ':' + grp.connId)">
                  <template #icon><DataSourceIcon v-if="grp.type" :type="grp.type" :connector-key="grp.connector_key" class="w-4 h-4 shrink-0" /><UIcon v-else name="i-heroicons-wrench-screwdriver" class="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" /></template>
                  <div v-for="tool in grp.tools" :key="tool.id || tool.name" class="flex items-center gap-2 h-8 rounded-md text-[13px] text-gray-600 dark:text-gray-400" style="padding-inline-start:62px;padding-inline-end:8px">
                    <UIcon name="i-heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-300 dark:text-gray-600 shrink-0" />
                    <span class="flex-1 text-start truncate font-mono text-xs">{{ tool.name }}</span>
                    <span v-if="tool.is_enabled === false" class="text-[9px] px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500">{{ $t('agentsPage.toolOff') }}</span>
                    <span v-else-if="(tool.effective_policy || tool.policy) && (tool.effective_policy || tool.policy) !== 'allow'" class="text-[9px] px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400" :class="tool.user_policy ? 'text-blue-500 dark:text-blue-400' : ''" :title="tool.user_policy ? $t('agentsPage.toolPolicyYours') : ''">{{ tool.effective_policy || tool.policy }}</span>
                  </div>
                </TreeGroup>
                <EmptyHint v-if="(agentTools[agent.id]?.length ?? -1) === 0" :text="$t('agentsPage.noToolsConnected')" :pad="48" />
              </TreeGroup>

              <TreeGroup v-if="!needsSignIn(agent)" :label="$t('agentsPage.files')" icon="i-heroicons-paper-clip" :count="filesGroupCount(agent.id)" :indent="1" addable :active="panelView?.kind === 'files' && panelView?.agentId === agent.id" :open="isOpen('files:' + agent.id)" @toggle="onPanelRowClick('files', agent.id)" @add="triggerUpload(agent.id)">
                <!-- Directory connections: each glob rule, prefixed with its connection-type icon -->
                <template v-for="fc in (agentFileConns[agent.id] || [])" :key="fc.id">
                  <div v-for="g in fc.globs" :key="fc.id + ':' + g"
                    class="flex items-center gap-2 h-8 rounded-md text-[13px] text-gray-600 dark:text-gray-400 min-w-0"
                    style="padding-inline-start:48px;padding-inline-end:8px">
                    <DataSourceIcon :type="fc.type" :connector-key="fc.connector_key" class="w-3.5 h-3.5 shrink-0" />
                    <span class="flex-1 text-start truncate font-mono text-xs">{{ g }}</span>
                    <span class="text-[10px] text-gray-400 dark:text-gray-500 truncate max-w-[100px]">{{ fc.name }}</span>
                  </div>
                  <div v-if="fc.globs.length === 0" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500 italic min-w-0" style="padding-inline-start:48px">
                    <DataSourceIcon :type="fc.type" :connector-key="fc.connector_key" class="w-3.5 h-3.5 shrink-0" />
                    <span class="truncate">{{ fc.name }} · whole path</span>
                  </div>
                </template>
                <div
                  v-for="f in (agentFiles[agent.id] || [])" :key="f.id"
                  class="group/file w-full flex items-center gap-2 h-8 rounded-md text-[13px] transition-colors min-w-0 cursor-pointer"
                  :class="previewFile && previewFile.id === f.id ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70'"
                  style="padding-inline-start:48px;padding-inline-end:8px" @click="openFile(f, agent.id)"
                >
                  <UIcon :name="fileIcon(f.content_type, f.filename)" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
                  <span class="flex-1 text-start truncate">{{ f.filename }}</span>
                  <button v-if="canManageAgent(agent.id)" class="shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 opacity-0 group-hover/file:opacity-100 flex items-center justify-center" :title="$t('agentsPage.tipDeleteFile')" @click.stop="deleteFile(agent.id, f)"><UIcon name="i-heroicons-trash" class="w-3 h-3" /></button>
                </div>
                <EmptyHint v-if="(agentFiles[agent.id]?.length ?? 0) === 0 && (agentFileConns[agent.id]?.length ?? 0) === 0" :text="$t('agentsPage.noFiles')" add @add="triggerUpload(agent.id)" :pad="48" />
                <div v-if="uploadingAgent === agent.id" class="text-[11px] text-gray-400 dark:text-gray-500 italic py-1" style="padding-inline-start:48px">{{ $t('agentsPage.uploading') }}</div>
              </TreeGroup>

              <TreeGroup v-if="!needsSignIn(agent)" :label="$t('agentsPage.instructions')" icon="i-heroicons-document-text" v-bind="rootDropzoneAttrs(agent.id)" :count="loadedGroups.has(agent.id) ? listForAgent(agent.id).length : (agentCount(agent.id) || undefined)" :addable="canAddInstrFor(agent.id)" :folderable="canAddInstrFor(agent.id)" :indent="1" :open="isOpen('instr:' + agent.id)" @toggle="expand('instr:' + agent.id)" @add="openCreate({ agentId: agent.id })" @folder="newDirectory(agent.id)">
                <div v-if="groupLoading(agent.id)" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500" style="padding-inline-start:48px"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>
                <template v-else>
                  <div>
                    <DirNode v-for="d in childDirs(agent.id, null)" :key="d.id" :dir="d" :scope="agent.id" :list="listForAgent(agent.id)" :indent="2" :can-manage="canAddInstrFor(agent.id)" />
                    <InstrLeaf v-for="ins in rootInstrs(agent.id, listForAgent(agent.id))" :key="ins.id" :ins="ins" :indent="2" :drag-scope="agent.id" :draggable="canAddInstrFor(agent.id)" />
                    <EmptyHint v-if="loadedGroups.has(agent.id) && listForAgent(agent.id).length === 0 && !hasDirs(agent.id)" :text="$t('agentsPage.noInstructions')" :add="canAddInstrFor(agent.id)" @add="openCreate({ agentId: agent.id })" :pad="48" />
                  </div>
                </template>
              </TreeGroup>

              <!-- Evals: the chevron expands the suite tree, the LABEL still opens
                   the runs/self-learning panel, so the existing entry point is
                   not lost to the new hierarchy. -->
              <TreeGroup
                v-if="canManageAgentEvals(agent.id) && !needsSignIn(agent)"
                :label="$t('agentsPage.evals')"
                icon="i-heroicons-check-circle"
                :indent="1"
                :active="panelView?.kind === 'evals' && panelView?.agentId === agent.id"
                :count="evalTree[agent.id]?.loaded ? evalCount(agent.id) : undefined"
                :addable="canManageEvalScope(agent.id)"
                :folderable="canManageEvalScope(agent.id)"
                :open="isOpen('evals:' + agent.id)"
                @toggle="onEvalsRowClick(agent.id)"
                @folder="createSuiteIn(agent.id)"
                @add="openNewEvalCase(agent.id, suitesForScope(agent.id)[0]?.id || '')"
              >
                <div v-if="evalTree[agent.id]?.loading" class="flex items-center gap-2 h-8 text-[13px] text-gray-400 dark:text-gray-500" style="padding-inline-start:48px"><Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span></div>
                <template v-else>
                  <SuiteNode v-for="su in suitesForScope(agent.id)" :key="su.id" :suite="su" :scope="agent.id" :indent="2" :can-manage="canManageEvalScope(agent.id)" />
                  <!-- Cases whose target is this scope but whose suite lives
                       elsewhere (the org-wide Drafts bucket, mostly). Not a drop
                       target — it is derived, so you drag OUT of it into a real
                       suite and it empties itself. -->
                  <TreeGroup
                    v-if="unfiledForScope(agent.id).length"
                    :label="$t('agentsPage.unfiledTests')"
                    icon="i-heroicons-inbox"
                    :indent="2"
                    :count="unfiledForScope(agent.id).length"
                    :open="isOpen('evals-unfiled:' + agent.id)"
                    @toggle="expand('evals-unfiled:' + agent.id)"
                  >
                    <CaseLeaf v-for="c in unfiledForScope(agent.id)" :key="c.id" :case="c" :scope="agent.id" :indent="3" :draggable="canManageEvalScope(agent.id)" />
                  </TreeGroup>

                  <EmptyHint v-if="evalTree[agent.id]?.loaded && suitesForScope(agent.id).length === 0" :text="$t('agentsPage.noSuites')" :add="canManageEvalScope(agent.id)" @add="createSuiteIn(agent.id)" :pad="48" />
                </template>
              </TreeGroup>

              <button v-if="canManageAgent(agent.id)" type="button" class="group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0" :class="panelView?.kind === 'settings' && panelView?.agentId === agent.id ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70'" style="padding-inline-start:20px;padding-inline-end:8px" @click="openPanel('settings', agent.id)">
                <span class="w-3 shrink-0"></span>
                <UIcon name="i-heroicons-cog-6-tooth" class="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
                <span class="flex-1 text-start truncate">{{ $t('agentsPage.settings') }}</span>
                <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 text-gray-300 dark:text-gray-600 shrink-0 opacity-0 group-hover:opacity-100 rtl:rotate-180" />
              </button>
            </TreeGroup>
          </template>
        </div>

        <!-- Connections footer. The icon preview is the only flexible part: it
             clips when the pane is narrow so the label, +N count, "new" button
             and "View all" (the functional affordances) always stay in view and
             never overflow the pane. py/-my + pe/-me give the status dots room
             so overflow-hidden doesn't clip them. -->
        <div class="border-t border-gray-200 dark:border-gray-800 px-3 py-2 flex items-center gap-2">
          <span class="min-w-0 truncate text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 me-1">{{ $t('agentsPage.connections') }}</span>
          <Spinner v-if="!connectionsLoaded" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
          <div v-else class="flex items-center gap-2 min-w-0 shrink-[9999] overflow-hidden py-1 -my-1 pe-1 -me-1">
            <UTooltip v-for="c in connections.slice(0, 4)" :key="c.id" :text="`${c.name} · ${c.type}`">
              <button type="button" class="relative inline-flex items-center justify-center w-6 h-6 rounded-md border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="openConnectionDetail(c)">
                <DataSourceIcon :type="c.type" :connector-key="c.connector_key" class="w-3.5 h-3.5" />
                <span class="absolute -bottom-0.5 -end-0.5 w-1.5 h-1.5 rounded-full" :class="connDotClass(c)"></span>
              </button>
            </UTooltip>
          </div>
          <UTooltip v-if="connections.length > 4" class="shrink-0" :text="$t('agentsPage.viewAllConnections', { n: connections.length })">
            <button type="button" class="inline-flex items-center justify-center h-6 px-1.5 rounded-md border border-gray-200 dark:border-gray-800 text-[11px] font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="showConnectionsModal = true">+{{ connections.length - 4 }}</button>
          </UTooltip>
          <UTooltip v-if="canCreateDataSource && connections.length" class="shrink-0" :text="$t('agentsPage.newConnection')">
            <button type="button" class="inline-flex items-center justify-center w-6 h-6 rounded-md border border-dashed border-gray-300 dark:border-gray-700 text-gray-400 dark:text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:text-gray-600 dark:hover:text-gray-400" @click="connTargetAgentId = null; showAddConnection = true">
              <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
            </button>
          </UTooltip>
          <!-- Empty state: explicit CTA so connecting data is discoverable even with no agents yet -->
          <button v-if="connectionsLoaded && canCreateDataSource && connections.length === 0" type="button" class="shrink-0 inline-flex items-center gap-1 h-6 px-2 rounded-md border border-dashed border-gray-300 dark:border-gray-700 text-[11px] font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:text-gray-700 dark:hover:text-gray-300" @click="connTargetAgentId = null; showAddConnection = true">
            <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
            {{ $t('agentsPage.addConnection') }}
          </button>
          <button v-if="connections.length" type="button" class="ms-auto shrink-0 text-[11px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300" @click="showConnectionsModal = true">{{ $t('agentsPage.viewAll') }}</button>
        </div>

        <!-- Drag handle to resize the tree pane (desktop only) -->
        <div v-if="!isMobile" class="absolute top-0 end-0 h-full w-1 cursor-col-resize hover:bg-gray-300 dark:hover:bg-gray-700 transition-colors z-10" :title="$t('agentsPage.tipDragResize')" @mousedown="startTreeResize"></div>
      </aside>

      <!-- ── Pane 2: Detail ───────────────────────────── -->
      <!-- Detail pane. Mobile: full-width, hidden until an item is selected. -->
      <section class="flex-1 min-w-0 flex flex-col" :class="{ hidden: isMobile && !detailOpen }">
        <!-- Mobile back-to-tree bar -->
        <button
          v-if="isMobile && detailOpen"
          type="button"
          class="flex items-center gap-1.5 h-11 px-3 shrink-0 text-sm text-gray-600 dark:text-gray-300 border-b border-gray-200 dark:border-gray-800"
          @click="backToTree"
        >
          <UIcon name="i-heroicons-arrow-left" class="w-4 h-4 rtl-flip" />
          {{ $t('common.back') }}
        </button>
        <!-- Review feed -->
        <div v-if="reviewView" class="relative flex-1 min-h-0 flex flex-col">
          <ReviewFeed :agents="agents" :initial-agent-id="reviewView.agentId" @close="closeReview" @count="reviewCount = $event" @open-instruction="openInstructionFromReview" />
          <div v-if="reviewNavLoading" class="absolute inset-0 z-10 flex items-center justify-center bg-white/70 dark:bg-gray-900/70 backdrop-blur-[1px]">
            <UIcon name="i-heroicons-arrow-path" class="w-5 h-5 text-gray-400 dark:text-gray-500 animate-spin" />
          </div>
        </div>
        <!-- Agent overview -->
        <template v-else-if="agentView">
          <div class="shrink-0 px-4 sm:px-6 pt-4 pb-4 border-b border-gray-100 dark:border-gray-800">
            <!-- flex-wrap + basis-64: on phones the actions cluster wraps below
                 the title block instead of squeezing it into a sliver. -->
            <div class="flex flex-wrap items-start justify-between gap-3 gap-y-2">
              <div class="min-w-0 grow basis-64">
                <div class="flex flex-wrap items-center gap-2 gap-y-1.5 min-w-0">
                  <AgentIconPicker
                    v-if="agentDetail && agentCanUpdate"
                    :model-value="agentDetail.icon"
                    :type="agentDetail.type"
                    :connector-key="agentDetail.connector_key"
                    :connections="agentDetail.connections || []"
                    icon-only
                    icon-class="w-4 h-4"
                    class="shrink-0"
                    @change="setAgentIcon"
                  />
                  <DataSourceIcon v-else-if="agentDetail" :type="agentDetail.type" :connector-key="agentDetail.connector_key" :icon="agentDetail.icon" class="w-4 h-4 shrink-0" />
                  <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="(agentDetail?.status || 'active') === 'active' ? 'bg-green-500' : 'bg-gray-300'" :title="(agentDetail?.status || 'active') === 'active' ? $t('agentsPage.active') : $t('agentsPage.inactive')"></span>
                  <h2 class="text-base font-semibold text-gray-900 dark:text-white truncate">{{ agentDetail?.name || agentViewName }}</h2>
                  <UPopover v-if="agentCanUpdate" :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-md' }">
                    <button type="button" class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium shrink-0 transition-colors" :class="agentDetail?.is_public ? 'border-blue-200 dark:border-blue-500/30 bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-500/20' : 'border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70'">
                      <UIcon :name="agentDetail?.is_public ? 'i-heroicons-globe-alt' : 'i-heroicons-lock-closed'" class="w-3 h-3" />{{ agentDetail?.is_public ? $t('agentsPage.public') : $t('agentsPage.private') }}
                      <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 opacity-60" />
                    </button>
                    <template #panel="{ close }">
                      <div class="p-1 w-40">
                        <button class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start" @click="setAgentPublic(true); close()"><UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.public') }}<UIcon v-if="agentDetail?.is_public" name="i-heroicons-check" class="w-3 h-3 ms-auto text-gray-900 dark:text-white" /></button>
                        <button class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start" @click="setAgentPublic(false); close()"><UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.private') }}<UIcon v-if="!agentDetail?.is_public" name="i-heroicons-check" class="w-3 h-3 ms-auto text-gray-900 dark:text-white" /></button>
                      </div>
                    </template>
                  </UPopover>
                  <span v-else class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium shrink-0" :class="agentDetail?.is_public ? 'border-blue-200 dark:border-blue-500/30 bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400' : 'border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-800 text-gray-500 dark:text-gray-400'"><UIcon :name="agentDetail?.is_public ? 'i-heroicons-globe-alt' : 'i-heroicons-lock-closed'" class="w-3 h-3" />{{ agentDetail?.is_public ? $t('agentsPage.public') : $t('agentsPage.private') }}</span>
                  <PublishStatusControl v-if="agentDetail" :key="agentView.agentId" :data-source-id="agentView.agentId" :status="agentDetail.publish_status || 'published'" :reliability-status="agentDetail.reliability_status" @updated="onAgentPublishUpdated" />
                  <!-- Auth badges (parity with the legacy agents page) -->
                  <UTooltip v-if="agentDetail && usesServiceAccount(agentDetail)" :text="$t('agentsPage.serviceAccountTip')">
                    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 h-5 rounded shrink-0 bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"><UIcon name="i-heroicons-cpu-chip" class="w-2.5 h-2.5" />{{ $t('agentsPage.serviceAccount') }}</span>
                  </UTooltip>
                  <UTooltip v-if="agentListItem?.admin_only" :text="$t('agentsPage.adminTip')">
                    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 h-5 rounded shrink-0 bg-amber-100 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 uppercase tracking-wide font-medium"><UIcon name="i-heroicons-shield-check" class="w-2.5 h-2.5" />{{ $t('agentsPage.adminBadge') }}</span>
                  </UTooltip>
                </div>
                <div class="mt-1.5 group">
                  <input v-if="editingDesc" ref="descInputRef" v-model="descForm" type="text" :placeholder="$t('agentsPage.addDescription')" class="w-full text-sm text-gray-600 dark:text-gray-300 border-b border-blue-400 bg-transparent outline-none py-0.5" @keydown.enter="saveDesc" @keydown.escape="cancelDesc" @blur="saveDesc" />
                  <div v-else class="flex items-center gap-2">
                    <p class="text-sm text-gray-500 dark:text-gray-400 rounded px-1 -mx-1" :class="agentCanUpdate ? 'cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-800/70' : ''" @click="agentCanUpdate && startEditDesc()">{{ agentDetail?.description || (agentCanUpdate ? $t('agentsPage.addDescription') : '') }}</p>
                    <button v-if="agentCanUpdate" class="text-[10px] text-blue-600 hover:underline opacity-0 group-hover:opacity-100 shrink-0" @click="startEditDesc">{{ $t('agentsPage.edit') }}</button>
                  </div>
                </div>
              </div>
              <div class="flex flex-wrap items-center gap-2 ms-auto">
                <!-- Per-agent activity sparkline + task total -->
                <div v-if="activitySeries.length" class="flex items-center gap-2.5 pe-1" :title="$t('agentsPage.tasksTip')">
                  <span class="flex flex-col items-center leading-none">
                    <svg width="78" height="20" viewBox="0 0 96 26" preserveAspectRatio="none" class="overflow-visible"><path :d="sparkPath" fill="none" stroke="#10b981" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" /></svg>
                    <span class="mt-1 text-[10px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.activity') }}</span>
                  </span>
                  <span class="flex flex-col items-start leading-none">
                    <span class="text-sm font-semibold text-gray-900 dark:text-white tabular-nums">{{ totalTasks.toLocaleString() }}</span>
                    <span class="mt-1 text-[10px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.tasks') }}</span>
                  </span>
                </div>
                <button v-if="canManageAgent(agentView.agentId)" class="h-7 px-2.5 rounded-md border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium whitespace-nowrap hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1" :title="$t('agentsPage.selfLearningTip')" @click="showSelfLearning = true"><UIcon name="i-heroicons-sparkles" class="w-3.5 h-3.5 text-blue-500" />{{ $t('agentsPage.selfLearning') }}</button>
                <button class="h-7 px-2.5 rounded-md bg-blue-600 text-white text-xs font-medium whitespace-nowrap hover:bg-blue-700 inline-flex items-center gap-1" @click="createReportForAgent(agentView.agentId)"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('agentsPage.newReport') }}</button>
                <button v-if="canManageAgent(agentView.agentId)" type="button" :disabled="exportingInstructions" class="h-7 w-7 rounded-md flex items-center justify-center border border-gray-200 dark:border-gray-800 text-gray-400 dark:text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:text-gray-600 dark:hover:text-gray-400 disabled:opacity-50" :title="$t('agentsPage.exportInstructions')" @click="exportAgentInstructions(agentView.agentId)"><UIcon :name="exportingInstructions ? 'i-heroicons-arrow-path' : 'i-heroicons-arrow-down-tray'" :class="['w-3.5 h-3.5', exportingInstructions && 'animate-spin']" /></button>
                <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="exitAgentView"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
              </div>
            </div>
          </div>
          <div class="flex-1 overflow-y-auto px-4 sm:px-6 py-5 max-w-3xl">
            <div v-if="agentDetailLoading" class="flex items-center justify-center py-16 text-gray-400 dark:text-gray-500">
              <Spinner class="w-5 h-5 animate-spin" />
            </div>
            <template v-else>
            <!-- Connections / Connect -->
            <div class="flex flex-wrap items-center gap-1.5 mb-3">
              <button v-for="c in (agentDetail?.connections || [])" :key="c.id" class="inline-flex items-center gap-1.5 px-2 h-6 rounded-md border border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-400 text-[11px] hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="openConnectionDetail(c)">
                <DataSourceIcon :type="c.type" :connector-key="c.connector_key" class="w-3.5 h-3.5" />{{ c.name }}
                <span class="w-1.5 h-1.5 rounded-full" :class="c.is_active === false ? 'bg-gray-300' : 'bg-green-500'"></span>
              </button>
              <button v-if="agentDetail && needsSignIn(agentDetail)" class="inline-flex items-center gap-1.5 px-2.5 h-6 rounded-md bg-blue-50 dark:bg-blue-500/10 border border-blue-200 dark:border-blue-500/30 text-blue-600 dark:text-blue-400 text-[11px] font-medium hover:bg-blue-100 dark:hover:bg-blue-500/20" @click="openAgentTab(agentView.agentId)"><UIcon name="i-heroicons-key" class="w-3 h-3" />{{ $t('agentsPage.connect') }}</button>
              <UTooltip :text="$t('agentsPage.manageConnections')">
                <button type="button" class="inline-flex items-center justify-center w-6 h-6 rounded-md border border-gray-200 dark:border-gray-800 text-gray-400 dark:text-gray-500 hover:bg-gray-50 dark:hover:bg-gray-800/50 hover:text-gray-600 dark:hover:text-gray-400" @click="openConnModal(agentView.agentId)"><UIcon name="i-heroicons-cog-6-tooth" class="w-3.5 h-3.5" /></button>
              </UTooltip>
            </div>

            <!-- Counts (clean). Each acts as a shortcut into the matching tree
                 section, mirroring a click on that tree row. -->
            <div class="flex flex-wrap items-center gap-x-1 gap-y-1 text-xs text-gray-500 dark:text-gray-400 mb-6 pb-5 border-b border-gray-100 dark:border-gray-800">
              <button type="button" class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-gray-100 dark:hover:bg-gray-800/70 hover:text-gray-700 dark:hover:text-gray-300 transition-colors" @click="openAgentSection('tables', agentView.agentId)"><UIcon name="i-heroicons-table-cells" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.countTables', { n: agentTableTotals[agentView.agentId] ?? agentTables[agentView.agentId]?.length ?? '–' }, statChoice(agentTableTotals[agentView.agentId] ?? agentTables[agentView.agentId]?.length)) }}</button>
              <button type="button" class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-gray-100 dark:hover:bg-gray-800/70 hover:text-gray-700 dark:hover:text-gray-300 transition-colors" @click="openAgentSection('tools', agentView.agentId)"><UIcon name="i-heroicons-wrench-screwdriver" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.countTools', { n: agentTools[agentView.agentId]?.length ?? '–' }, statChoice(agentTools[agentView.agentId]?.length)) }}</button>
              <button type="button" class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-gray-100 dark:hover:bg-gray-800/70 hover:text-gray-700 dark:hover:text-gray-300 transition-colors" @click="openAgentSection('files', agentView.agentId)"><UIcon name="i-heroicons-paper-clip" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.countFiles', { n: agentFiles[agentView.agentId]?.length ?? '–' }, statChoice(agentFiles[agentView.agentId]?.length)) }}</button>
              <button type="button" class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 hover:bg-gray-100 dark:hover:bg-gray-800/70 hover:text-gray-700 dark:hover:text-gray-300 transition-colors" @click="openAgentSection('instructions', agentView.agentId)"><UIcon name="i-heroicons-document-text" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.countInstructions', { n: agentCount(agentView.agentId) }, statChoice(agentCount(agentView.agentId))) }}</button>
            </div>

            <!-- Primary instruction (inline, clean editor) -->
            <div v-if="creatingPrimary || editingPrimary">
              <div class="flex items-center justify-between gap-2 mb-2">
                <input v-model="primaryDraft.title" type="text" :placeholder="$t('agentsPage.untitled')" class="flex-1 min-w-0 text-sm font-medium text-gray-900 dark:text-white bg-transparent outline-none placeholder:text-gray-300 dark:placeholder:text-gray-600" />
                <div class="flex items-center gap-1.5 shrink-0">
                  <button class="h-7 px-3 rounded-md text-gray-500 dark:text-gray-400 text-xs hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="cancelPrimary">{{ $t('agentsPage.cancel') }}</button>
                  <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50" :disabled="primarySaving || !primaryDraft.text.trim()" @click="savePrimary">{{ primarySaving ? $t('agentsPage.saving') : $t('agentsPage.save') }}</button>
                </div>
              </div>
              <div class="prose-instruction">
                <InstructionEditor key="primary-edit" v-model="primaryDraft.text" mode="wysiwyg" :editable="true" :data-source-ids="[agentView.agentId]" :placeholder="$t('agentsPage.primaryPlaceholder')" />
              </div>
            </div>
            <template v-else-if="agentDetail?.primary_instruction">
              <div v-if="agentCanUpdate" class="flex items-center justify-end gap-3 mb-1.5">
                <PrimaryInstructionPicker :agent-id="agentView.agentId" :current-instruction-id="agentDetail.primary_instruction.id" :label="$t('agentsPage.change')" @select="onSelectExistingPrimary" />
                <button class="text-[11px] text-blue-600 hover:underline" @click="startEditPrimary">{{ $t('agentsPage.edit') }}</button>
              </div>
              <InstructionText :text="agentDetail.primary_instruction.text" :references="agentDetail.primary_instruction.references || []" :prose="true" :markdown="true" />
            </template>
            <div v-else class="rounded-xl border border-dashed border-gray-200 dark:border-gray-700 bg-gray-50/40 dark:bg-gray-800/40 px-6 py-8 text-center">
              <div class="mx-auto w-10 h-10 rounded-full bg-blue-50 dark:bg-blue-500/10 flex items-center justify-center mb-3">
                <UIcon name="i-heroicons-document-text" class="w-5 h-5 text-blue-500" />
              </div>
              <p class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ $t('agentsPage.noPrimary') }}</p>
              <p class="mt-1 max-w-md mx-auto text-xs text-gray-500 dark:text-gray-400">{{ $t('agentsPage.noPrimaryDesc') }}</p>
              <div v-if="agentCanUpdate" class="mt-4 flex items-center justify-center gap-3">
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors" @click="startCreatePrimary"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('agentsPage.addPrimary') }}</button>
                <span class="text-xs text-gray-400 dark:text-gray-500">{{ $t('agentsPage.or') }}</span>
                <PrimaryInstructionPicker :agent-id="agentView.agentId" :label="$t('agentsPage.selectExisting')" @select="onSelectExistingPrimary" />
              </div>
              <div v-if="agentCanStartTraining" class="mt-3">
                <button class="text-xs text-sky-600 hover:underline inline-flex items-center gap-1" @click="startTrainingSessionForAgent(agentView.agentId)"><UIcon name="i-heroicons-academic-cap" class="w-3.5 h-3.5" />{{ $t('agentsPage.startTraining') }}</button>
              </div>
            </div>

            <!-- Conversation starters (editable) -->
            <div class="mt-6">
              <div class="flex items-center gap-2 mb-2">
                <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">{{ $t('agentsPage.conversationStarters') }}</span>
                <button v-if="agentCanUpdate" class="text-[10px] text-blue-600 hover:underline" @click="openEditStarters">{{ $t('agentsPage.edit') }}</button>
              </div>
              <div v-if="starterPrompts.length" class="flex flex-wrap gap-2">
                <button v-for="(p, i) in starterPrompts" :key="p.id || i" type="button" :disabled="startingReport" class="group/cs inline-flex items-center gap-1.5 bg-gray-100 dark:bg-gray-800 rounded-lg px-3 py-2 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-900 dark:hover:bg-gray-700 hover:text-white dark:hover:text-white disabled:opacity-50 transition-colors" @click="startReportWithStarter(agentView.agentId, p.text, i)">
                  <Spinner v-if="startingReport && startingStarterIdx === i" class="w-3 h-3 animate-spin shrink-0" />
                  <span>{{ starterTitle(p.text) }}</span>
                </button>
              </div>
              <p v-else class="text-[11px] text-gray-300 dark:text-gray-600 italic">{{ $t('agentsPage.noConversationStarters') }}</p>
            </div>
            </template>
          </div>
        </template>

        <!-- Tables / Tools editable panel -->
        <!-- A test case opens HERE, in the pane, rather than in a dialog: the
             tree stays visible so you can click through cases the way you click
             through instructions. Same TestCaseEditor the modal hosts, so the
             expectations builder is not duplicated. -->
        <template v-else-if="evalCaseView">
          <div class="flex items-center gap-2 px-6 py-3 border-b border-gray-100 dark:border-gray-800">
            <UIcon name="i-heroicons-beaker" class="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
            <span class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ evalCaseView.caseId ? $t('agentsPage.editTest') : $t('agentsPage.newTest') }}</span>
            <div class="ms-auto flex items-center gap-2">
              <UButton :loading="evalEditorRef?.isSaving" :disabled="evalEditorRef?.initialLoading" color="blue" size="xs" @click="() => evalEditorRef?.save()">{{ $t('agentsPage.saveTest') }}</UButton>
              <UButton :loading="evalEditorRef?.isRunning" :disabled="evalEditorRef?.initialLoading" color="blue" variant="soft" size="xs" @click="() => evalEditorRef?.runNow()">{{ $t('agentsPage.saveAndRun') }}</UButton>
              <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70 shrink-0" @click="closeEvalCase"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
            </div>
          </div>
          <div class="flex-1 overflow-auto px-6 py-4">
            <TestCaseEditor
              ref="evalEditorRef"
              :key="'evalcase-' + (evalCaseView.caseId || 'new') + '-' + evalCaseView.suiteId"
              :suite-id="evalCaseView.suiteId"
              :case-id="evalCaseView.caseId || undefined"
              :agent-id="evalCaseView.scope === 'global' ? undefined : evalCaseView.scope"
              :closable="false"
              @close="closeEvalCase"
              @created="onEvalCaseSaved"
              @updated="onEvalCaseSaved"
            />
          </div>
        </template>

        <template v-else-if="panelView">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
            <div class="flex items-center gap-1.5 min-w-0">
              <template v-if="panelView.kind === 'global-evals'">
                <UIcon name="i-heroicons-check-circle" class="w-[18px] h-[18px] shrink-0 text-gray-400 dark:text-gray-500" />
                <span class="text-[13px] font-medium text-gray-700 dark:text-gray-300 truncate">{{ $t('agentsPage.globalEvals') }}</span>
                <span class="text-[11px] px-1.5 h-4 inline-flex items-center rounded bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 shrink-0">{{ $t('agentsPage.allAgentsTag') }}</span>
              </template>
              <template v-else>
                <button type="button" class="flex items-center gap-1.5 min-w-0 rounded px-1 -mx-1 hover:bg-gray-100 dark:hover:bg-gray-800/70" :title="$t('agentsPage.tipOpenAgent')" @click="openAgent(panelView.agentId)">
                  <DataSourceIcon :type="panelAgent?.type" :connector-key="panelAgent?.connector_key" :icon="panelAgent?.icon" class="w-[18px] h-[18px] shrink-0" />
                  <span class="text-[13px] font-medium text-gray-700 dark:text-gray-300 truncate hover:text-gray-900 dark:hover:text-white">{{ panelAgent?.name || $t('agentsPage.agent') }}</span>
                </button>
                <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 dark:text-gray-600 shrink-0 rtl:rotate-180" />
                <span class="text-[13px] text-gray-500 dark:text-gray-400 shrink-0">{{ panelKindLabel }}</span>
                <span v-if="(panelView.kind === 'tables' || panelView.kind === 'tools') && !panelCanUpdate" class="text-[11px] px-1.5 h-4 inline-flex items-center rounded bg-gray-100 dark:bg-gray-800 text-gray-400 dark:text-gray-500 shrink-0">{{ $t('agentsPage.readOnly') }}</span>
              </template>
            </div>
            <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70 shrink-0" @click="closePanel"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
          </div>
          <div class="flex-1 overflow-auto">
            <AgentEvalsPanel v-if="panelView.kind === 'evals'" :key="'evals-' + panelView.agentId" :agent-id="panelView.agentId" :initial-run-id="pendingRunId" />
            <AgentEvalsPanel v-else-if="panelView.kind === 'global-evals'" key="global-evals" global :initial-run-id="pendingRunId" />
            <AgentSettingsPanel v-else-if="panelView.kind === 'settings'" :key="'settings-' + panelView.agentId" :agent-id="panelView.agentId" @updated="onAgentSettingsUpdated" @deleted="onAgentDeleted" />
            <div v-else class="px-6 py-4">
              <TablesSelector
                v-if="panelView.kind === 'tables'"
                :key="'tables-' + panelView.agentId + '-' + tablesRefreshKey"
                :ds-id="panelView.agentId"
                schema="full"
                :can-update="panelCanUpdate"
                :show-refresh="panelCanUpdate"
                :show-save="panelCanUpdate"
                :show-stats="true"
                max-height="calc(100vh - 240px)"
              >
                <template #reload-left>
                  <button type="button" class="h-7 px-2.5 rounded-md border border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-400 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1" :title="$t('agentsPage.manageConnections')" @click="openConnModal(panelView.agentId)"><UIcon name="i-heroicons-link" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.connections') }}</button>
                </template>
              </TablesSelector>
              <ToolsSelector
                v-else-if="panelView.kind === 'tools'"
                :key="'tools-' + panelView.agentId + '-' + toolsRefreshKey"
                :ds-id="panelView.agentId"
                :connections="panelConnections"
                :can-update="panelCanUpdate"
                @add-mcp="openAddMcp(panelView.agentId)"
                @add-custom-api="openAddCustomApi(panelView.agentId)"
                @edit-connection="openConnectionDetail"
                @delete-connection="onToolsConnectionChanged"
              />
              <AgentFilesPanel
                v-else-if="panelView.kind === 'files'"
                :key="'files-' + panelView.agentId"
                :ds-id="panelView.agentId"
                :can-update="panelCanUpdate"
                @edit-connection="openConnectionDetail"
              />
            </div>
          </div>
        </template>

        <!-- File preview -->
        <template v-else-if="previewFile">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
            <div class="flex items-center gap-2 min-w-0">
              <UIcon :name="fileIcon(previewFile.content_type, previewFile.filename)" class="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
              <span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">{{ previewFile.filename }}</span>
              <span class="text-[10px] text-gray-300 dark:text-gray-600 shrink-0">{{ previewFile.content_type }}</span>
            </div>
            <div class="flex items-center gap-1.5">
              <button v-if="previewUrl" class="h-7 px-3 rounded-md border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="downloadPreview">{{ $t('agentsPage.open') }}</button>
              <button v-if="previewFileAgentId && canManageAgent(previewFileAgentId)" class="h-7 px-3 rounded-md border border-gray-200 dark:border-gray-800 text-red-600 dark:text-red-400 text-xs font-medium hover:bg-red-50 dark:hover:bg-red-500/10" @click="deleteFile(previewFileAgentId, previewFile)">{{ $t('agentsPage.delete') }}</button>
              <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="closePreview"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
            </div>
          </div>
          <div class="flex-1 overflow-auto p-6">
            <div v-if="previewLoading" class="text-center text-xs text-gray-400 dark:text-gray-500 py-10">{{ $t('agentsPage.loading') }}</div>
            <img v-else-if="isImage(previewFile) && previewUrl" :src="previewUrl" class="max-w-full rounded-lg border border-gray-200 dark:border-gray-800" />
            <iframe v-else-if="isPdf(previewFile) && previewUrl" :src="previewUrl" class="w-full h-[72vh] rounded-lg border border-gray-200 dark:border-gray-800"></iframe>
            <pre v-else-if="previewText !== null" class="text-xs text-gray-800 dark:text-gray-200 whitespace-pre-wrap font-mono bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg p-4 overflow-auto">{{ previewText }}</pre>
            <div v-else class="text-center text-sm text-gray-400 dark:text-gray-500 py-10">
              <UIcon :name="fileIcon(previewFile.content_type, previewFile.filename)" class="w-9 h-9 mx-auto text-gray-200 dark:text-gray-700" />
              <p class="mt-2">{{ $t('agentsPage.noInlinePreview') }}</p>
              <button v-if="previewUrl" class="mt-2 text-xs text-gray-700 dark:text-gray-300 underline" @click="downloadPreview">{{ $t('agentsPage.openFile') }}</button>
            </div>
          </div>
        </template>

        <template v-else-if="detail || creating">
          <!-- Header: status + actions -->
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
            <div class="flex items-center gap-2 min-w-0">
              <template v-if="creating">
                <span class="text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('agentsPage.newInstructionHeader') }}</span>
              </template>
              <template v-else>
                <!-- Pending state is authoritative from the live-hunk review
                     (/pending-changes), not the build-status heuristic, so the
                     badge never goes stale relative to the dots. -->
                <span class="w-1.5 h-1.5 rounded-full" :class="isPending(detail) ? 'bg-amber-400' : h.getStatusIconClass({ ...detail, current_build_status: null, current_build_id: null })"></span>
                <span class="text-xs font-medium text-gray-500 dark:text-gray-400">{{ isPending(detail) ? $t('agentsPage.pendingReview') : h.getStatusLabel({ ...detail, current_build_status: null, current_build_id: null }) }}</span>
                <!-- The review pane below runs headerless (`hide-header`), so its
                     count and bulk actions live here instead of stacking a second
                     "Pending review" bar under this one. -->
                <span v-if="reviewMode && reviewHunks.total" class="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums shrink-0">· {{ reviewHunks.total === 1 ? $t('agentsPage.changeCountOne', { n: reviewHunks.total }) : $t('agentsPage.changeCountMany', { n: reviewHunks.total }) }}</span>
              </template>
            </div>
            <div class="flex items-center gap-1.5">
              <span v-if="savingMeta" class="text-[10px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.saving') }}</span>
              <template v-if="reviewMode && canEditDetail && reviewHunks.total">
                <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-40 transition-colors" :disabled="reviewHunks.busy" @click="resolveAllHunks('reject')"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.rejectAll') }}</button>
                <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium hover:bg-emerald-100 dark:hover:bg-emerald-500/20 disabled:opacity-40 transition-colors" :disabled="reviewHunks.busy" @click="resolveAllHunks('accept')"><UIcon :name="reviewHunks.busy ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5', { 'animate-spin': reviewHunks.busy }]" />{{ $t('agentsPage.acceptAll') }}</button>
                <span class="w-px h-4 bg-gray-200 dark:bg-gray-700 mx-0.5"></span>
              </template>
              <button v-if="!creating && canEditDetail" class="h-7 w-7 rounded-md flex items-center justify-center transition-colors" :class="showHistory ? 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300' : 'text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70'" :title="$t('agentsPage.tipVersionHistory')" @click="toggleHistory()">
                <UIcon name="i-heroicons-clock" class="w-4 h-4" />
              </button>
              <template v-if="!editing && !diff">
                <button v-if="!creating" class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors" :title="$t('agentsPage.tipDownloadMarkdown')" @click="downloadMarkdown">
                  <UIcon name="i-heroicons-arrow-down-tray" class="w-4 h-4" />
                </button>
                <button v-if="canEditDetail" class="h-7 px-3 rounded-md border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="startEdit">{{ $t('agentsPage.edit') }}</button>
                <span v-else class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md text-[11px] text-gray-400 dark:text-gray-500" :title="$t('agentsPage.sharedManagedElsewhere')"><UIcon name="i-heroicons-lock-closed" class="w-3 h-3" />{{ $t('agentsPage.readOnly') }}</span>
              </template>
              <template v-else-if="!diff">
                <button v-if="!creating && canEditDetail" class="h-7 px-3 rounded-md text-red-600 dark:text-red-400 text-xs font-medium hover:bg-red-50 dark:hover:bg-red-500/10 disabled:opacity-50" :disabled="deleting || saving" :title="$t('agentsPage.tipDeleteInstruction')" @click="deleteInstruction"><span class="inline-flex items-center gap-1"><UIcon :name="deleting ? 'i-heroicons-arrow-path' : 'i-heroicons-trash'" :class="['w-3.5 h-3.5', { 'animate-spin': deleting }]" />{{ deleting ? $t('agentsPage.deleting') : $t('agentsPage.delete') }}</span></button>
                <span v-if="!creating && canEditDetail" class="w-px h-4 bg-gray-200 dark:bg-gray-700 mx-0.5"></span>
                <button class="h-7 px-3 rounded-md text-gray-500 dark:text-gray-400 text-xs hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="cancelEdit">{{ $t('agentsPage.cancel') }}</button>
                <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50" :disabled="saving" @click="save">{{ saving ? $t('agentsPage.saving') : (creating ? $t('agentsPage.create') : $t('agentsPage.save')) }}</button>
              </template>
            </div>
          </div>

          <!-- Pending changes are still being worked out for this instruction.
               Computing them means diffing every open suggestion against the
               current text, which takes real time on an instruction that has
               collected a lot of them — so say so instead of showing the plain
               text and then flipping into review mode without warning. -->
          <div v-if="canEditDetail && reviewLoading" data-testid="review-loading" class="px-6 py-2 flex items-center gap-2 text-[13px] text-gray-400 dark:text-gray-500 border-b border-gray-100 dark:border-gray-800">
            <Spinner class="w-3.5 h-3.5" /><span>{{ $t('agentsPage.loading') }}</span>
          </div>

          <!-- Per-hunk tracked-changes review (server-authoritative cherry-pick) -->
          <div v-if="reviewMode" class="flex-1 flex flex-col min-h-0">
            <InstructionTrackedChanges
              :key="detail.id"
              ref="trackedChangesRef"
              :instruction-id="detail.id"
              :can-approve="canEditDetail"
              hide-header
              @state="onReviewState"
              @changed="reloadAfterResolve"
              @empty="onReviewEmpty"
              @error="onReviewEmpty"
            />
          </div>

          <!-- Diff view (version compare) -->
          <div v-else-if="diff" class="flex-1 flex flex-col min-h-0">
            <div class="px-6 py-3 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
              <div class="flex items-center gap-2 min-w-0">
                <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="activeSuggestion?.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
                <span class="text-xs font-medium text-gray-700 dark:text-gray-300 truncate">{{ diff.title }}</span>
                <!-- Version compares read old → current, so name both ends:
                     green is what current has that this version didn't. -->
                <template v-if="diff.versionId">
                  <UIcon name="i-heroicons-arrow-right" class="w-3 h-3 text-gray-300 dark:text-gray-600 shrink-0 rtl:rotate-180" />
                  <span class="text-xs font-medium text-gray-700 dark:text-gray-300 shrink-0">{{ $t('agentsPage.current') }}</span>
                </template>
                <span v-if="diff.buildId && hunkCount" class="text-[11px] text-gray-400 dark:text-gray-500 shrink-0 tabular-nums">· {{ hunkCount === 1 ? $t('agentsPage.changeCountOne', { n: hunkCount }) : $t('agentsPage.changeCountMany', { n: hunkCount }) }}</span>
              </div>
              <div class="flex items-center gap-1.5">
                <template v-if="diff.buildId && canEditDetail">
                  <button class="inline-flex items-center px-2 py-1 rounded-md text-[11px] font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70 disabled:opacity-40 transition-colors" :disabled="resolving !== null || !hunkCount" @click="rejectAll">{{ resolving === 'reject-all' ? $t('agentsPage.rejecting') : $t('agentsPage.rejectAll') }}</button>
                  <button class="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-gray-50 dark:bg-gray-800 hover:bg-gray-100 dark:hover:bg-gray-800/70 border border-gray-150 dark:border-gray-700 text-[11px] font-medium text-gray-700 dark:text-gray-300 disabled:opacity-40 transition-colors" :disabled="resolving !== null || !hunkCount" @click="acceptAll"><UIcon :name="resolving === 'all' ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5 text-green-600', { 'animate-spin': resolving === 'all' }]" />{{ resolving === 'all' ? $t('agentsPage.accepting') : $t('agentsPage.acceptAll') }}</button>
                </template>
                <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800/70" :title="$t('agentsPage.tipClose')" @click="closeDiff"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
              </div>
            </div>
            <!-- Run this suggestion's evals (validate the candidate build) -->
            <div v-if="diff.buildId && canManageTests" class="px-6 py-3 border-b border-gray-100 dark:border-gray-800 bg-gray-50/40 dark:bg-gray-800/40 shrink-0">
              <div v-if="evalSuiteOptions.length" class="flex items-center gap-2">
                <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0" />
                <select v-model="selectedEvalSuiteId" class="h-7 flex-1 min-w-0 text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 outline-none">
                  <option v-for="o in evalSuiteOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
                <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1 shrink-0" :disabled="!selectedEvalSuiteId || evalRunning || !evalHasCases" @click="runSuggestionEval">
                  <UIcon :name="evalRunning ? 'i-heroicons-arrow-path' : 'i-heroicons-play'" :class="['w-3 h-3', { 'animate-spin': evalRunning }]" />
                  {{ evalRunning ? $t('agentsPage.running') : $t('agentsPage.runEval') }}
                </button>
              </div>
              <p v-else class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.noTestCasesHint') }}</p>

              <!-- Active / latest run progress -->
              <div v-if="evalActiveRun" class="mt-2 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-lg p-2.5 space-y-1.5">
                <div class="flex items-center justify-between">
                  <div class="flex items-center gap-1.5">
                    <UIcon :name="evalActiveRun.status === 'in_progress' ? 'i-heroicons-arrow-path' : (evalActiveRun.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle')" :class="['w-3.5 h-3.5', evalActiveRun.status === 'in_progress' ? 'text-blue-500 animate-spin' : (evalActiveRun.status === 'success' ? 'text-green-500' : 'text-red-500')]" />
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ evalPrettyStatus(evalActiveRun.status) }}</span>
                  </div>
                  <NuxtLink :to="`/evals/runs/${evalActiveRun.id}`" class="text-[10px] text-blue-500 dark:text-blue-400 hover:underline inline-flex items-center gap-0.5">{{ $t('agentsPage.viewDetails') }}<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" /></NuxtLink>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span class="px-1.5 py-0.5 rounded border bg-slate-50 dark:bg-slate-500/10 text-slate-600 dark:text-slate-400 border-slate-200 dark:border-slate-500/30">{{ $t('agentsPage.casesLabel') }}: {{ evalSummary.total }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400 border-green-200 dark:border-green-500/30">{{ $t('agentsPage.passLabel') }}: {{ evalSummary.passed }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400 border-red-200 dark:border-red-500/30">{{ $t('agentsPage.failLabel') }}: {{ evalSummary.failed }}</span>
                  <span v-if="evalSummary.inProgress" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-blue-50 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-500/30"><UIcon name="i-heroicons-arrow-path" class="w-2.5 h-2.5 animate-spin" />{{ $t('agentsPage.runningLabel') }}: {{ evalSummary.inProgress }}</span>
                </div>
                <div v-if="evalActiveRun.status === 'in_progress'" class="w-full bg-gray-100 dark:bg-gray-800 rounded-full h-1.5">
                  <div class="bg-blue-500 h-1.5 rounded-full transition-all duration-300" :style="{ width: `${evalSummary.progressPercent}%` }" />
                </div>
              </div>
            </div>

            <div ref="reviewScroll" class="flex-1 min-h-0 overflow-auto px-4 sm:px-8 py-6 max-w-3xl">
              <!-- Inline per-hunk review for suggestions. Clean tracked changes;
                   hover a change to reveal provenance + accept/reject. -->
              <template v-if="diff.buildId">
                <div v-if="!hunkCount" class="text-center text-xs text-gray-400 dark:text-gray-500 py-10">{{ $t('agentsPage.noRemainingChanges') }}</div>
                <div v-else dir="auto" class="text-[13px] leading-[1.6] whitespace-pre-wrap break-words text-gray-800 dark:text-gray-200" style="unicode-bidi: plaintext; text-align: start;">
                  <template v-for="(seg, si) in hunks" :key="si">
                    <span v-if="seg.kind === 'context'">{{ seg.text }}</span>
                    <span v-else class="group/h relative inline align-baseline rounded-[3px] transition-colors" :class="resolving === seg.idx ? 'bg-amber-100 dark:bg-amber-500/20' : 'hover:bg-amber-50 dark:hover:bg-amber-500/10'">
                      <template v-for="(op, oi) in seg.ops" :key="oi">
                        <del v-if="op.type === -1" class="text-rose-500/70 line-through decoration-rose-300 decoration-1">{{ op.text }}</del>
                        <ins v-else class="text-emerald-700 underline decoration-dotted decoration-emerald-400/70 underline-offset-[3px] decoration-1">{{ op.text }}</ins>
                      </template>
                      <!-- Floating control anchored just below the first line of
                           the change (near the hover point even for tall blocks). -->
                      <span v-if="canEditDetail" class="invisible opacity-0 group-hover/h:visible group-hover/h:opacity-100 transition-opacity absolute z-30 top-0 start-0 pt-[1.7em] cursor-default select-none whitespace-normal" @click.stop>
                        <span class="block w-max max-w-xs rounded-lg bg-white dark:bg-gray-900 shadow-md ring-1 ring-gray-200/70 dark:ring-gray-700 p-2">
                          <span class="flex items-center gap-1.5 mb-1.5">
                            <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="activeSuggestion?.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
                            <span class="text-[10px] text-gray-500 dark:text-gray-400 truncate">{{ activeSuggestion?.source === 'ai' ? $t('agentsPage.aiSuggestion') : $t('agentsPage.proposed') }}<template v-if="activeSuggestion?.created_at"> · {{ fmtDate(activeSuggestion.created_at) }}</template></span>
                            <button v-if="activeSuggestion?.completion_id || activeSuggestion?.report_id" type="button" class="ms-1 text-gray-300 dark:text-gray-600 hover:text-gray-600 dark:hover:text-gray-400 transition-colors" :title="$t('agentsPage.tipViewTrace')" @click.stop="openTrace(activeSuggestion)"><UIcon name="i-heroicons-arrows-pointing-out" class="w-3 h-3" /></button>
                          </span>
                          <!-- Brief evidence stamped by the AI when it proposed this change -->
                          <span v-if="activeSuggestion?.evidence" class="block mb-1.5 text-[10px] leading-snug text-gray-400 dark:text-gray-500 italic line-clamp-3">{{ activeSuggestion.evidence }}</span>
                          <span class="flex items-center gap-1.5">
                            <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium hover:bg-emerald-100 dark:hover:bg-emerald-500/20 disabled:opacity-40 transition-colors" :disabled="resolving !== null" @click.stop="acceptHunk(seg.idx)"><UIcon :name="resolving === seg.idx ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5', { 'animate-spin': resolving === seg.idx }]" />{{ $t('agentsPage.accept') }}</button>
                            <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-40 transition-colors" :disabled="resolving !== null" @click.stop="rejectHunk(seg.idx)"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.reject') }}</button>
                          </span>
                        </span>
                      </span>
                    </span>
                  </template>
                </div>
              </template>
              <!-- Read-only word diff for version comparisons -->
              <TrackedChangesView v-else :diff-ops="diffOps" />
            </div>
          </div>

          <div v-else class="flex-1 flex flex-col min-h-0">
            <!-- Pending-change banner: only when there are EFFECTIVE changes to
                 review (a rebased-no-op pending build must not raise it). -->
            <button v-if="canEditDetail && !editing && !creating && pendingViews.length" type="button" class="shrink-0 flex items-center gap-2 px-4 sm:px-8 py-2 border-b border-amber-100 dark:border-amber-500/30 bg-amber-50/60 dark:bg-amber-500/10 text-start hover:bg-amber-50 dark:hover:bg-amber-500/20 transition-colors" @click="viewSuggestion(pendingViews[0].build)">
              <span class="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0"></span>
              <span class="text-[12px] text-amber-800 dark:text-amber-300">{{ pendingViews.length === 1 ? $t('agentsPage.pendingOne') : $t('agentsPage.pendingMany', { n: pendingViews.length }) }}</span>
              <span class="ms-auto text-[11px] font-medium text-amber-700 dark:text-amber-400 inline-flex items-center gap-0.5 shrink-0">{{ $t('agentsPage.review') }}<UIcon name="i-heroicons-arrow-right" class="w-3 h-3 rtl:rotate-180" /></span>
            </button>
            <!-- Scrollable content: title + body -->
            <div class="flex-1 overflow-y-auto px-4 sm:px-8 py-6 w-full">
              <div class="max-w-3xl">
                <input v-if="editing" v-model="draft.title" dir="auto" :placeholder="$t('agentsPage.untitledInstruction')" class="w-full text-lg font-semibold text-gray-900 dark:text-white bg-transparent outline-none placeholder:text-gray-300 dark:placeholder:text-gray-600 mb-2" />
                <h2 v-else dir="auto" class="text-lg font-semibold text-gray-900 dark:text-white mb-2">{{ displayTitle(detail) }}</h2>
                <!-- Optional description (advertised for skills) -->
                <input v-if="editing" v-model="draft.description" dir="auto" :placeholder="$t('agentsPage.addDescriptionOptional')" class="w-full text-sm text-gray-600 dark:text-gray-300 bg-transparent outline-none placeholder:text-gray-300 dark:placeholder:text-gray-600 mb-4" />
                <p v-else-if="detail?.description" dir="auto" class="text-sm text-gray-500 dark:text-gray-400 mb-4">{{ detail.description }}</p>
                <div v-else class="mb-4"></div>
                <div class="prose-instruction">
                  <InstructionEditor :key="(detail?.id || 'new') + (editing ? '-edit' : '-view')" v-model="draft.text" mode="wysiwyg" :editable="editing" :data-source-ids="draft.data_source_ids" :is-all-data-sources="draft.data_source_ids.length === 0" :placeholder="$t('agentsPage.instructionPlaceholder')" @mention-selected="onEditorMention" />
                </div>
              </div>
            </div>
          </div>
          <!-- Frozen bottom panel: Details (compact, horizontal) / Analyze tabs.
               Sibling of the review / diff / normal view branches above so the
               instruction's metadata stays visible in all of them — a pending
               change swaps the body for tracked changes, not the whole page. -->
          <div v-if="detail || creating" class="shrink-0 border-t border-gray-100 dark:border-gray-800 bg-gray-50/40 dark:bg-gray-800/40">
            <div class="px-4 sm:px-8 flex items-stretch gap-1 border-b border-gray-100/70 dark:border-gray-800">
              <button type="button" class="flex items-center gap-1.5 py-2 text-[11px] font-medium border-b-2 -mb-px transition-colors" :class="bottomTab === 'details' ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-white' : 'border-transparent text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'" @click="bottomTab = 'details'"><UIcon name="i-heroicons-adjustments-horizontal" class="w-3.5 h-3.5" />{{ $t('agentsPage.details') }}</button>
              <button v-if="detail" type="button" class="flex items-center gap-1.5 py-2 ms-3 text-[11px] font-medium border-b-2 -mb-px transition-colors" :class="bottomTab === 'analyze' ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-white' : 'border-transparent text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'" @click="openAnalyzeTab"><UIcon name="i-heroicons-chart-bar" class="w-3.5 h-3.5" />{{ $t('agentsPage.analyze') }}</button>
            </div>

            <!-- Details: compact horizontal pills (inline-editable for admins) -->
            <div v-if="bottomTab === 'details'" class="px-4 sm:px-8 py-3 w-full overflow-y-auto" style="max-height:34vh">
              <div class="max-w-4xl flex flex-wrap items-center gap-1.5">
                <!-- Status -->
                <KSelect v-if="metaEditable" v-model="draft.status" :options="statusEditOpts" @update:modelValue="onMetaChange" />
                <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-medium">{{ h.getStatusLabel(visibleInstructionState(detail)) }}</span>
                <!-- Loading (skills are always 'Smart' — locked) -->
                <template v-if="metaEditable">
                  <KSelect v-if="draft.kind !== 'skill'" v-model="draft.load_mode" :options="loadEditOpts" icon="i-heroicons-bolt" @update:modelValue="onMetaChange" />
                  <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[11px] font-medium" :title="$t('agentsPage.smartTip')"><UIcon name="i-heroicons-bolt" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.smart') }}</span>
                </template>
                <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-medium"><UIcon name="i-heroicons-bolt" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />{{ h.getLoadModeLabel(detail.load_mode) }}</span>
                <!-- Category -->
                <KSelect v-if="metaEditable" v-model="draft.category" :options="categoryOpts" :placeholder="$t('agentsPage.general')" @update:modelValue="onMetaChange" />
                <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-medium">{{ h.formatCategory(detail.category) }}</span>
                <!-- Agents -->
                <KSelect v-if="metaEditable" v-model="draft.data_source_ids" :options="agentOptsForDraft" multiple :placeholder="$t('agentsPage.allAgentsPlaceholder')" icon="i-heroicons-cube" @update:modelValue="onMetaChange" />
                <template v-else>
                  <span v-if="(detail.data_sources || []).length === 0" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><UIcon name="i-heroicons-globe-alt" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.allAgentsPlaceholder') }}</span>
                  <span v-for="ds in detail.data_sources" :key="ds.id" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><DataSourceIcon :type="ds.type" :connector-key="ds.connector_key" :icon="ds.icon" class="w-3 h-3" />{{ ds.name }}</span>
                </template>
                <!-- Folder (cosmetic placement), one per scope. Picking a
                     folder here files the instruction without a drag; "Top
                     level" takes it back out. -->
                <template v-for="f in detailScopes" :key="'dir'+f.scope">
                  <KSelect v-if="canAddInstrFor(f.scope === GLOBAL_SCOPE ? undefined : f.scope)" :model-value="f.dirId" :options="f.options" icon="i-heroicons-folder" :placeholder="$t('agentsPage.topLevel')" @update:modelValue="(v: string) => detail && setPlacement(f.scope, detail.id, v || null)" />
                  <span v-else-if="f.path" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]" :title="f.scopeLabel + ' · ' + f.path">
                    <UIcon name="i-heroicons-folder" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ f.path }}
                  </span>
                </template>
                <!-- Primary: only when scoped to a single agent -->
                <KSelect v-if="metaEditable && singleAgentId && !creating" v-model="primarySelectValue" :options="primaryOpts" icon="i-heroicons-star" />
                <span v-else-if="!metaEditable && (detail?.primary_for || []).length" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 text-[11px] font-medium"><UIcon name="i-heroicons-star" class="w-3 h-3" />{{ $t('agentsPage.primary') }}</span>
                <!-- References -->
                <!-- Editors get the picker alone: it already renders what is
                     attached, so the chips beside it were the same references a
                     second time. Read-only viewers, who have no picker, keep
                     the chips. (Same shape as Labels, just below.) -->
                <span v-for="(r, i) in (!metaEditable ? (detail?.references || []) : [])" :key="'ref'+i" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-mono">
                  <UIcon :name="h.getRefIcon(r.object_type)" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ r.display_text || r.object_id }}
                </span>
                <KSelect v-if="metaEditable && refOptions.length" v-model="refIds" :options="refOptions" multiple summarize :placeholder="$t('agentsPage.addReference')" icon="i-heroicons-table-cells" @update:modelValue="onMetaChange" />
                <!-- Labels -->
                <KSelect v-if="metaEditable && labelOpts.length" v-model="draft.label_ids" :options="labelOpts" multiple :placeholder="$t('agentsPage.addLabel')" icon="i-heroicons-tag" @update:modelValue="onMetaChange" />
                <span v-for="l in (!metaEditable ? (detail.labels || []) : [])" :key="l.id" class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]">{{ l.name }}</span>
                <!-- Kind (last) -->
                <KSelect v-if="metaEditable" v-model="draft.kind" :options="kindOpts" :icon="draft.kind === 'skill' ? 'i-heroicons-sparkles' : 'i-heroicons-document-text'" @update:modelValue="onKindChange" />
                <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-medium"><UIcon :name="draft.kind === 'skill' ? 'i-heroicons-sparkles' : 'i-heroicons-document-text'" class="w-3 h-3 me-1 text-gray-400 dark:text-gray-500" />{{ draft.kind === 'skill' ? $t('agentsPage.skill') : $t('agentsPage.instruction') }}</span>
              </div>

              <!-- Advanced: run-mode + channel scoping (collapsed by default) -->
              <div class="mt-2 border-t border-gray-100/70 dark:border-gray-800 pt-2">
                <button type="button" class="flex items-center gap-1 text-[11px] font-medium text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="showAdvanced = !showAdvanced">
                  <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3 h-3 rtl:rotate-180" />
                  {{ $t('agentsPage.advanced') }}
                  <span v-if="advancedHasValues && !showAdvanced" class="ms-1 w-1.5 h-1.5 rounded-full bg-gray-400 dark:bg-gray-500"></span>
                </button>
                <div v-show="showAdvanced" class="mt-2 flex flex-col gap-2">
                  <!-- Modes (empty = all modes) -->
                  <div class="flex items-center gap-2">
                    <span class="text-[11px] text-gray-400 dark:text-gray-500 w-20 shrink-0">{{ $t('agentsPage.modes') }}</span>
                    <KSelect v-if="metaEditable" v-model="modeScope" :options="modeScopeOpts" :placeholder="$t('agentsPage.allModes')" icon="i-heroicons-rectangle-stack" @update:modelValue="onMetaChange" />
                    <template v-else>
                      <span v-if="!sanitizeModes(detail.applicable_modes).length" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><UIcon name="i-heroicons-rectangle-stack" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.allModes') }}</span>
                      <span v-for="m in sanitizeModes(detail.applicable_modes)" :key="'mode'+m" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><UIcon name="i-heroicons-rectangle-stack" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ modeLabel(m) }}</span>
                    </template>
                  </div>
                  <!-- Channels (empty = all channels) -->
                  <div class="flex items-center gap-2">
                    <span class="text-[11px] text-gray-400 dark:text-gray-500 w-20 shrink-0">{{ $t('agentsPage.channels') }}</span>
                    <KSelect v-if="metaEditable" v-model="draft.applicable_channels" :options="channelOpts" multiple :placeholder="$t('agentsPage.allChannels')" icon="i-heroicons-signal" @update:modelValue="onMetaChange" />
                    <template v-else>
                      <span v-if="!(detail.applicable_channels || []).length" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><UIcon name="i-heroicons-signal" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.allChannels') }}</span>
                      <span v-for="c in (detail.applicable_channels || [])" :key="'chan'+c" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[11px]"><UIcon name="i-heroicons-signal" class="w-3 h-3 text-gray-400 dark:text-gray-500" />{{ channelLabel(c) }}</span>
                    </template>
                  </div>
                </div>
              </div>
              <!-- Source + author/timestamps -->
              <div v-if="detail" class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400 dark:text-gray-500">
                <span class="inline-flex items-center gap-1"><UIcon :name="h.getSourceIcon(detail)" class="w-3 h-3" />{{ h.getSourceTooltip(detail) }}</span>
                <span v-if="detail.user" class="inline-flex items-center gap-1"><UIcon name="i-heroicons-user-circle" class="w-3 h-3" />{{ detail.user.name || detail.user.email }}</span>
                <span v-if="detail.created_at">{{ $t('agentsPage.created', { date: fmtDate(detail.created_at) }) }}</span>
                <span v-if="detail.updated_at && detail.updated_at !== detail.created_at">· {{ $t('agentsPage.updated', { date: fmtDate(detail.updated_at) }) }}</span>
              </div>
              <!-- Brief evidence stamped by the AI when it suggested the current version -->
              <div v-if="detail?.evidence" class="mt-1 text-[11px] text-gray-400 dark:text-gray-500 italic" :title="$t('agentsPage.evidenceTip')">
                <UIcon name="i-heroicons-light-bulb" class="w-3 h-3 inline-block align-[-2px] me-1" />{{ detail.evidence }}
              </div>
            </div>

            <!-- Analyze -->
            <div v-else-if="bottomTab === 'analyze'" class="px-6 py-3 w-full overflow-y-auto" style="max-height:42vh">
              <InstructionAnalysisPanel
                :related="analysis.related"
                :is-loading-related="analyzeLoading"
                :impacted-prompts="analysis.impactedPrompts"
                :is-loading-impact="analyzeLoading"
                :impact-score="analysis.impactScore"
                :impact-matched-count="analysis.impactMatched"
                :impact-total-count="analysis.impactTotal"
                section-max-height="16vh"
                @refresh="runAnalysis"
              />
            </div>
          </div>
        </template>

        <div v-else class="flex-1 flex items-center justify-center px-6">
          <div class="relative w-full max-w-lg h-72 overflow-hidden">
            <img src="/assets/empty-states/empty-integrations.png" alt="" class="absolute inset-x-0 bottom-8 w-full opacity-80 select-none pointer-events-none dark:hidden" />
            <div class="absolute inset-x-0 bottom-0 dark:top-0 flex flex-col items-center justify-center text-center px-6 pb-2">
              <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-sm ring-1 ring-gray-200/70 dark:ring-gray-700 shadow-sm"><UIcon name="i-heroicons-book-open" class="w-5 h-5 text-gray-400 dark:text-gray-500" /></div>
              <h3 class="mt-3 text-base font-medium text-gray-900 dark:text-white">{{ $t('agentsPage.configureAgents') }}</h3>
              <p class="mt-1.5 max-w-xs text-sm leading-relaxed text-gray-500 dark:text-gray-400">{{ agents.length ? $t('agentsPage.selectAgentHint') : $t('agentsPage.connectDataHint') }}</p>
              <div v-if="canCreateAgent" class="mt-4 flex items-center gap-2">
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors" @click="showNewAgent = true"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('agentsPage.createNewAgent') }}</button>
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-800 bg-white/70 dark:bg-gray-900/70 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 transition-colors" @click="connTargetAgentId = null; showAddConnection = true"><UIcon name="i-heroicons-circle-stack" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.connectData') }}</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Pane 3: version history only (hidden by default; toggle via clock) ── -->
      <!-- Mobile: no room for a third column — overlay the detail pane instead. -->
      <aside v-if="detail && canEditDetail && !creating && !reviewView && showHistory" class="flex flex-col bg-white dark:bg-gray-900" :class="isMobile ? 'absolute inset-0 z-20' : 'w-72 shrink-0 border-s border-gray-200 dark:border-gray-800'">
        <div class="h-11 px-3 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
          <span class="text-[12px] font-medium text-gray-700 dark:text-gray-300">{{ $t('agentsPage.history') }}</span>
          <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-300 dark:text-gray-600 hover:text-gray-600 dark:hover:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70" :title="$t('agentsPage.tipClose')" @click="showHistory = false"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
        </div>
        <div class="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
          <div v-if="versionsLoading" class="p-3 text-center text-[11px] text-gray-400 dark:text-gray-500">{{ $t('agentsPage.loading') }}</div>
          <div v-else-if="versions.length === 0" class="p-6 text-center text-[11px] text-gray-300 dark:text-gray-600">{{ $t('agentsPage.noHistory') }}</div>
          <button v-for="(v, i) in versions" :key="v.id" type="button"
                  class="group/h w-full text-start px-2.5 py-2 rounded-lg flex items-center justify-between transition-colors"
                  :class="diff && diff.versionId === v.id ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-50 dark:hover:bg-gray-800/50'"
                  @click="viewVersion(v, isCurrentVersion(v, i))">
            <div class="min-w-0">
              <!-- "current" = the LIVE version (the one in the main build), not
                   simply the newest row: a pending suggestion also writes a
                   version, so the top of this list is often an unpublished one. -->
              <div class="text-[13px] text-gray-800 dark:text-gray-200">v{{ v.version_number }}<span v-if="isCurrentVersion(v, i)" class="ms-1.5 text-[10px] font-medium text-green-600 dark:text-green-400">{{ $t('agentsPage.current') }}</span><span v-else-if="isUnpublishedVersion(i)" class="ms-1.5 text-[10px] font-medium text-amber-600 dark:text-amber-400">{{ $t('agentsPage.versionNotPublished') }}</span></div>
              <div class="text-[11px] text-gray-400 dark:text-gray-500">{{ fmtDate(v.created_at) }}</div>
            </div>
            <span v-if="!isCurrentVersion(v, i)" class="text-[11px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 opacity-0 group-hover/h:opacity-100 shrink-0" @click.stop="restore(v)">{{ $t('agentsPage.restore') }}</span>
          </button>
        </div>
      </aside>
    </div>

    <GitRepoModalComponent v-model="showGitModal" @changed="onGitChanged" />

    <!-- Org-wide instruction list + changelog. The tree browses one agent at a
         time; this is the view across all of them, and the only place
         instructions the live build isn't carrying are visible. -->
    <AllInstructionsModal
      v-model="showAllInstructions"
      :agents="agents"
      :initial-tab="allInstructionsTab"
      :initial-state="allInstructionsState"
      @open-instruction="onOpenFromAll"
    />

    <!-- Agent trace for a suggestion (opened from the inline review hover card) -->
    <TraceModal v-if="canViewConsole" v-model="showTraceModal" :report-id="traceReportId" :completion-id="traceCompletionId" />

    <!-- All connections (clean list) -->
    <UModal v-model="showConnectionsModal" :ui="{ width: 'sm:max-w-lg' }">
      <div class="p-5">
        <div class="flex items-center justify-between mb-3">
          <div>
            <div class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('agentsPage.connections') }}</div>
            <div class="text-xs text-gray-500 dark:text-gray-400">{{ connections.length === 1 ? $t('agentsPage.connectedSourceOne', { n: connections.length }) : $t('agentsPage.connectedSourceMany', { n: connections.length }) }}</div>
          </div>
          <button v-if="canCreateDataSource" type="button" class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="showConnectionsModal = false; connTargetAgentId = null; showAddConnection = true"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('agentsPage.new') }}</button>
        </div>
        <div class="max-h-[60vh] overflow-auto -mx-1 px-1 space-y-0.5">
          <button v-for="c in connections" :key="c.id" type="button" class="w-full flex items-center gap-3 px-2.5 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start transition-colors" @click="showConnectionsModal = false; openConnectionDetail(c)">
            <span class="relative inline-flex items-center justify-center w-8 h-8 rounded-md border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 shrink-0">
              <DataSourceIcon :type="c.type" :connector-key="c.connector_key" class="w-4 h-4" />
              <span class="absolute -bottom-0.5 -end-0.5 w-2 h-2 rounded-full ring-2 ring-white dark:ring-gray-900" :class="connDotClass(c)"></span>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{{ c.name }}</span>
              <span class="block text-xs text-gray-400 dark:text-gray-500 truncate">{{ c.type }}</span>
            </span>
            <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 text-gray-300 dark:text-gray-600 shrink-0 rtl:rotate-180" />
          </button>
        </div>
      </div>
    </UModal>

    <ConnectionDetailModal v-model="showConnectionModal" :connection="selectedConnection" @updated="onConnectionChanged" />

    <!-- Manage (link/unlink/edit/test) the connections attached to an agent.
         Opened from the agent overview and the Tables panel. Mirrors the
         legacy agents Tables view. -->
    <AgentConnectionsModal
      v-if="connModalAgentId"
      v-model="showConnModal"
      :ds-id="connModalAgentId"
      :connections="connModalConnections"
      @changed="onConnModalChanged"
    />
    <AddConnectionModal v-model="showAddConnection" @created="onConnCreated" />
    <NewAgentWizardModal v-model="showNewAgent" @finished="onNewAgentFinished" />
    <AddMCPModal v-model="showAddMCP" :existing-connections="mcpExistingConnections" @created="onConnCreated" />
    <AddCustomAPIModal v-model="showAddCustomAPI" :existing-connections="customApiExistingConnections" @created="onConnCreated" />
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="credsAgent" @saved="onCredsSaved" />
    <input ref="fileInputRef" type="file" multiple class="hidden" @change="onUploadInput" />

    <UModal v-model="showEditStarters" :ui="{ width: 'sm:max-w-2xl' }">
      <div class="p-5">
        <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('agentsPage.editStarters') }}</div>
        <div class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ $t('agentsPage.editStartersDesc') }}</div>
        <div class="mt-4 space-y-2 max-h-[60vh] overflow-auto pe-1">
          <div v-for="(item, idx) in editStarters" :key="idx" class="rounded-md border border-gray-100 dark:border-gray-800 p-2">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{{ $t('agentsPage.starterN', { n: idx + 1 }) }}</span>
              <button class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400" @click="removeStarter(idx)">{{ $t('agentsPage.remove') }}</button>
            </div>
            <div class="space-y-1">
              <input v-model="item.title" type="text" :placeholder="$t('agentsPage.starterTitlePlaceholder')" class="w-full h-8 text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 focus:outline-none focus:ring-2 focus:ring-blue-200" />
              <textarea v-model="item.prompt" rows="2" :placeholder="$t('agentsPage.starterPromptPlaceholder')" class="w-full text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200"></textarea>
            </div>
          </div>
          <button class="text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg px-2 py-1 hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="addStarter">{{ $t('agentsPage.addStarter') }}</button>
        </div>
        <div class="flex justify-end gap-2 mt-4">
          <button class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg" @click="showEditStarters = false">{{ $t('agentsPage.cancel') }}</button>
          <button class="px-3 py-1.5 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50" :disabled="savingStarters" @click="saveStarters">{{ savingStarters ? $t('agentsPage.saving') : $t('agentsPage.save') }}</button>
        </div>
      </div>
    </UModal>

    <!-- Self Learning (per-agent automation policy) -->
    <UModal v-model="showSelfLearning" :ui="{ width: 'sm:max-w-lg' }">
      <div class="p-5">
        <div class="flex items-center gap-2 mb-1">
          <UIcon name="i-heroicons-sparkles" class="w-4 h-4 text-blue-500" />
          <div class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('agentsPage.selfLearning') }}</div>
        </div>
        <AgentAutomationSettings v-if="showSelfLearning && agentView" :agent-id="agentView.agentId" @saved="onSelfLearningSaved" />
        <div class="flex justify-end mt-4">
          <button class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="showSelfLearning = false">{{ $t('agentsPage.close') }}</button>
        </div>
      </div>
    </UModal>

    <!-- Folder dialog: create / new subfolder / rename / delete (in-app, replaces
         the browser's native prompt/confirm). -->
    <UModal v-model="dirModal.open" :ui="{ width: 'sm:max-w-md' }">
      <form class="p-5" @submit.prevent="submitDirModal">
        <div class="flex items-center gap-2 mb-1">
          <UIcon :name="dirModal.mode === 'delete' ? 'i-heroicons-trash' : 'i-heroicons-folder'" :class="['w-4 h-4', dirModal.mode === 'delete' ? 'text-red-500' : 'text-gray-400 dark:text-gray-500']" />
          <div class="text-sm font-semibold text-gray-900 dark:text-white">{{ dirModalTitle }}</div>
        </div>
        <template v-if="dirModal.mode === 'delete'">
          <p v-if="dirModal.kind === 'suite'" class="text-xs text-gray-500 dark:text-gray-400 mt-2">{{ $t('agentsPage.suiteDeleteConfirm', { name: dirModal.suite?.name }) }}</p>
          <p v-else class="text-xs text-gray-500 dark:text-gray-400 mt-2">{{ $t('agentsPage.dirDeleteConfirm', { name: dirModal.dir?.name }) }}</p>
        </template>
        <template v-else>
          <input
            ref="dirModalInput"
            v-model="dirModal.name"
            type="text"
            :placeholder="dirModal.kind === 'suite' ? $t('agentsPage.suiteNamePrompt') : $t('agentsPage.dirNamePrompt')"
            maxlength="100"
            class="mt-3 w-full h-9 text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 rounded-md px-2.5 focus:outline-none focus:ring-2 focus:ring-blue-200 dark:focus:ring-blue-500/40"
          />
        </template>
        <div class="flex justify-end gap-2 mt-4">
          <button type="button" class="px-3 py-1.5 text-xs border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/50" @click="closeDirModal">{{ $t('agentsPage.cancel') }}</button>
          <button
            type="submit"
            :disabled="dirModal.busy || (dirModal.mode !== 'delete' && !dirModal.name.trim())"
            :class="['px-3 py-1.5 text-xs rounded-lg text-white disabled:opacity-50', dirModal.mode === 'delete' ? 'bg-red-600 hover:bg-red-700' : 'bg-blue-600 hover:bg-blue-700']"
          >{{ dirModal.busy ? $t('agentsPage.saving') : (dirModal.mode === 'delete' ? $t('agentsPage.delete') : (dirModal.mode === 'rename' ? $t('agentsPage.save') : $t('agentsPage.create'))) }}</button>
        </div>
      </form>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { h as createElement } from 'vue'
import InstructionTrackedChanges from '~/components/instructions/InstructionTrackedChanges.vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import PrimaryInstructionPicker from '~/components/instructions/PrimaryInstructionPicker.vue'
import AgentEvalsPanel from '~/components/AgentEvalsPanel.vue'
import TestCaseEditor from '~/components/monitoring/TestCaseEditor.vue'
import AgentSettingsPanel from '~/components/AgentSettingsPanel.vue'
import PublishStatusControl from '~/components/datasources/PublishStatusControl.vue'
import InstructionAnalysisPanel from '~/components/InstructionAnalysisPanel.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import AgentIconPicker from '~/components/AgentIconPicker.vue'
import KSelect from '~/components/KSelect.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import AllInstructionsModal from '~/components/instructions/AllInstructionsModal.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'
import AgentConnectionsModal from '~/components/AgentConnectionsModal.vue'
import AddConnectionModal from '~/components/AddConnectionModal.vue'
import NewAgentWizardModal from '~/components/NewAgentWizardModal.vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'
import ToolsSelector from '~/components/datasources/ToolsSelector.vue'
import AgentFilesPanel from '~/components/datasources/AgentFilesPanel.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import AddCustomAPIModal from '~/components/AddCustomAPIModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import TrackedChangesView from '~/components/instructions/TrackedChangesView.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import ReviewFeed from '~/components/ReviewFeed.vue'
import AgentAutomationSettings from '~/components/AgentAutomationSettings.vue'
import DiffMatchPatch from 'diff-match-patch'
import { useCan, useCanAny, useCanAll } from '~/composables/usePermissions'
import { useConnectionSignIn } from '~/composables/useConnectionSignIn'
import { getEffectiveStatus, statusDotClass } from '~/composables/useConnectionStatus'
import { useInstructionHelpers, type Instruction } from '~/composables/useInstructionHelpers'
import { useOrgSettings } from '~/composables/useOrgSettings'

const h = useInstructionHelpers()
const toast = useToast()
const { t } = useI18n()
// Training mode is the per-agent admin capability: gated on the org setting plus
// manage_instructions on the currently-open agent (a per-DS `manage` grant
// implies it; full_admin bypasses). Mirrors the backend gate.
const { isTrainingModeEnabled } = useOrgSettings()
const agentCanStartTraining = computed(() => {
  const id = agentView.value?.agentId
  return isTrainingModeEnabled.value && !!id && useCan('manage_instructions', { type: 'data_source', id })
})

// ── State ───────────────────────────────────────────────
// `allInstructions` is the LAZY row cache — it holds only the instruction rows
// for groups that have been expanded (global, skills, and per-agent), not the
// whole org. Badges come from `counts` instead, so the tree draws without
// loading every instruction on mount.
const allInstructions = ref<Instruction[]>([])
const instrLoading = ref(true)
// Aggregate badge counts (GET /api/instructions/counts): { global, skills,
// pending_total, by_agent: {id:n}, pending_by_agent: {id:true} }.
const counts = ref<any>({ global: 0, skills: 0, pending_total: 0, by_agent: {}, pending_by_agent: {} })
// Which lazy groups have had their rows loaded into `allInstructions`.
const loadedGroups = ref<Set<string>>(new Set())   // 'global' | 'skills' | <agentId>
const loadingGroups = ref<Set<string>>(new Set())
// Server-side cross-entity search results ({ agents, instructions }); non-null
// while a search is active, which swaps the tree for a flat results view.
const searchResults = ref<{ agents: any[]; instructions: Instruction[] } | null>(null)
const searching = ref(false)
const agents = ref<any[]>([])
// Show a spinner in the agents tree until the first load completes. Stays true
// through later refreshes, which update the list silently.
const agentsLoaded = ref(false)
// "Self Learning" per-agent automation modal (opened from the agent header).
const showSelfLearning = ref(false)
function onSelfLearningSaved() { toast.add({ title: t('agentsPage.toastSelfLearningSaved'), color: 'green' }) }
// Admin-only "show all" toggle: include every agent in the org, not just the
// caller's memberships. Re-fetches the agent list when flipped.
const showAllAgents = ref(false)
watch(showAllAgents, () => { fetchAgents() })
const labels = ref<{ id: string; name: string }[]>([])
const categories = ref<string[]>([])
const search = ref('')
// Server-side "Search everything": debounced call to /api/knowledge/search that
// returns matching agents AND instructions. While a query is present we render a
// flat grouped results view instead of the lazy tree (the tree only has loaded
// rows, so client-side search can't see everything).
let searchTimer: any = null
const runSearch = async (q: string) => {
  const term = (q || '').trim()
  if (!term) { searchResults.value = null; searching.value = false; return }
  searching.value = true
  try {
    const { data } = await useMyFetch<any>('/api/knowledge/search', { method: 'GET', query: { q: term, limit: 30 } })
    searchResults.value = { agents: data.value?.agents || [], instructions: data.value?.instructions || [] }
  } catch (e) { console.error(e); searchResults.value = { agents: [], instructions: [] } }
  finally { searching.value = false }
}
watch(search, (q) => {
  if (searchTimer) clearTimeout(searchTimer)
  // Typing a query leaves the "Pending changes" view (search takes over the pane).
  if (q.trim() && pendingView.value) pendingView.value = false
  if (!q.trim()) { searchResults.value = null; searching.value = false; return }
  searchTimer = setTimeout(() => runSearch(q), 250)
})

const fStatus = ref<string[]>([]); const fLoad = ref<string[]>([]); const fSource = ref<string[]>([]); const fCategory = ref<string[]>([])

const expanded = ref<Set<string>>(new Set())
const agentTables = ref<Record<string, { id: string; name: string; is_active: boolean }[]>>({})
// Active-table totals from the paginated response — the fetched rows are
// capped at one page, so counts must not rely on array length.
const agentTableTotals = ref<Record<string, number>>({})
const agentTools = ref<Record<string, any[]>>({})
const agentFiles = ref<Record<string, any[]>>({})
// File-source connections per agent, with their include-glob rules — shown in
// the Files tree next to uploads.
const FILE_CONN_TYPES = new Set(['network_dir', 's3', 'sharepoint', 'onedrive', 'google_drive', 'outlook_mail', 'gmail_mail'])
const agentFileConns = ref<Record<string, any[]>>({})
const agentLoaded = ref<Set<string>>(new Set())

// ── Instruction directories (cosmetic, per-agent folders) ────────────────
// Purely an organizational overlay for THIS tree — no AI semantics. Keyed by
// scope: an agent id, or 'global' for the Global instructions group. Each entry
// holds the folder rows plus the (instruction -> directory) placement edges for
// that scope. Placement is per-scope on purpose (instructions are m:n with
// agents), so the same instruction can live in different folders under two
// agents without collision.
type Dir = { id: string; name: string; data_source_id: string | null; parent_id: string | null; position: number }
const dirState = ref<Record<string, { dirs: Dir[]; placement: Record<string, string> }>>({})
const GLOBAL_SCOPE = 'global'
const scopeKey = (agentId?: string | null) => agentId || GLOBAL_SCOPE
const dirsForScope = (scope: string): Dir[] => dirState.value[scope]?.dirs || []
const placementFor = (scope: string): Record<string, string> => dirState.value[scope]?.placement || {}
// Child folders of `parentId` (null = top level) within a scope, ordered.
const childDirs = (scope: string, parentId: string | null): Dir[] => {
  const valid = new Set(dirsForScope(scope).map(d => d.id))
  return dirsForScope(scope)
    .filter(d => (d.parent_id || null) === (parentId || null) && (!d.parent_id || valid.has(d.parent_id)))
    .sort((a, b) => (a.position - b.position) || a.name.localeCompare(b.name))
}
// Instructions filed directly in `dirId`, from an already-filtered agent list.
const instrsInDir = (scope: string, dirId: string, list: Instruction[]): Instruction[] => {
  const pl = placementFor(scope)
  return list.filter(i => pl[i.id] === dirId)
}
// Instructions with no (valid) placement in this scope — shown at the root.
const rootInstrs = (scope: string, list: Instruction[]): Instruction[] => {
  const pl = placementFor(scope)
  const valid = new Set(dirsForScope(scope).map(d => d.id))
  return list.filter(i => !pl[i.id] || !valid.has(pl[i.id]))
}
const hasDirs = (scope: string) => dirsForScope(scope).length > 0
// "Finance/Definitions" for a directory id, walking up to the scope root.
const dirPath = (scope: string, dirId: string): string => {
  const byId = new Map(dirsForScope(scope).map(d => [d.id, d]))
  const parts: string[] = []
  const seen = new Set<string>()
  let cur = byId.get(dirId)
  while (cur && !seen.has(cur.id)) { seen.add(cur.id); parts.unshift(cur.name); cur = cur.parent_id ? byId.get(cur.parent_id) : undefined }
  return parts.join('/')
}
// Folder placement for the open instruction, one entry per scope it belongs to
// (placement is per-agent, so a multi-agent instruction can sit in a different
// folder under each). This drives a picker in the detail pane: filing and
// un-filing both work here, with no drag and no tree — the tree's drag/hover
// affordances are a shortcut, not the only way.
const detailScopes = computed(() => {
  const ins = detail.value
  if (!ins) return [] as { scope: string; scopeLabel: string; dirId: string; path: string; options: { value: string; label: string }[] }[]
  const agents = (ins.data_sources || [])
  const scopes = agents.length
    ? agents.map((d: any) => ({ scope: String(d.id), scopeLabel: d.name }))
    : [{ scope: GLOBAL_SCOPE, scopeLabel: t('agentsPage.globalInstructions') }]
  return scopes.flatMap(s => {
    const dirs = dirsForScope(s.scope)
    // No folders in this scope => nothing to choose between; stay out of the way.
    if (!dirs.length) return []
    const filed = placementFor(s.scope)[ins.id] || ''
    const dirId = dirs.some(d => d.id === filed) ? filed : ''
    const options = [{ value: '', label: t('agentsPage.topLevel') }].concat(
      dirs.map(d => ({ value: d.id, label: dirPath(s.scope, d.id) }))
          .sort((a, b) => a.label.localeCompare(b.label)))
    return [{ ...s, dirId, path: dirId ? dirPath(s.scope, dirId) : '', options }]
  })
})
// An instruction can be opened without ever expanding its group (search, a
// deep link, the review feed), so pull the folder overlay for its scopes on
// demand — otherwise the chip below would silently never appear.
const ensureDirScopes = async (ins: Instruction | null) => {
  if (!ins) return
  const agents = (ins.data_sources || [])
  const scopes = agents.length ? agents.map((d: any) => String(d.id)) : [GLOBAL_SCOPE]
  await Promise.all(scopes.filter(s => !dirState.value[s]).map(s => loadDirectories(s)))
}
// Fetch (or refresh) the folder tree + placements for one scope.
const loadDirectories = async (scope: string) => {
  try {
    const query: Record<string, any> = {}
    if (scope !== GLOBAL_SCOPE) query.data_source_id = scope
    const { data } = await useMyFetch<any>('/api/instructions/directories', { method: 'GET', query })
    const payload: any = data.value || {}
    const placement: Record<string, string> = {}
    for (const p of (payload.placements || [])) placement[String(p.instruction_id)] = String(p.directory_id)
    dirState.value = { ...dirState.value, [scope]: { dirs: (payload.dirs || payload.directories || []) as Dir[], placement } }
  } catch (e) { console.error(e) }
}
const scopeDataSourceId = (scope: string): string | null => (scope === GLOBAL_SCOPE ? null : scope)

// In-app folder dialog (replaces native prompt/confirm). One modal drives
// create / subfolder / rename / delete; `submitDirModal` dispatches by mode.
type DirModalMode = 'create' | 'subfolder' | 'rename' | 'delete'
// Eval suites reuse this dialog rather than window.prompt. They are the same
// interaction — name a container in the tree — and a native prompt next to a
// styled tree was jarring, unstyleable and gave no way to report a 403.
type DirModalKind = 'dir' | 'suite'
const dirModal = ref<{ open: boolean; mode: DirModalMode; kind: DirModalKind; scope: string; parentId: string | null; dir: Dir | null; suite: any | null; name: string; busy: boolean }>(
  { open: false, mode: 'create', kind: 'dir', scope: GLOBAL_SCOPE, parentId: null, dir: null, suite: null, name: '', busy: false }
)
const dirModalInput = ref<HTMLInputElement | null>(null)
const dirModalTitle = computed(() => {
  const suite = dirModal.value.kind === 'suite'
  switch (dirModal.value.mode) {
    case 'rename': return suite ? t('agentsPage.suiteRenameTitle') : t('agentsPage.dirRenameTitle')
    case 'subfolder': return t('agentsPage.dirNewSubfolderTitle')
    case 'delete': return suite ? t('agentsPage.suiteDeleteTitle') : t('agentsPage.dirDeleteTitle')
    default: return suite ? t('agentsPage.suiteNewTitle') : t('agentsPage.dirNewTitle')
  }
})
// Focus + select the name field when a name-entry modal opens.
watch(() => dirModal.value.open, (open) => {
  if (open && dirModal.value.mode !== 'delete') nextTick(() => { dirModalInput.value?.focus(); dirModalInput.value?.select() })
})
const openDirModal = (mode: DirModalMode, scope: string, opts: { parentId?: string | null; dir?: Dir; kind?: DirModalKind; suite?: any } = {}) => {
  const kind = opts.kind || 'dir'
  dirModal.value = {
    open: true, mode, kind, scope,
    parentId: opts.parentId ?? null,
    dir: opts.dir ?? null,
    suite: opts.suite ?? null,
    name: mode === 'rename' ? (kind === 'suite' ? (opts.suite?.name || '') : (opts.dir?.name || '')) : '',
    busy: false,
  }
}
const closeDirModal = () => { dirModal.value = { ...dirModal.value, open: false, busy: false } }
// Public entry points wired to the tree buttons — all open the modal now.
const newDirectory = (scope: string, parentId: string | null = null) =>
  openDirModal(parentId ? 'subfolder' : 'create', scope, { parentId })
const renameDirectory = (scope: string, dir: Dir) => openDirModal('rename', scope, { dir })
const deleteDirectory = (scope: string, dir: Dir) => openDirModal('delete', scope, { dir })
const submitDirModal = async () => {
  const m = dirModal.value
  const name = (m.name || '').trim()
  if (m.mode !== 'delete' && !name) return
  dirModal.value = { ...m, busy: true }
  try {
    if (m.kind === 'suite') {
      await submitSuiteModal(m, name)
      closeDirModal()
      return
    }
    if (m.mode === 'create' || m.mode === 'subfolder') {
      const body: any = { name, data_source_id: scopeDataSourceId(m.scope), parent_id: m.parentId }
      const { error } = await useMyFetch('/api/instructions/directories', { method: 'POST', body })
      if (error.value) throw new Error((error.value as any)?.data?.detail || 'Create failed')
      if (m.parentId) expanded.value = new Set(expanded.value).add('dir:' + m.scope + ':' + m.parentId)
      await loadDirectories(m.scope)
      toast.add({ title: t('agentsPage.toastDirCreated'), color: 'green' })
    } else if (m.mode === 'rename' && m.dir) {
      if (name !== m.dir.name) {
        const { error } = await useMyFetch(`/api/instructions/directories/${m.dir.id}`, { method: 'PATCH', body: { name } })
        if (error.value) throw new Error((error.value as any)?.data?.detail || 'Rename failed')
        await loadDirectories(m.scope)
      }
    } else if (m.mode === 'delete' && m.dir) {
      const { error } = await useMyFetch(`/api/instructions/directories/${m.dir.id}`, { method: 'DELETE' })
      if (error.value) throw new Error((error.value as any)?.data?.detail || 'Delete failed')
      await loadDirectories(m.scope)
      toast.add({ title: t('agentsPage.toastDirDeleted'), color: 'green' })
    }
    closeDirModal()
  } catch (e: any) {
    dirModal.value = { ...dirModal.value, busy: false }
    toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' })
  }
}
// Move an instruction into a folder (dirId), or to the scope root (dirId=null).
const setPlacement = async (scope: string, instructionId: string, dirId: string | null) => {
  // Optimistic: reflect the move immediately, revert on failure.
  const prev = { ...placementFor(scope) }
  const next = { ...prev }
  if (dirId) next[instructionId] = dirId; else delete next[instructionId]
  dirState.value = { ...dirState.value, [scope]: { dirs: dirsForScope(scope), placement: next } }
  try {
    const body: any = { directory_id: dirId, data_source_id: scopeDataSourceId(scope) }
    const { error } = await useMyFetch(`/api/instructions/${instructionId}/directory`, { method: 'PUT', body })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Move failed')
  } catch (e: any) {
    dirState.value = { ...dirState.value, [scope]: { dirs: dirsForScope(scope), placement: prev } }
    toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' })
  }
}
// Move a folder under another folder (or to root when targetId is null).
const moveDirectory = async (scope: string, dir: Dir, targetId: string | null) => {
  if (dir.id === targetId || (dir.parent_id || null) === (targetId || null)) return
  try {
    const { error } = await useMyFetch(`/api/instructions/directories/${dir.id}`, { method: 'PATCH', body: { parent_id: targetId } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Move failed')
    if (targetId) expanded.value = new Set(expanded.value).add('dir:' + scope + ':' + targetId)
    await loadDirectories(scope)
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}
// ── Drag state ────────────────────────────────────────────
// Only one drag at a time. Kind distinguishes dragging an instruction row from
// dragging a folder. Scope pins the drag to its agent/global group — cross-scope
// drops are rejected (placement is per-scope).
const drag = ref<{ kind: 'instr' | 'dir' | 'case'; id: string; scope: string } | null>(null)
const dropTarget = ref<string | null>(null)   // 'dir:<scope>:<id>' | 'root:<scope>'
// NOTE: nothing may MOUNT synchronously from dragstart. Vue flushes reactive
// effects at the microtask checkpoint — still inside the browser's drag-
// initiation sequence — and inserting a node that displaces the drag source
// makes Chromium abort the nascent drag with an immediate dragend (this killed
// dragging entirely when a drop strip used to mount above the rows here).
// State set below may only toggle classes on existing nodes.
const startDragInstr = (scope: string, insId: string, e: DragEvent) => {
  drag.value = { kind: 'instr', id: insId, scope }
  try { e.dataTransfer?.setData('text/plain', insId); if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move' } catch {}
}
const startDragDir = (scope: string, dirId: string, e: DragEvent) => {
  drag.value = { kind: 'dir', id: dirId, scope }
  try { e.dataTransfer?.setData('text/plain', dirId); if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move' } catch {}
}
const startDragCase = (scope: string, caseId: string, e: DragEvent) => {
  drag.value = { kind: 'case', id: caseId, scope }
  try { e.dataTransfer?.setData('text/plain', caseId); if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move' } catch {}
}
const endDrag = () => { drag.value = null; dropTarget.value = null }
// True if `nodeId` is `ancestorId` itself or nested somewhere beneath it.
const isDirDescendant = (scope: string, ancestorId: string, nodeId: string | null): boolean => {
  const byId = new Map(dirsForScope(scope).map(d => [d.id, d]))
  let cur: string | null = nodeId
  const seen = new Set<string>()
  while (cur && !seen.has(cur)) {
    if (cur === ancestorId) return true
    seen.add(cur)
    cur = byId.get(cur)?.parent_id || null
  }
  return false
}
// A drop is valid only within the same scope; a folder can't be dropped onto
// itself or into its own subtree (that would orphan the branch).
const canDrop = (scope: string, targetDirId: string | null): boolean => {
  const d = drag.value
  if (!d || d.scope !== scope) return false
  if (d.kind === 'dir') {
    if (d.id === targetDirId) return false
    if (targetDirId && isDirDescendant(scope, d.id, targetDirId)) return false
  }
  return true
}
const onDropInto = async (scope: string, targetDirId: string | null, key: string) => {
  dropTarget.value = null
  const d = drag.value
  if (!d || !canDrop(scope, targetDirId)) { endDrag(); return }
  const { kind, id } = d
  endDrag()
  if (kind === 'instr') await setPlacement(scope, id, targetDirId)
  else {
    const dir = dirsForScope(scope).find(x => x.id === id)
    if (dir) await moveDirectory(scope, dir, targetDirId)
  }
}
// Root drop zone ("move to no folder") — surfaced on the Instructions group
// header via rootDropzoneAttrs below.
const rootDropKey = (scope: string) => 'root:' + scope
const rootDropActive = (scope: string) => dropTarget.value === rootDropKey(scope) && canDrop(scope, null) && !!drag.value
const onRootDragover = (scope: string, e: DragEvent) => { if (canDrop(scope, null)) { e.preventDefault(); if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'; dropTarget.value = rootDropKey(scope) } }
const onRootDragleave = (scope: string) => { if (dropTarget.value === rootDropKey(scope)) dropTarget.value = null }
const onRootDrop = (scope: string) => onDropInto(scope, null, rootDropKey(scope))
// Drop-zone bindings for a scope's Instructions group HEADER: dropping on the
// header row itself means "move to top level", so the group name is a target
// too — not only the strip below it. Spread onto TreeGroup via v-bind.
const rootDropzoneAttrs = (scope: string) => ({
  dropActive: rootDropActive(scope),
  onDropzone: () => onRootDrop(scope),
  onDragover: (e: DragEvent) => onRootDragover(scope, e),
  onDragleave: () => onRootDragleave(scope),
})

// ── Eval suites tree ──────────────────────────────────────
// Suites render as folders under each agent's Evals group, and test cases as
// leaves inside them. The hierarchy already existed in the data
// (TestCase.suite_id is a plain FK) but had only ever been shown as a flat
// table with a "Suite" column.
//
// Deliberately FLAT: suites do not nest, so there is no parent_id, no cycle
// check and no recursion here. Instruction directories nest; test suites have
// not needed to.
type EvalSuite = { id: string; name: string; data_source_id?: string | null }
type EvalCase = {
  id: string; suite_id: string; status: string; auto_generated?: boolean
  prompt_json?: any; data_source_ids_json?: string[]
}
const evalTree = ref<Record<string, { suites: EvalSuite[]; cases: EvalCase[]; unfiled: EvalCase[]; loaded: boolean; loading: boolean }>>({})

const evalScopeState = (scope: string) =>
  evalTree.value[scope] || { suites: [], cases: [], unfiled: [], loaded: false, loading: false }
const suitesForScope = (scope: string) => evalScopeState(scope).suites
const casesInSuite = (scope: string, suiteId: string) =>
  evalScopeState(scope).cases.filter(c => String(c.suite_id) === String(suiteId))
const unfiledForScope = (scope: string) => evalScopeState(scope).unfiled
const evalCount = (scope: string) =>
  evalScopeState(scope).cases.length + evalScopeState(scope).unfiled.length

async function loadEvalTree(scope: string, opts: { force?: boolean } = {}) {
  const cur = evalTree.value[scope]
  if (cur?.loading) return
  if (cur?.loaded && !opts.force) return
  evalTree.value = { ...evalTree.value, [scope]: { suites: cur?.suites || [], cases: cur?.cases || [], unfiled: cur?.unfiled || [], loaded: !!cur?.loaded, loading: true } }
  try {
    // Suites are asked for by scope. Cases are asked for twice because the two
    // buckets below are answers to different questions, and asking one org-wide
    // question instead used to lose both once an org had more cases than the
    // page: by FILING (what sits in this agent's suites, which may include one
    // dragged in that targets someone else) and by TARGET (what this agent is
    // tested by, wherever it is filed). Both come back already filtered to what
    // this user may read.
    const scopeQ = scope === 'global' ? 'scope=global' : `data_source_id=${encodeURIComponent(scope)}`
    const sRes: any = await useMyFetch(`/api/tests/suites?limit=100&${scopeQ}`)
    const suites = ((sRes as any)?.data?.value || []) as EvalSuite[]
    const suiteIds = new Set(suites.map(x => String(x.id)))
    const suiteQ = suites.map(s => `suite_ids=${encodeURIComponent(String(s.id))}`).join('&')
    const [filedRes, targetedRes] = await Promise.all([
      suiteQ ? useMyFetch(`/api/tests/cases?limit=1000&${suiteQ}`) : Promise.resolve(null),
      useMyFetch(`/api/tests/cases?limit=1000&${scopeQ}`),
    ])
    const cases = (((filedRes as any)?.data?.value || []) as EvalCase[])
    // Cases that belong here by target but sit in a suite that lives elsewhere
    // — chiefly the org-wide Drafts bucket, where everything auto-drafted used
    // to land before suites had a home. Without this they would be invisible in
    // the tree even though their agent's manager owns them, and there would be
    // no way to drag them into a real suite.
    const belongsHere = (c: EvalCase) => {
      const ds = (c.data_source_ids_json || []).map(String)
      return scope === 'global' ? ds.length === 0 : (ds.length === 1 && ds[0] === scope)
    }
    const unfiled = (((targetedRes as any)?.data?.value || []) as EvalCase[])
      .filter(c => !suiteIds.has(String(c.suite_id)) && belongsHere(c))
    evalTree.value = { ...evalTree.value, [scope]: { suites, cases, unfiled, loaded: true, loading: false } }
  } catch (e) {
    console.error('Failed to load eval suites', e)
    evalTree.value = { ...evalTree.value, [scope]: { suites: [], cases: [], unfiled: [], loaded: true, loading: false } }
  }
}

// Re-file a case by dragging it onto another suite. Optimistic with rollback,
// mirroring setPlacement — the tree should move under the cursor, not after a
// round trip.
async function moveCaseToSuite(scope: string, caseId: string, suiteId: string) {
  const st = evalScopeState(scope)
  const prev = { cases: st.cases.map(c => ({ ...c })), unfiled: st.unfiled.map(c => ({ ...c })) }
  // Filing an unfiled case moves it between the two buckets, so both are
  // rewritten together — otherwise it would appear in the suite AND stay listed
  // as unfiled until the next load.
  const moved = st.cases.find(c => String(c.id) === String(caseId))
    || st.unfiled.find(c => String(c.id) === String(caseId))
  const next = st.cases
    .filter(c => String(c.id) !== String(caseId))
    .concat(moved ? [{ ...moved, suite_id: suiteId }] : [])
  const nextUnfiled = st.unfiled.filter(c => String(c.id) !== String(caseId))
  evalTree.value = { ...evalTree.value, [scope]: { ...st, cases: next, unfiled: nextUnfiled } }
  try {
    const res: any = await useMyFetch(`/api/tests/cases/${caseId}`, {
      method: 'PATCH', body: { suite_id: suiteId },
    })
    if (res?.error?.value) throw res.error.value
  } catch (e: any) {
    evalTree.value = { ...evalTree.value, [scope]: { ...evalScopeState(scope), ...prev } }
    const detail = e?.data?.detail || e?.message
    toast.add({ title: t('agentsPage.evalMoveFailed'), description: typeof detail === 'string' ? detail : undefined, color: 'red' })
  }
}

// A suite the caller may not manage still shows (they can read its cases), so
// creating and dropping are gated on the agent, matching the server.
const canManageEvalScope = (scope: string) =>
  scope === 'global' ? useCan('manage_evals') : useCan('manage_evals', { type: 'data_source', id: scope })

// Suites use the same in-app dialog as folders — see dirModal. A native
// window.prompt could not be styled, sat oddly beside the tree, and had nowhere
// to show the server's reason when a create is refused (an org-wide shelf takes
// org-level manage_evals).
const createSuiteIn = (scope: string) => openDirModal('create', scope, { kind: 'suite' })
const renameSuite = (scope: string, suite: any) => openDirModal('rename', scope, { kind: 'suite', suite })
const deleteSuite = (scope: string, suite: any) => openDirModal('delete', scope, { kind: 'suite', suite })

// Run a whole suite from the tree. POST /tests/suites/{id}/runs has existed and
// been permission-gated all along with no caller — running a suite meant
// opening it, ticking every case and using "run selected". The new run opens in
// the Evals panel for this scope, the same place a single-case run lands.
const runningSuiteId = ref('')
const pendingRunId = ref('')
async function runSuite(scope: string, suite: any) {
  if (runningSuiteId.value) return
  runningSuiteId.value = String(suite.id)
  try {
    const res: any = await useMyFetch(`/api/tests/suites/${suite.id}/runs`, { method: 'POST' })
    if (res?.error?.value) throw res.error.value
    const run = res?.data?.value
    if (!run?.id) throw new Error('No run returned')
    pendingRunId.value = String(run.id)
    if (scope === 'global') openGlobalEvals()
    else openPanel('evals', scope)
  } catch (e: any) {
    const detail = e?.data?.detail || e?.response?._data?.detail
    toast.add({ title: t('agentsPage.runSuiteFailed'), description: typeof detail === 'string' ? detail : undefined, color: 'red' })
  } finally {
    runningSuiteId.value = ''
  }
}

async function submitSuiteModal(m: any, name: string) {
  const fail = (e: any) => {
    const detail = e?.data?.detail || e?.response?._data?.detail || e?.message
    throw new Error(typeof detail === 'string' ? detail : 'Request failed')
  }
  if (m.mode === 'create') {
    const res: any = await useMyFetch('/api/tests/suites', {
      method: 'POST',
      body: { name, data_source_id: m.scope === 'global' ? null : m.scope },
    })
    if (res?.error?.value) fail(res.error.value)
  } else if (m.mode === 'rename' && m.suite) {
    if (name === m.suite.name) return
    const res: any = await useMyFetch(`/api/tests/suites/${m.suite.id}`, { method: 'PATCH', body: { name } })
    if (res?.error?.value) fail(res.error.value)
  } else if (m.mode === 'delete' && m.suite) {
    const res: any = await useMyFetch(`/api/tests/suites/${m.suite.id}`, { method: 'DELETE' })
    if (res?.error?.value) fail(res.error.value)
    // Deleting is partial whenever the suite held cases this user may not
    // destroy: those are reparented to Drafts and survive. Say so.
    const moved = Number((res?.data?.value as any)?.reparented || 0)
    if (moved > 0) {
      toast.add({
        title: t('agentsPage.toastSuiteDeleted'),
        description: t('agentsPage.suiteDeleteReparented', { count: moved }),
        color: 'green',
      })
    }
    if (evalCaseView.value?.scope === m.scope) closeEvalCase()
  }
  await loadEvalTree(m.scope, { force: true })
}

// Whole-row click expands AND opens the runs panel, matching Tables / Tools /
// Files (onPanelRowClick). Splitting it — chevron toggles, label opens — made
// Evals the only row in the tree that behaved differently from Instructions.
const onEvalsRowClick = (scope: string) => {
  const key = 'evals:' + scope
  const kind = scope === 'global' ? 'global-evals' : 'evals'
  const already = panelView.value?.kind === kind
    && (scope === 'global' || panelView.value?.agentId === scope)
  loadEvalTree(scope)
  if (already) { expand(key); return }
  if (!isOpen(key)) expand(key)
  if (scope === 'global') openGlobalEvals()
  else openPanel('evals', scope)
}

// ── Eval case detail (right pane, not a modal) ────────────
const evalCaseView = ref<null | { caseId: string | null; suiteId: string; scope: string }>(null)
const evalEditorRef = ref<any | null>(null)
const closeEvalCase = () => { evalCaseView.value = null }
const openEvalCase = (scope: string, c: EvalCase) => {
  clearRightPane()
  evalCaseView.value = { caseId: String(c.id), suiteId: String(c.suite_id), scope }
}
const openNewEvalCase = (scope: string, suiteId: string) => {
  clearRightPane()
  evalCaseView.value = { caseId: null, suiteId, scope }
}
const onEvalCaseSaved = async () => {
  if (evalCaseView.value) await loadEvalTree(evalCaseView.value.scope, { force: true })
}
const evalCasePromptOf = (c: EvalCase) => (c?.prompt_json?.content || '').trim() || t('agentsPage.untitledTest')

// file preview
const previewFile = ref<any | null>(null)
const previewUrl = ref<string | null>(null)
const previewText = ref<string | null>(null)
const previewLoading = ref(false)

const selectedId = ref<string | null>(null)
const detail = ref<Instruction | null>(null)
const editing = ref(false)
const creating = ref(false)
const saving = ref(false)
const deleting = ref(false)
const draft = reactive<{ title: string; description: string; text: string; kind: string; load_mode: string; status: string; category: string; data_source_ids: string[]; label_ids: string[]; references: any[]; applicable_modes: string[]; applicable_channels: string[] }>(
  { title: '', description: '', text: '', kind: 'instruction', load_mode: 'always', status: 'published', category: 'general', data_source_ids: [], label_ids: [], references: [], applicable_modes: [], applicable_channels: [] }
)
const kindOpts = computed(() => [{ value: 'instruction', label: t('agentsPage.optInstruction') }, { value: 'skill', label: t('agentsPage.optSkill') }])
// Mode/channel scoping options (empty selection = applies everywhere)
const modeOpts = computed(() => [{ value: 'chat', label: t('agentsPage.optModeChat') }, { value: 'training', label: t('agentsPage.optModeTraining') }])
// Modes are a tri-state, not a multi-select: with only chat and training left,
// "both checked" and "none checked" both mean "every mode", so a multi-select
// offers two ways to say the same thing. '' == applies everywhere.
const modeScopeOpts = computed(() => [
  { value: '', label: t('agentsPage.allModes') },
  ...modeOpts.value.map((o) => ({ value: o.value, label: o.label })),
])
// Retired modes must not survive a round-trip: KSelect's toggle spreads the
// existing array, so a stale value would be written straight back on any edit.
// Deny-list rather than allow-list — 'knowledge' is a real mode this picker
// never offered but the API can set, and dropping it here would destroy it.
const RETIRED_MODES = ['deep']
const sanitizeModes = (modes: any): string[] =>
  (Array.isArray(modes) ? modes : []).filter((m: string) => !RETIRED_MODES.includes(m))
const modeScope = computed<string>({
  get: () => (draft.applicable_modes || []).length === 1 ? draft.applicable_modes[0] : '',
  set: (v: string) => { draft.applicable_modes = v ? [v] : [] },
})
const channelOpts = computed(() => [{ value: 'app', label: t('agentsPage.optChannelApp') }, { value: 'slack', label: t('agentsPage.optChannelSlack') }, { value: 'teams', label: t('agentsPage.optChannelTeams') }, { value: 'email', label: t('agentsPage.optChannelEmail') }, { value: 'mcp', label: t('agentsPage.optChannelMcp') }])
const modeLabel = (v: string) => modeOpts.value.find(o => o.value === v)?.label || v
const channelLabel = (v: string) => channelOpts.value.find(o => o.value === v)?.label || v
// Reference options come from the selected agents' tables and their enabled
// connection tools (overlay-resolved by /data_sources/{id}/tools).
const refOptions = computed(() => {
  const opts: { value: string; label: string; type?: string; connectorKey?: string | null; iconToken?: string | null; heroicon?: string; objectType: string }[] = []
  for (const aid of draft.data_source_ids) {
    const a = agents.value.find(x => x.id === aid)
    // connector_key + icon ride along so a connector-backed agent shows its
    // brand logo and a custom agent emoji still wins — passing only `type` left
    // both rendering the generic type asset.
    for (const t of (agentTables.value[aid] || [])) opts.push({ value: t.id, label: t.name, type: a?.type, connectorKey: a?.connector_key, iconToken: a?.icon, objectType: 'datasource_table' })
    for (const tool of (agentTools.value[aid] || [])) {
      if (tool.is_enabled === false) continue
      // A tool is not a data source, so it gets a heroicon rather than none.
      opts.push({ value: String(tool.id), label: tool.name, heroicon: h.getRefIcon('connection_tool'), objectType: 'connection_tool' })
    }
  }
  // The picker is the only place an editor sees the attached references, so
  // every reference must resolve to an option — otherwise one attached to an
  // agent whose tables haven't loaded (or that has since left the instruction's
  // scope) would render as a bare uuid, and the refIds setter would write that
  // uuid back as its display_text.
  for (const r of draft.references) {
    const id = String(r.object_id)
    if (opts.some(o => o.value === id)) continue
    opts.push({ value: id, label: r.display_text || id, heroicon: h.getRefIcon(r.object_type), objectType: r.object_type })
  }
  return opts
})
const refIds = computed<string[]>({
  get: () => draft.references.map(r => String(r.object_id)),
  set: (ids) => {
    draft.references = ids.map(id => {
      const ex = draft.references.find(r => String(r.object_id) === id)
      if (ex) return ex
      const opt = refOptions.value.find(o => o.value === id)
      return { object_type: opt?.objectType || 'datasource_table', object_id: id, relation_type: 'scope', display_text: opt?.label || id }
    })
  },
})
// @-mentions in the editor must land in draft.references — the save body sends
// only draft.references, so an unhandled mention would never become a row.
const onEditorMention = (item: any) => {
  if (!item?.id || !item?.type) return
  if (draft.references.some(r => String(r.object_id) === String(item.id))) return
  draft.references.push({ object_type: item.type, object_id: String(item.id), relation_type: 'scope', display_text: item.name || String(item.id), column_name: null })
}
// Newly scoped agents need their tables/tools loaded for refOptions.
watch(() => [...draft.data_source_ids], (ids) => { ids.forEach(id => loadAgentMeta(id)) })

const showHistory = ref(false)
const versions = ref<any[]>([])
const versionsLoading = ref(false)
// The LIVE (published) text + version id of the open instruction, read
// authoritatively from /review-hunks (the is_main build's content). The
// instruction ROW (`detail.text` / the newest row in `versions`) is a cache
// that staged suggestion versions leave ahead of what is actually live, so
// neither is a trustworthy "current" for the history panel or a version diff.
const mainText = ref<string | null>(null)
const mainVersionId = ref<string | null>(null)
// What the user should understand as "the current text". Falls back to the row
// when the instruction has no main-build content yet (brand-new instruction
// that so far only exists in pending builds, or a legacy pre-build org).
const liveText = computed(() => (mainVersionId.value ? (mainText.value ?? '') : (detail.value?.text || '')))
// A version is "current" when it IS the live one — not merely the newest.
const isCurrentVersion = (v: any, i: number) =>
  mainVersionId.value ? String(v.id) === String(mainVersionId.value) : i === 0
// Index of the live version in the (newest-first) history list.
const currentVersionIndex = computed(() =>
  mainVersionId.value ? versions.value.findIndex(v => String(v.id) === String(mainVersionId.value)) : 0)
// Versions ABOVE the live one were written by a suggestion that was never
// published (still pending, or rejected) — say so rather than letting the
// newest row read as the live state.
const isUnpublishedVersion = (i: number) => currentVersionIndex.value > 0 && i < currentVersionIndex.value

// git
const gitRepos = ref<{ provider: string; repoName: string }[]>([])
const gitLastIndexed = ref<string | null>(null)
const showGitModal = ref(false)

// Org-wide instruction list + changelog (the "All instructions" modal).
// URL-bound so a link reproduces the exact view — support can point at
// "?instructions=all&state=not_live" instead of describing where to click.
const allRoute = useRoute()
const allRouter = useRouter()
const showAllInstructions = ref(false)
const allInstructionsTab = ref('list')
const allInstructionsState = ref('all')
// Includes instructions the live build isn't carrying — otherwise the button
// would quietly shrink at exactly the moment it should be drawing attention.
const totalInstructionCount = computed(() => counts.value?.total ?? 0)
const openAllInstructions = (tab = 'list', state = 'all') => {
  allInstructionsTab.value = tab
  allInstructionsState.value = state
  showAllInstructions.value = true
  allRouter.replace({ query: { ...allRoute.query, instructions: tab === 'log' ? 'changelog' : 'all' } })
}
const onOpenFromAll = (row: any) => {
  const agentId = (row?.data_sources || [])[0]?.id
  if (agentId) loadGroup(agentId)
  openInstruction(row)
}
watch(showAllInstructions, (open) => {
  if (open) return
  const q = { ...allRoute.query }
  delete q.instructions
  delete q.state
  allRouter.replace({ query: q })
})
onMounted(() => {
  const q = allRoute.query
  if (q.instructions === 'all' || q.instructions === 'changelog') {
    openAllInstructions(q.instructions === 'changelog' ? 'log' : 'list', String(q.state || 'all'))
  }
})

const statusOpts = computed(() => [{ value: 'published', label: t('agentsPage.optStatusActive') }, { value: 'draft', label: t('agentsPage.optStatusInactive') }, { value: 'pending_review', label: t('agentsPage.optStatusPending') }])
const statusEditOpts = computed(() => [{ value: 'published', label: t('agentsPage.optStatusActive') }, { value: 'draft', label: t('agentsPage.optStatusInactive') }])
// Filter list keeps "Off" so existing disabled rows remain findable.
const loadOpts = computed(() => [{ value: 'always', label: t('agentsPage.optLoadAlways') }, { value: 'intelligent', label: t('agentsPage.optLoadSmart') }, { value: 'disabled', label: t('agentsPage.optLoadOff') }])
// The editor no longer OFFERS "Off": it read as the harder off-switch while
// being the weaker one (the row still shows Active, stays in the build, and is
// still returned by the agent's search_instructions, which filters on status
// only). Inactive is the switch that actually takes an instruction out of play.
// "Off" stays listed while a row is already on it, so legacy rows render their
// real value instead of an empty select — and drop the option once moved off.
const loadEditOpts = computed(() => {
  const opts = [{ value: 'always', label: t('agentsPage.optLoadAlways') }, { value: 'intelligent', label: t('agentsPage.optLoadSmart') }]
  if (draft.load_mode === 'disabled') opts.push({ value: 'disabled', label: t('agentsPage.optLoadOff') })
  return opts
})
const sourceOpts = computed(() => [{ value: 'user', label: t('agentsPage.optSourceUser') }, { value: 'ai', label: t('agentsPage.optSourceAi') }, { value: 'git', label: t('agentsPage.optSourceGit') }])
const categoryOpts = computed(() => categories.value.filter(c => c !== 'dashboard').map(c => ({ value: c, label: h.formatCategory(c) })))
const agentOpts = computed(() => agents.value.map(a => ({ value: a.id, label: a.name, type: a.type })))
// The instruction may be scoped to agents missing from /data_sources/active
// (deactivated by a failed connection test, or not visible to this user).
// Merge those in from the instruction payload — which carries their names —
// so the chip never falls back to a raw id and the entry stays individually
// removable from the dropdown.
const agentOptsForDraft = computed(() => {
  // Only agents this user may author instructions on are offerable. Being able
  // to SEE an agent (membership/query access) is not authority to attach a rule
  // to it — the backend checks manage_instructions on every agent in the new
  // scope, so offering the rest just produces a 403 on save.
  const opts = agentOpts.value.filter(o => useCan('manage_instructions', { type: 'data_source', id: o.value }))
  // Agents already on the instruction stay in the list so their chip renders by
  // name rather than a raw id, and stays individually removable.
  for (const ds of ((detail.value?.data_sources || []) as any[])) {
    if (!opts.some(o => o.value === ds.id)) opts.push({ value: ds.id, label: ds.name, type: ds.type })
  }
  return opts
})

// Primary instruction toggle — only meaningful when the instruction is scoped to
// exactly one agent. `primary_for` (from the API) lists data sources whose
// primary_instruction_id points at this instruction.
const singleAgentId = computed(() => draft.data_source_ids.length === 1 ? draft.data_source_ids[0] : null)
const primaryOpts = computed(() => [{ value: 'primary', label: t('agentsPage.optPrimary') }, { value: 'standard', label: t('agentsPage.optNotPrimary') }])
const settingPrimary = ref(false)
const primarySelectValue = computed<string>({
  get: () => {
    const aid = singleAgentId.value
    if (!aid) return 'standard'
    return ((detail.value as any)?.primary_for || []).some((d: any) => String(d.id) === String(aid)) ? 'primary' : 'standard'
  },
  set: (val) => { setPrimaryForSingleAgent(val === 'primary') },
})
const setPrimaryForSingleAgent = async (makePrimary: boolean) => {
  const aid = singleAgentId.value
  const iid = detail.value?.id
  if (!aid || !iid || settingPrimary.value) return
  settingPrimary.value = true
  try {
    await useMyFetch(`/data_sources/${aid}`, { method: 'PUT', body: { primary_instruction_id: makePrimary ? iid : null } })
    const d = detail.value as any
    if (makePrimary) {
      if (!(d.primary_for || []).some((x: any) => String(x.id) === String(aid))) {
        d.primary_for = [...(d.primary_for || []), { id: aid, name: agents.value.find(a => a.id === aid)?.name || '' }]
      }
    } else {
      d.primary_for = (d.primary_for || []).filter((x: any) => String(x.id) !== String(aid))
    }
    // Keep the agent panel in sync if it's open for this agent.
    if (agentView.value?.agentId === aid) await refreshAgentDetail()
    toast.add({ title: t('agentsPage.toastSaved'), color: 'green' })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) } finally { settingPrimary.value = false }
}

// right-pane panel for Tables/Tools/Evals/Settings
const panelView = ref<null | { kind: 'tables' | 'tools' | 'files' | 'evals' | 'settings' | 'global-evals'; agentId: string }>(null)
const closePanel = () => { panelView.value = null }
const panelKindLabel = computed(() => ({ tables: t('agentsPage.tables'), tools: t('agentsPage.tools'), files: t('agentsPage.files'), evals: t('agentsPage.evals'), settings: t('agentsPage.settings'), 'global-evals': t('agentsPage.globalEvals') } as Record<string, string>)[panelView.value?.kind || ''] || '')
const panelAgent = computed(() => panelView.value ? agents.value.find(a => a.id === panelView.value!.agentId) : null)
const panelConnections = computed(() => {
  const a = panelAgent.value as any
  return (a?.connections || []).filter((c: any) => c.type === 'mcp' || c.type === 'custom_api')
})
const openPanel = (kind: 'tables' | 'tools' | 'files' | 'evals' | 'settings', agentId: string) => {
  clearRightPane()
  loadAgentMeta(agentId)
  panelView.value = { kind, agentId }
}
// Org-wide evals view — not bound to any agent.
const openGlobalEvals = () => {
  clearRightPane()
  panelView.value = { kind: 'global-evals', agentId: '' }
}
const onAgentSettingsUpdated = async () => { await fetchAgents(); if (agentView.value) refreshAgentDetail() }
const onAgentDeleted = async () => { closePanel(); await Promise.all([fetchAgents(), fetchConnections()]) }
// Row-click on Tables/Tools opens the editable panel immediately (like clicking
// an agent). Re-clicking the already-open row just collapses the tree node.
const onPanelRowClick = (kind: 'tables' | 'tools' | 'files', agentId: string) => {
  if (panelView.value?.kind === kind && panelView.value?.agentId === agentId) { expand(kind + ':' + agentId); return }
  if (!isOpen(kind + ':' + agentId)) expand(kind + ':' + agentId)
  openPanel(kind, agentId)
}

// ── Agent overview ──────────────────────────────────────
const agentView = ref<null | { agentId: string }>(null)
const agentDetail = ref<any | null>(null)
// The lightweight list entry for the open agent — carries list-only fields
// (admin_only) the full detail payload doesn't.
const agentListItem = computed(() => agents.value.find(a => a.id === agentView.value?.agentId) || null)
const agentReportCount = ref(0)
const agentViewName = computed(() => agentView.value ? (agents.value.find(a => a.id === agentView.value!.agentId)?.name || 'Agent') : '')
const agentCanUpdate = computed(() => canManageAgent(agentView.value?.agentId))
// inline-edit state
const editingDesc = ref(false); const descForm = ref(''); const descInputRef = ref<HTMLInputElement | null>(null)
const creatingPrimary = ref(false); const editingPrimary = ref(false)
const showEditStarters = ref(false); const editStarters = ref<{ title: string; prompt: string }[]>([]); const savingStarters = ref(false)

const agentDetailLoading = ref(false)
// Conversation starters are sourced from agent-scoped starter Prompts (not the
// legacy data_source.conversation_starters JSON). Each prompt's `text` is the
// "title\nprompt" string.
const starterPrompts = ref<any[]>([])
const closeAgentView = () => { agentView.value = null; agentDetail.value = null; agentDetailLoading.value = false; editingDesc.value = false; creatingPrimary.value = false; editingPrimary.value = false; starterPrompts.value = [] }
const refreshStarterPrompts = async () => {
  const id = agentView.value?.agentId; if (!id) { starterPrompts.value = []; return }
  try {
    const { data } = await useMyFetch<any>(`/prompts?data_source_id=${id}`)
    if (agentView.value?.agentId === id) starterPrompts.value = (data.value as any)?.prompts || []
  } catch { if (agentView.value?.agentId === id) starterPrompts.value = [] }
}
const refreshAgentDetail = async () => {
  const id = agentView.value?.agentId; if (!id) return
  try { const { data } = await useMyFetch<any>(`/data_sources/${id}`, { method: 'GET' }); if (agentView.value?.agentId === id) agentDetail.value = data.value } catch {} finally { if (agentView.value?.agentId === id) agentDetailLoading.value = false }
  refreshStarterPrompts()
}
const fetchAgentReports = async (id: string) => {
  agentReportCount.value = 0
  try { const { data } = await useMyFetch<any>('/reports', { method: 'GET', query: { data_source_id: id, limit: 1, filter: 'published' } }); agentReportCount.value = (data.value as any)?.total ?? 0 } catch {}
}
const onAgentPublishUpdated = (val: { publish_status: string; reliability_status?: string }) => {
  const apply = (o: any) => { if (!o) return; o.publish_status = val.publish_status; if (val.reliability_status !== undefined) o.reliability_status = val.reliability_status }
  apply(agentDetail.value)
  const a = agents.value.find(x => x.id === agentView.value?.agentId); if (a) { apply(a); agents.value = [...agents.value] }
}
const setAgentPublic = async (val: boolean) => {
  const id = agentView.value?.agentId; if (!id) return
  try {
    await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { is_public: val } })
    if (agentDetail.value) agentDetail.value.is_public = val
    const a = agents.value.find(x => x.id === id); if (a) { a.is_public = val; agents.value = [...agents.value] }
    toast.add({ title: val ? t('agentsPage.toastMadePublic') : t('agentsPage.toastMadePrivate'), color: 'green' })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}
// Change the agent's custom icon from the agent-view header (manage access only).
// `token` is an icon token ("emoji:…" | "type:…") or null to reset to default.
const setAgentIcon = async (token: string | null) => {
  const id = agentView.value?.agentId; if (!id) return
  const prev = agentDetail.value?.icon ?? null
  if (agentDetail.value) agentDetail.value.icon = token
  try {
    await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { icon: token } })
    const a = agents.value.find(x => x.id === id); if (a) { a.icon = token; agents.value = [...agents.value] }
    toast.add({ title: t('agentsPage.toastSaved'), color: 'green' })
  } catch (e: any) {
    if (agentDetail.value) agentDetail.value.icon = prev
    toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' })
  }
}
const openAgent = async (id: string) => {
  clearRightPane()
  agentView.value = { agentId: id }; agentDetail.value = null; agentDetailLoading.value = true; starterPrompts.value = []
  creatingPrimary.value = false; editingPrimary.value = false; editingDesc.value = false
  loadAgentMeta(id); fetchAgentReports(id); refreshAgentDetail(); fetchActivity(id)
}
// Close button: clear the view (the URL sync watcher drops the id from the URL).
const exitAgentView = () => { closeAgentView() }
const onAgentClick = (agent: any) => {
  if (needsSignIn(agent)) {
    // Managers/admins keep an admin path without personal credentials: expand
    // the node (which then shows only Settings) instead of forcing sign-in.
    // The detail pane stays closed — it assumes a queryable agent. Everyone
    // else goes to the sign-in flow, same as before.
    if (canManageAgent(agent.id)) { expand('agent:' + agent.id); return }
    openAgentTab(agent.id); return
  }
  // Re-clicking the already-open agent just collapses its tree node; keeps the pane.
  if (agentView.value?.agentId === agent.id) { expand('agent:' + agent.id); return }
  if (!isOpen('agent:' + agent.id)) expand('agent:' + agent.id)
  openAgent(agent.id)
}
const createReportForAgent = async (id: string) => {
  try {
    const { data, error } = await useMyFetch<any>('/reports', { method: 'POST', body: { title: 'New report', data_sources: [id] } })
    const rid = (data.value as any)?.id
    if (error.value || !rid) throw new Error('Failed to create report')
    navigateTo(`/reports/${rid}`)
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}
// Start a training session for an agent: a new report scoped to ONLY this
// agent/data source, switched to training mode, with a pre-filled (non-submitting)
// prompt — mirrors the legacy agents page.
const startTrainingSessionForAgent = async (agentId: string) => {
  if (!agentId) return
  const prompt = 'I need to update the instruction for this agent with '
  try {
    const { data, error } = await useMyFetch<any>('/reports', { method: 'POST', body: { title: 'Training session', data_sources: [agentId] } })
    const rid = (data.value as any)?.id
    if (error.value || !rid) throw new Error('Failed to create report')
    const { error: modeErr } = await useMyFetch(`/reports/${rid}`, { method: 'PUT', body: { mode: 'training' } })
    if (modeErr.value) throw new Error(String(modeErr.value))
    await navigateTo({ path: `/reports/${rid}`, query: { prompt } })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}
// description inline edit
const startEditDesc = () => { descForm.value = agentDetail.value?.description || ''; editingDesc.value = true; nextTick(() => descInputRef.value?.focus()) }
const cancelDesc = () => { editingDesc.value = false }
const saveDesc = async () => {
  if (!editingDesc.value) return
  editingDesc.value = false
  const id = agentView.value?.agentId; if (!id) return
  const v = descForm.value
  if (v === (agentDetail.value?.description || '')) return
  try { await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { description: v } }); if (agentDetail.value) agentDetail.value.description = v; toast.add({ title: t('agentsPage.toastSaved'), color: 'green' }) } catch { toast.add({ title: t('agentsPage.toastSaveDescFailed'), color: 'red' }) }
}
// primary instruction inline edit (clean editor: title + body + save/cancel)
const primaryDraft = reactive<{ title: string; text: string }>({ title: '', text: '' })
const primarySaving = ref(false)
const startCreatePrimary = () => { primaryDraft.title = agentDetail.value?.name ? agentDetail.value.name + ' - Main' : 'Main'; primaryDraft.text = ''; creatingPrimary.value = true; editingPrimary.value = false }
const onSelectExistingPrimary = async (instruction: any) => {
  const newId = instruction?.id; const aid = agentView.value?.agentId
  if (!newId || !aid) return
  try {
    await useMyFetch(`/data_sources/${aid}`, { method: 'PUT', body: { primary_instruction_id: newId } })
    await refreshAgentDetail()
    toast?.add?.({ title: t('agentsPage.toastSaved'), description: t('agentsPage.toastPrimaryUpdated') })
  } catch (e: any) { toast?.add?.({ title: t('agentsPage.toastError'), description: String(e?.message || e), color: 'red' }) }
}
const startEditPrimary = () => { const p = agentDetail.value?.primary_instruction; primaryDraft.title = p?.title || ''; primaryDraft.text = p?.text || ''; editingPrimary.value = true; creatingPrimary.value = false }
const cancelPrimary = () => { creatingPrimary.value = false; editingPrimary.value = false }
const savePrimary = async () => {
  if (primarySaving.value || !primaryDraft.text.trim()) return
  primarySaving.value = true
  const id = agentView.value?.agentId
  try {
    if (editingPrimary.value && agentDetail.value?.primary_instruction?.id) {
      const piid = agentDetail.value.primary_instruction.id
      await useMyFetch(`/api/instructions/${piid}`, { method: 'PUT', body: { title: primaryDraft.title || null, text: primaryDraft.text } })
    } else {
      const { data } = await useMyFetch<any>('/api/instructions', { method: 'POST', body: { title: primaryDraft.title || null, text: primaryDraft.text, status: 'published', load_mode: 'always', category: 'general', data_source_ids: id ? [id] : [] } })
      const newId = (data.value as any)?.id
      if (newId && id) await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { primary_instruction_id: newId } })
    }
    creatingPrimary.value = false; editingPrimary.value = false
    await Promise.all([refreshAgentDetail(), fetchAll()])
    toast.add({ title: t('agentsPage.toastSaved'), color: 'green' })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) } finally { primarySaving.value = false }
}
// conversation starters edit
const starterTitle = (cs: any) => typeof cs === 'string' ? (cs.split('\n')[0] || '') : (cs?.title || cs?.prompt || '')
// The prompt to submit: body for "title\nprompt" strings, else the title/whole string.
const starterPrompt = (cs: any) => {
  if (typeof cs === 'string') { const parts = cs.split('\n'); return (parts.slice(1).join('\n').trim() || parts[0] || '').trim() }
  return (cs?.prompt || cs?.title || '').trim()
}
// Click a starter → create a report for this agent and submit the prompt (like AgentFlyout).
const startingReport = ref(false); const startingStarterIdx = ref<number | null>(null)
const startReportWithStarter = async (agentId: string, cs: any, idx: number) => {
  if (startingReport.value) return
  const prompt = starterPrompt(cs); if (!prompt) return
  startingReport.value = true; startingStarterIdx.value = idx
  try {
    const { data, error } = await useMyFetch<any>('/reports', { method: 'POST', body: { title: 'untitled report', files: [], new_message: prompt, data_sources: agentId ? [agentId] : [] } })
    const rid = (data.value as any)?.id
    if (error.value || !rid) throw new Error('Failed to create report')
    await navigateTo({ path: `/reports/${rid}`, query: { new_message: prompt } })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) } finally { startingReport.value = false; startingStarterIdx.value = null }
}
const openEditStarters = () => {
  // Build the editor from the agent's starter Prompts (text = "title\nprompt").
  editStarters.value = starterPrompts.value.map((p: any) => {
    const s = String(p?.text ?? '')
    return { title: (s.split('\n')[0] || '').trim(), prompt: s.split('\n').slice(1).join('\n').trim() }
  })
  if (!editStarters.value.length) editStarters.value = [{ title: '', prompt: '' }]
  showEditStarters.value = true
}
const addStarter = () => editStarters.value.push({ title: '', prompt: '' })
const removeStarter = (i: number) => editStarters.value.splice(i, 1)
const saveStarters = async () => {
  if (savingStarters.value) return
  savingStarters.value = true
  const id = agentView.value?.agentId
  const conversation_starters = editStarters.value.map(s => `${(s.title || '').trim()}${s.prompt?.trim() ? '\n' + s.prompt.trim() : ''}`).filter(s => s.trim().length > 0)
  try {
    // Back the starters with the Prompt model (agent-scoped starter Prompts).
    // Replace-all: drop this agent's existing starter prompts, recreate from the editor.
    const { data: existing } = await useMyFetch(`/prompts?data_source_id=${id}`)
    for (const p of ((existing.value as any)?.prompts || [])) {
      await useMyFetch(`/prompts/${p.id}`, { method: 'DELETE' })
    }
    for (const text of conversation_starters) {
      await useMyFetch(`/prompts`, { method: 'POST', body: {
        text, title: (text.split('\n')[0] || '').slice(0, 60),
        scope: 'agent', is_starter: true, data_source_ids: [id],
      } })
    }
    await refreshStarterPrompts(); showEditStarters.value = false; toast.add({ title: t('agentsPage.toastSaved'), color: 'green' })
  }
  catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) } finally { savingStarters.value = false }
}
// reload tables / tools from the tree
const tablesRefreshKey = ref(0)
const reloadTables = async (id: string) => {
  try { await useMyFetch(`/data_sources/${id}/refresh_schema`, { method: 'GET' }) } catch {}
  agentLoaded.value.delete(id); await loadAgentMeta(id)
  tablesRefreshKey.value++  // force the open TablesSelector panel to re-fetch
  toast.add({ title: t('agentsPage.toastTablesReloaded'), color: 'green' })
}
const reloadTools = async (id: string) => {
  for (const c of (agents.value.find(a => a.id === id)?.connections || [])) { try { await useMyFetch(`/connections/${c.id}/refresh-tools`, { method: 'POST' }) } catch {} }
  agentLoaded.value.delete(id); await loadAgentMeta(id); toast.add({ title: t('agentsPage.toastToolsReloaded'), color: 'green' })
}

// ── File upload (per agent) ─────────────────────────────
const uploadingAgent = ref<string | null>(null)
const uploadTargetAgent = ref<string | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const triggerUpload = (agentId: string) => { uploadTargetAgent.value = agentId; nextTick(() => fileInputRef.value?.click()) }
// A proxy-level rejection (413) carries no JSON body, so name it rather than
// surfacing a bare status code.
const uploadErrorText = (e: any) => {
  const status = e?.statusCode || e?.response?.status
  if (status === 413) return t('agentsPage.uploadTooLarge')
  return e?.data?.detail || e?.statusMessage || e?.message || `HTTP ${status || '?'}`
}
const onUploadInput = async (e: Event) => {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  const agentId = uploadTargetAgent.value
  if (!files.length || !agentId) return
  uploadingAgent.value = agentId
  try {
    // useMyFetch resolves rather than throws on the client — it hands the
    // failure back in `error`. The try/catch below never fires for a rejected
    // upload, so check per file; otherwise a 500 still reported "Uploaded 1
    // file(s)" and the file simply never appeared in the tree.
    let ok = 0
    for (const file of files) {
      const fd = new FormData(); fd.append('file', file)
      const { error } = await useMyFetch(`/data_sources/${agentId}/files`, { method: 'POST', body: fd })
      if (error.value) {
        toast.add({ title: t('agentsPage.toastUploadFailed'), description: `${file.name} — ${uploadErrorText(error.value)}`, color: 'red' })
        continue
      }
      ok++
    }
    if (ok) toast.add({ title: t('agentsPage.toastUploaded', { n: ok }), color: 'green' })
    agentLoaded.value.delete(agentId)
    await loadAgentMeta(agentId)
    if (!isOpen('files:' + agentId)) expand('files:' + agentId)
  } catch (err: any) { toast.add({ title: t('agentsPage.toastUploadFailed'), description: err?.message, color: 'red' }) }
  finally { uploadingAgent.value = null; if (input) input.value = '' }
}

// Clear every right-pane mode (preview / diff / tables-tools panel / agent view / detail)
// ── Review feed (center-pane view) ──────────────────────
const reviewView = ref<null | { agentId: string | null }>(null)
const reviewCount = ref(0)
const fetchReviewCount = async () => {
  try { const { data } = await useMyFetch<any>('/api/review/count', { method: 'GET' }); reviewCount.value = data.value?.open || 0 } catch {}
}
const closeReview = () => { reviewView.value = null; fetchReviewCount() }
const clearRightPane = () => {
  closePreview(); closeDiff(); closePanel(); closeAgentView(); closeReview(); closeEvalCase()
  detail.value = null; selectedId.value = null; creating.value = false; editing.value = false
  versions.value = []; pendingBuilds.value = []; mainText.value = null; mainVersionId.value = null
}
const openReview = (agentId: string | null = null) => {
  clearRightPane()
  reviewView.value = { agentId }
}
// Open an instruction (from a Review item) and surface its pending diff.
// Resolve the instruction BEFORE swapping panes so the Review feed → detail
// transition happens in one tick (no flash of the agents list underneath). The
// Review pane stays mounted with a spinner overlay while we fetch.
const reviewNavLoading = ref(false)
const openInstructionFromReview = async (p: { instructionId: string; buildId?: string }) => {
  let ins = allInstructions.value.find(i => i.id === p.instructionId)
  if (!ins) {
    reviewNavLoading.value = true
    try { const { data } = await useMyFetch<any>(`/api/instructions/${p.instructionId}`, { method: 'GET' }); ins = data.value } catch {}
    reviewNavLoading.value = false
  }
  // openInstruction() closes the Review pane and sets detail synchronously.
  if (ins) openInstruction(ins)
  else closeReview()
}

// tree pane resize
const treeWidth = ref(300)
const clampTreeWidth = (w: number) => Math.min(600, Math.max(220, w))
const startTreeResize = (e: MouseEvent) => {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = treeWidth.value
  // In RTL the tree sits on the right with its resize handle on the inline-end
  // (left) edge, so a rightward drag must shrink the pane (and vice-versa).
  const dir = (typeof document !== 'undefined' && document.documentElement.getAttribute('dir') === 'rtl') ? -1 : 1
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  const onMove = (ev: MouseEvent) => { treeWidth.value = clampTreeWidth(startWidth + dir * (ev.clientX - startX)) }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

// version diff + pending suggestions
const pendingBuilds = ref<any[]>([])
// True while GET /instructions/{id}/review-hunks is in flight for the open row.
const reviewLoading = ref(false)
// Rows the AUTHORITATIVE per-hunk pass (/review-hunks, via loadPending) proved
// have nothing left to review this session. The counts sweep never diffs, so a
// drifted suggestion can still read "pending" there — when fetchCounts replaces
// pendingInstrIds wholesale, these verdicts must survive the overwrite, or the
// badge the user just watched clear pops straight back. An id leaves this set
// the moment the authoritative pass finds a real pending change again.
const verifiedNotPending = ref<Set<string>>(new Set())
// Global set of instruction ids that have a REAL pending change (a build that
// intentionally changed them vs its base, not stale-snapshot inheritance). The
// backend computes this so the count/dots match the per-instruction review.
const pendingInstrIds = ref<Set<string>>(new Set())
const fetchPendingMap = async () => {
  try {
    const { data } = await useMyFetch<any>('/api/instructions/pending-changes', { method: 'GET' })
    pendingInstrIds.value = new Set<string>((data.value?.instruction_ids || []).map((x: any) => String(x)))
  } catch {}
}

// ── "Pending changes" view ──────────────────────────────────────────────────
// Activated from the amber badge in the header. Swaps the lazy tree for a flat
// list of ONLY the instructions with a live pending change, grouped by agent.
// The set is computed server-side (cheap, access-scoped) via
// /api/instructions?pending_only=true — we do NOT lazy-load every agent.
const pendingView = ref(false)
const pendingRows = ref<Instruction[]>([])
const pendingLoading = ref(false)
const loadPendingChanges = async () => {
  if (!canApprove.value) {
    pendingRows.value = []
    return
  }
  pendingLoading.value = true
  try {
    const { items } = await fetchAllInstructions({
      pending_only: true, include_drafts: true, include_archived: true,
    })
    // The endpoint is visibility-scoped, not review-authority-scoped. Keep
    // drafts for instructions this user can merely VIEW out of client state;
    // only rows they can actually manage belong in the review surface.
    pendingRows.value = (items as Instruction[]).filter(canEditInstruction)
    // Keep the lazy cache + dot set in sync so opening a row from here behaves
    // identically to opening it from the tree.
    mergeRows(pendingRows.value)
  } catch (e) { console.error(e); pendingRows.value = [] }
  finally { pendingLoading.value = false }
}
// Group pending rows by agent (data source). Global instructions (no agent)
// collapse into one synthetic "Global" group. Each group keeps a stable label
// and icon so the flat list reads like the tree's agent sections.
const pendingGroups = computed(() => {
  const map = new Map<string, { id: string; name: string; type?: string; connector_key?: string; rows: Instruction[] }>()
  // The pending_only list is served by the same optimistic sweep as the dots —
  // hide rows the authoritative pass has since proven resolved.
  for (const ins of pendingRows.value.filter(r => canEditInstruction(r) && !verifiedNotPending.value.has(r.id))) {
    const dss = ins.data_sources || []
    if (!dss.length) {
      const key = '__global__'
      if (!map.has(key)) map.set(key, { id: key, name: t('agentsPage.globalInstructions'), type: undefined, rows: [] })
      map.get(key)!.rows.push(ins)
    } else {
      for (const ds of dss) {
        if (!map.has(ds.id)) {
          const agent = agents.value.find(a => a.id === ds.id)
          map.set(ds.id, { id: ds.id, name: ds.name, type: agent?.type || (ds as any).type, icon: agent?.icon ?? (ds as any).icon, connector_key: agent?.connector_key, rows: [] })
        }
        map.get(ds.id)!.rows.push(ins)
      }
    }
  }
  return Array.from(map.values()).sort((a, b) => a.name.localeCompare(b.name))
})
const enterPendingView = () => {
  search.value = ''
  pendingView.value = true
  loadPendingChanges()
}
const exitPendingView = () => { pendingView.value = false }
// Source label (who proposed the change) for a pending row.
const pendingSourceLabel = (ins: Instruction) => {
  const s = ins.pending_source || ins.source_type || 'user'
  if (s === 'ai') return t('agentsPage.pendingSourceAi')
  if (s === 'git') return t('agentsPage.pendingSourceGit')
  return ins.pending_created_by || t('agentsPage.pendingSourceUser')
}
const pendingSourceIcon = (ins: Instruction) => {
  const s = ins.pending_source || ins.source_type || 'user'
  if (s === 'ai') return 'i-heroicons-sparkles'
  if (s === 'git') return 'i-heroicons-code-bracket'
  return 'i-heroicons-user'
}
const pendingDate = (ins: Instruction) => {
  const raw = ins.pending_created_at || ins.updated_at
  if (!raw) return ''
  try { return new Date(raw).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) } catch { return '' }
}
const diff = ref<null | { title: string; label: string; original: string; modified: string; buildId?: string | null; versionId?: string | null }>(null)
const activeSuggestion = ref<any | null>(null)
const resolving = ref<any>(null)
const approving = ref<string | null>(null)
const discarding = ref<string | null>(null)
// connection modals
const showConnectionModal = ref(false)
const showConnectionsModal = ref(false)
const selectedConnection = ref<any>(null)
const showAddConnection = ref(false)
const showNewAgent = ref(false)
const showAddMCP = ref(false)
const showAddCustomAPI = ref(false)

// ── Per-user OAuth / OBO sign-in (user_required agents) ──────────────────────
// Replaces the old behaviour of popping the legacy /old_agents connection page.
// Mirrors the legacy /agents index: for OAuth-only connections jump straight to
// the provider; otherwise fall back to the credentials modal.
const signIn = useConnectionSignIn()
const showCredsModal = ref(false)
const credsAgent = ref<any>(null)
const connectingAgentId = ref<string | null>(null)
// The first user_required connection on an agent that still lacks credentials.
const pendingSignInConnection = (a: any) => (a?.connections || []).find((c: any) => c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials) || null
const connectAgent = async (agentId: string) => {
  const a = agents.value.find(x => x.id === agentId) || (agentDetail.value?.id === agentId ? agentDetail.value : null)
  if (!a) return
  const pending = pendingSignInConnection(a)
  if (pending) {
    connectingAgentId.value = agentId
    const result = await signIn.triggerUserSignIn(pending)
    if (result.redirecting) return // keep spinning; the page is navigating to the provider
    connectingAgentId.value = null
    if (result.error) toast.add({ title: t('agentsPage.toastSignInFailed'), description: result.error, color: 'red' })
  }
  // Non-OAuth (or OAuth that couldn't auto-redirect): collect creds in-app.
  credsAgent.value = a
  showCredsModal.value = true
}
// After credentials are saved, refresh the agent + repopulate its per-user table
// overlay (the shared-catalog reload now backfills it server-side).
const onCredsSaved = async () => {
  showCredsModal.value = false
  const id = credsAgent.value?.id
  await fetchAgents()
  if (id) {
    if (agentView.value?.agentId === id) await refreshAgentDetail()
    await reloadTables(id)
  }
}
// New agent wizard finished: refresh the agent list and open the new agent's page.
const onNewAgentFinished = async (id: string) => {
  showNewAgent.value = false
  if (!id) return
  await fetchAgents()
  expand('agent:' + id, true)
  openAgent(id)
}
const toolsRefreshKey = ref(0)
// When a connection is created from an agent's Tools panel, link it to that agent.
// Null when creating a brand-new agent (header "New › Agent").
const connTargetAgentId = ref<string | null>(null)
const openAddMcp = (agentId: string) => { connTargetAgentId.value = agentId; showAddMCP.value = true }
const openAddCustomApi = (agentId: string) => { connTargetAgentId.value = agentId; showAddCustomAPI.value = true }
// Offer only connections not already linked to the target agent — picking a
// linked one would be a guaranteed "already linked" error.
const linkedConnIds = computed(() => new Set(
  (((agents.value.find(a => a.id === connTargetAgentId.value) as any)?.connections) || []).map((c: any) => String(c.id))
))
const mcpExistingConnections = computed(() => connections.value.filter((c: any) => c.type === 'mcp' && !linkedConnIds.value.has(String(c.id))))
const customApiExistingConnections = computed(() => connections.value.filter((c: any) => c.type === 'custom_api' && !linkedConnIds.value.has(String(c.id))))
// New or existing connection picked: link it to the target agent (if any) and
// refresh its tools. useMyFetch resolves (never throws) on HTTP errors, so the
// link outcome must be read from response.error — a 403 here (e.g. the caller
// lacks `create_data_sources` on the connection) would otherwise vanish.
const onConnCreated = async (conn?: any, meta?: { existing?: boolean }) => {
  const aid = connTargetAgentId.value
  if (aid && conn?.id) {
    const link = await useMyFetch(`/data_sources/${aid}/connections/${conn.id}`, { method: 'POST' })
    if (link.error?.value) {
      const err: any = link.error.value
      toast.add({ title: t('agentsPage.connectionLinkFailed'), description: err?.data?.detail || err?.message, color: 'red' })
    } else {
      // The modal only toasts when it actually created/updated a connection;
      // for the "use existing" path the link IS the action — report it.
      if (meta?.existing) toast.add({ title: t('agentsPage.connectionLinked'), color: 'green' })
      await useMyFetch(`/connections/${conn.id}/refresh-tools`, { method: 'POST' })
    }
  }
  showAddMCP.value = false; showAddCustomAPI.value = false; showAddConnection.value = false
  if (aid) { agentLoaded.value.delete(aid); await loadAgentMeta(aid); if (agentView.value?.agentId === aid) await refreshAgentDetail() }
  await Promise.all([fetchAgents(), fetchConnections()]); toolsRefreshKey.value++
  connTargetAgentId.value = null
}
// Connection deleted from the Tools panel: just refresh the agent's tools.
const onToolsConnectionChanged = async () => {
  showAddMCP.value = false
  const aid = panelView.value?.agentId
  if (aid) { agentLoaded.value.delete(aid); await loadAgentMeta(aid) }
  await Promise.all([fetchAgents(), fetchConnections()]); toolsRefreshKey.value++
}

// "Manage connections" modal — opened from the agent overview and the Tables
// panel. Linking/unlinking a connection changes the agent's catalog, so refresh
// the agent list (connection chips) and force the TablesSelector to re-fetch.
const showConnModal = ref(false)
const connModalAgentId = ref<string | null>(null)
const connModalConnections = computed(() => {
  const id = connModalAgentId.value
  if (!id) return []
  return ((agents.value.find(a => a.id === id) as any)?.connections) || []
})
const openConnModal = (agentId: string) => { connModalAgentId.value = agentId; showConnModal.value = true }
const onConnModalChanged = async () => {
  const aid = connModalAgentId.value
  await fetchAgents()
  if (aid) { agentLoaded.value.delete(aid); await loadAgentMeta(aid) }
  tablesRefreshKey.value++
  if (agentView.value?.agentId === aid) await refreshAgentDetail()
}
// Top banner (license/onboarding) presence — so this full-height view subtracts
// the banner height instead of overflowing 40px below the viewport.
const { showTopBanner, bannerHeight } = useTopBanner()

// Mobile master → detail: on phones the tree and detail can't sit side by side,
// so we show one at a time. `detailOpen` is true whenever the detail pane has
// something to show; `backToTree` clears every detail state to return to the tree.
const { isMobile } = useMobile()
const detailOpen = computed(() => !!(
  reviewView.value || agentView.value || panelView.value ||
  previewFile.value || detail.value || creating.value
))
const backToTree = () => {
  closeReview()
  closeAgentView()
  closePanel()
  closePreview()
  closeDiff()
  detail.value = null
  selectedId.value = null
  creating.value = false
  editing.value = false
}
// The counts in the agent overview act as shortcuts into the tree sections,
// mirroring a click on the matching tree row. Tables/Tools/Files open their
// editable panel (which also expands the tree node); Instructions has no
// right-pane panel, so we expand its tree node instead. On mobile the tree is
// hidden behind the detail pane, so for Instructions we fall back to it.
const openAgentSection = (kind: 'tables' | 'tools' | 'files' | 'instructions', agentId: string) => {
  expand('agent:' + agentId, true)
  if (kind === 'instructions') {
    expand('instr:' + agentId, true)
    if (isMobile.value) backToTree()
  } else {
    onPanelRowClick(kind, agentId)
  }
}
// perms
const canApprove = computed(() => useCanAny('manage_instructions', 'data_source'))
// Editing/deleting a specific instruction requires manage_instructions on EVERY
// agent it is attached to (global => org-level), mirroring the backend's
// all-attached-agents rule. A per-agent manager viewing an instruction shared
// with agents they don't control sees it read-only. Ported from PR #732.
const canEditInstruction = (instr: any) => useCanManageInstruction(instr)
const canEditDetail = computed(() => canEditInstruction(detail.value))
// Populate the pending-review index only for users who can review something.
// Permissions arrive asynchronously after mount, so a watcher is more reliable
// than a one-shot mounted hook. Losing the permission clears draft-derived UI.
watch(canApprove, (allowed) => {
  if (allowed) loadPendingChanges()
  else {
    pendingRows.value = []
    pendingView.value = false
  }
}, { immediate: true })
// POST /instructions is manage_instructions (org-wide or per-agent) — the same
// tier that reviews suggestions, so the header "New" affordance follows it.
const canCreateInstruction = canApprove
// Tree "+" affordances mirror the backend create gates: global instructions
// need the org-level perm; per-agent rows also accept a per-agent grant.
const canAddInstrFor = (id?: string) => id ? useCan('manage_instructions', { type: 'data_source', id }) : useCan('manage_instructions')
// Building an AGENT: org-wide `create_data_source`, or per-connection
// `create_data_sources` on at least one connection (the route then enforces the
// specific connection). Checking only the org perm hid the affordance from
// users whose role grants exactly this.
const canCreateAgent = computed(() =>
  useCan('create_data_source') || useCanAny('create_data_sources', 'connection'))
// Creating a CONNECTION, and connecting a Git repo, are broader than building an
// agent on a connection someone already granted you — Git sync can rewrite every
// git-sourced instruction, and /api/git/* is org-gated server-side. These stay on
// the org permission; folding them into canCreateAgent would surface buttons a
// per-connection grantee cannot use.
const canCreateDataSource = computed(() => useCan('create_data_source'))
// Git is an INSTRUCTION source, not an agent-building capability: syncing a
// branch stages instruction content and disconnecting a repo soft-deletes every
// linked instruction. Mirrors the org-level gate on /api/git/*.
const canManageGit = computed(() => useCan('manage_instructions'))
// Org-wide data-source governance gates the "show all" toggle — admin-only,
// exactly like the legacy agents page (full_admin_access bypasses useCan, so
// this is true for full admins too; per-DS `manage` does NOT grant it).
const canViewAllAgents = computed(() => useCan('manage_connections'))
// True when the user runs a user_required agent via the connection's system
// (service-principal) creds — admin/owner fallback, no personal sign-in needed.
const usesServiceAccount = (a: any) => {
  if (!a) return false
  const conns = a.connections || []
  if (conns.length) return conns.some((c: any) => c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials && c.user_status?.effective_auth === 'system')
  return a.user_status?.has_user_credentials !== true && a.user_status?.effective_auth === 'system'
}
// Editing tables/tools requires manage on the data source (full_admin bypasses; otherwise a per-resource `manage` grant).
const canManageAgent = (id?: string) => id ? useCan('manage', { type: 'data_source', id }) : false
// The agent's Evals tab follows manage_evals on THAT agent, not the `manage`
// superset — a holder of a per-agent manage_evals grant could otherwise not
// reach the evals surface their grant is for. (`manage` implies manage_evals,
// so full agent managers still pass.)
const canManageAgentEvals = (id?: string) => id ? useCan('manage_evals', { type: 'data_source', id }) : false
// Global Evals is an org-admin surface, gated by the org-level manage_evals perm.
const canManageEvals = computed(() => useCan('manage_evals'))
const panelCanUpdate = computed(() => canManageAgent(panelView.value?.agentId))

const openConnectionDetail = (c: any) => { selectedConnection.value = c; showConnectionModal.value = true }
// Status dot for footer / list icons: derive the shared effective status
// (test result + indexing state) instead of rendering every active
// connection green. Inactive stays gray; unknown treated as healthy.
const connDotClass = (c: any) => {
  if (c?.is_active === false) return 'bg-gray-300'
  const s = getEffectiveStatus(c)
  return s === 'unknown' ? 'bg-green-500' : statusDotClass(s)
}
const onConnectionChanged = async () => { await Promise.all([fetchAgents(), fetchConnections()]) }
const loadPending = async (id: string) => {
  const stillAuthorized = () => selectedId.value === id
    && detail.value?.id === id
    && canEditDetail.value
  if (!stillAuthorized()) {
    pendingBuilds.value = []
    reviewLoading.value = false
    reviewEmpty.value = false
    reviewHunks.value = { total: 0, busy: false }
    mainText.value = null
    mainVersionId.value = null
    return
  }
  reviewEmpty.value = false
  reviewLoading.value = true
  // Authoritative: a "pending" instruction is one with live hunks in the
  // cherry-pick review (a fully-resolved suggestion build no longer counts).
  // Same response carries the authoritative live text/version (main build), so
  // the history panel and version diffs don't have to trust the row cache.
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${id}/review-hunks`, { method: 'GET' })
    // A manager can click a read-only row while this request is in flight.
    // Never let the previous instruction's draft response populate the new
    // selection, even for a single render frame.
    if (!stillAuthorized()) return
    pendingBuilds.value = (data.value?.suggestions || [])
    mainText.value = data.value?.main_text ?? null
    mainVersionId.value = data.value?.main_version_id ?? null
  }
  catch {
    if (stillAuthorized()) {
      pendingBuilds.value = []
      mainText.value = null
      mainVersionId.value = null
    }
  }
  finally { if (selectedId.value === id || !selectedId.value) reviewLoading.value = false }
  if (!stillAuthorized()) return
  // The tree's dots come from a deliberately cheap check that never runs the
  // per-hunk rebase, so a suggestion whose change is already applied can still
  // carry a dot. This IS the authoritative answer for this row — remember the
  // verdict (verifiedNotPending) so the next fetchCounts overwrite of
  // pendingInstrIds can't resurrect a badge this pass just cleared, and clear
  // the dot/badge now instead of leaving the tree disagreeing with what the
  // user is looking at.
  const verified = new Set(verifiedNotPending.value)
  if (pendingBuilds.value.length) verified.delete(id)
  else verified.add(id)
  verifiedNotPending.value = verified
  if (!pendingBuilds.value.length && pendingInstrIds.value.has(id)) {
    const next = new Set(pendingInstrIds.value)
    next.delete(id)
    pendingInstrIds.value = next
    if (counts.value?.pending_total) counts.value = { ...counts.value, pending_total: Math.max(0, counts.value.pending_total - 1) }
  }
}
const closeDiff = () => { diff.value = null; activeSuggestion.value = null; evalActiveRun.value = null; evalResults.value = []; stopEvalPoll() }

// ── Inline per-hunk review ─────────────────────────────────────────────────
// A "hunk" is a contiguous run of change ops (insertions/deletions) bounded by
// unchanged context. Each is independently acceptable/rejectable.
const hunks = computed(() => {
  const segs: any[] = []
  if (!diff.value || !diff.value.buildId) return segs
  let cur: any = null
  let idx = -1
  for (const op of diffOps.value) {
    if (op.type === 0) { segs.push({ kind: 'context', text: op.text }); cur = null }
    else { if (!cur) { idx++; cur = { kind: 'hunk', idx, ops: [] }; segs.push(cur) } cur.ops.push(op) }
  }
  return segs
})
const hunkCount = computed(() => hunks.value.filter((s: any) => s.kind === 'hunk').length)
// Synthesize a full text by applying ONLY the hunks in `acceptIdxs` onto the
// current text; all other hunks revert to current. (insert = keep added text,
// delete = drop removed text when accepted; the inverse when not.)
const buildHunkText = (acceptIdxs: Set<number>) => {
  let out = ''
  let h = -1
  let inHunk = false
  for (const op of diffOps.value) {
    if (op.type === 0) { out += op.text; inHunk = false; continue }
    if (!inHunk) { h++; inHunk = true }
    const accepted = acceptIdxs.has(h)
    if (op.type === 1) { if (accepted) out += op.text }
    else { if (!accepted) out += op.text }
  }
  return out
}
const doResolve = async (key: number | 'all' | 'reject-all', promoteText: string, remainingText: string) => {
  if (!detail.value || resolving.value !== null) return
  const buildId = diff.value?.buildId || null
  resolving.value = key
  const prevScroll = reviewScroll.value?.scrollTop ?? 0
  try {
    const { error } = await useMyFetch(`/api/instructions/${detail.value.id}/resolve`, { method: 'POST', body: { build_id: buildId, promote_text: promoteText, remaining_text: remainingText } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed to apply change')
    // Pull the new live text, then recompute remaining suggestions against it.
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'GET' })
    if (data.value) { detail.value = data.value; if (!editing.value) syncDraft(data.value) }
    await loadPending(detail.value.id)
    await loadVersions(detail.value.id)
    refreshLists()
    const stillPb = pendingBuilds.value.find((p: any) => p.build_id === buildId)
    if (stillPb) viewSuggestion(stillPb)
    else closeDiff()
    restoreScroll(prevScroll)
  } catch (e: any) {
    toast.add({ title: t('agentsPage.toastApplyFailed'), description: e?.message, color: 'red' })
  } finally { resolving.value = null }
}
const acceptHunk = (idx: number) => doResolve(idx, buildHunkText(new Set([idx])), diff.value?.modified || '')
const rejectHunk = (idx: number) => {
  const keep = new Set<number>()
  for (let i = 0; i < hunkCount.value; i++) if (i !== idx) keep.add(i)
  doResolve(idx, diff.value?.original || '', buildHunkText(keep))
}
const acceptAll = () => doResolve('all', diff.value?.modified || '', diff.value?.modified || '')
const rejectAll = () => doResolve('reject-all', diff.value?.original || '', diff.value?.original || '')

// ── Multi-source merged review: show ALL pending suggestions inline at once ───
const dmpLib = new (DiffMatchPatch as any)()
// Word-level diff: tokenize into words / whitespace / single symbols, map each
// unique token to a char, diff the encoded strings, then decode. A changed word
// surfaces as a whole-word replacement instead of scattered character fragments
// inside the word (e.g. "customer" → "CuStoMeR" is one swap, not "Cu·s·S·…").
function wordDiffOps(a: string, b: string): { type: number; text: string }[] {
  if (a === b) return a ? [{ type: 0, text: a }] : []
  const tokenize = (s: string) => s.match(/\w+|\s+|[^\w\s]/g) || []
  const tokenToChar = new Map<string, string>()
  const charToToken: string[] = []
  const encode = (toks: string[]) => toks.map((t) => {
    let c = tokenToChar.get(t)
    if (c === undefined) { c = String.fromCharCode(charToToken.length); tokenToChar.set(t, c); charToToken.push(t) }
    return c
  }).join('')
  const ea = encode(tokenize(a)), eb = encode(tokenize(b))
  const raw = dmpLib.diff_main(ea, eb, false)
  return raw.map((o: [number, string]) => {
    let text = ''
    for (let i = 0; i < o[1].length; i++) text += charToToken[o[1].charCodeAt(i)]
    return { type: o[0], text }
  })
}
function computeBuildHunks(current: string, modified: string) {
  if (current === modified || modified === '') return { ops: [], hunks: [] }
  const ops = wordDiffOps(current, modified)
  const hunks: any[] = []
  let cpos = 0, cur: any = null, idx = -1
  for (const op of ops) {
    if (op.type === 0) { cpos += op.text.length; cur = null; continue }
    if (!cur) { idx++; cur = { idx, ops: [], start: cpos, end: cpos }; hunks.push(cur) }
    cur.ops.push(op)
    if (op.type === -1) { cpos += op.text.length; cur.end = cpos }   // deletion consumes current
  }
  return { ops, hunks }
}
function applyHunks(ops: any[], acceptIdxs: Set<number>) {
  let out = '', h = -1, inHunk = false
  for (const op of ops) {
    if (op.type === 0) { out += op.text; inHunk = false; continue }
    if (!inHunk) { h++; inHunk = true }
    const acc = acceptIdxs.has(h)
    if (op.type === 1) { if (acc) out += op.text } else { if (!acc) out += op.text }
  }
  return out
}
// True iff `big` equals `small` plus pure insertions (small fully preserved,
// no deletions). Mirrors the backend `covers()` — used to recognise when one
// text already contains another so we don't re-derive (and duplicate) it.
function coversText(small: string, big: string): boolean {
  if (small === big) return false
  if (!small) return true
  const d = dmpLib.diff_main(small, big)
  for (const part of d) if (part[0] === -1) return false   // any deletion → not a pure superset
  return true
}
// Rebase a suggestion's *intended change* (base_text -> pending_text) onto the
// current text via a 3-way merge, so a still-valid sibling stays applicable
// after another sibling was accepted (current advanced past its base) and we
// never render spurious "re-add removed text" hunks. Falls back to the raw
// pending text when no base was recorded (legacy/new-from-scratch).
function rebaseSuggestion(baseText: string | null | undefined, pendingText: string, current: string): string {
  if (baseText == null) return pendingText            // no base → full snapshot
  if (pendingText === current) return current         // already applied → no-op
  if (baseText === pendingText) return current        // no intended change
  // The suggestion already incorporates everything in current plus more (it's a
  // pure additive superset of current) → the merged result IS the suggestion
  // text. Re-deriving via patch would re-insert the shared part ("Lorem ipsum"
  // already promoted, re-added again). This is the common sequential-edit case.
  if (coversText(current, pendingText)) return pendingText
  // Current already contains the whole suggestion (advanced past it) → no-op.
  if (coversText(pendingText, current)) return current
  if (baseText === current) return pendingText         // fresh → trivial
  try {
    const patches = dmpLib.patch_make(baseText, pendingText)
    if (!patches.length) return current
    const [merged] = dmpLib.patch_apply(patches, current)
    return merged
  } catch { return pendingText }
}
const mergedTextFor = (pb: any) => rebaseSuggestion(pb?.base_text, pb?.pending_text || '', detail.value?.text || '')
const pendingViews = computed(() => {
  if (!canEditDetail.value) return []
  const cur = detail.value?.text || ''
  return pendingBuilds.value
    .map((pb: any) => { const merged = rebaseSuggestion(pb.base_text, pb.pending_text || '', cur); return { build: pb, merged, ...computeBuildHunks(cur, merged) } })
    .filter((v: any) => v.hunks.length > 0)   // drop suggestions already applied to current (rebased no-op)
})
// Enter the per-hunk review when the instruction has pending suggestion builds.
// The review component is server-authoritative; if the server finds no live
// hunks it emits `empty`, and we fall back to the plain text view.
const trackedChangesRef = ref<any>(null)
const reviewEmpty = ref(false)
const onReviewEmpty = () => { reviewEmpty.value = true }
// The review component runs headerless here (this pane already has a status
// bar); it reports its hunk count / in-flight state so the header can render
// the count and the bulk actions, and delegates them back to it.
const reviewHunks = ref<{ total: number; busy: boolean }>({ total: 0, busy: false })
const onReviewState = (s: { total: number; busy: boolean }) => { reviewHunks.value = s }
const resolveAllHunks = (mode: 'accept' | 'reject') => trackedChangesRef.value?.resolveAll?.(mode)
const reviewMode = computed(() => canEditDetail.value && !!detail.value && !creating.value && !editing.value && !(diff.value && diff.value.versionId) && pendingBuilds.value.length > 0 && !reviewEmpty.value)
const mergedReviewCount = computed(() => pendingViews.value.reduce((n: number, v: any) => n + v.hunks.length, 0))
// Interleave every build's hunks onto the current text, ordered by position.
const mergedSegments = computed(() => {
  const cur = detail.value?.text || ''
  const all: any[] = []
  const n = pendingViews.value.length
  pendingViews.value.forEach((v: any, vi: number) => {
    // Recency rank — a newer suggestion wins when two overlap the same span.
    // build_number is monotonic; fall back to list order (API returns newest
    // first, so index 0 is the most recent).
    const rank = v.build.build_number ?? (n - vi)
    for (const h of v.hunks) all.push({ ...h, buildId: v.build.build_id, build: v.build, buildOps: v.ops, merged: v.merged, rank })
  })
  // Two suggestions touching the same span of current text can't both render
  // cleanly (e.g. older word-swaps inside a line the newest suggestion deletes
  // wholesale). Claim spans NEWEST-first so the latest intent wins, dropping the
  // overlapping older hunks; then render what's kept in document order.
  const kept: any[] = []
  const claimed: [number, number][] = []
  for (const h of [...all].sort((a, b) => (b.rank - a.rank) || (a.start - b.start))) {
    const s = h.start, e = Math.max(h.end, h.start)
    const isPoint = e === s
    const clash = claimed.some(([cs, ce]) => (isPoint ? (s > cs && s < ce) : (s < ce && e > cs)))
    if (clash) continue
    claimed.push([s, e]); kept.push(h)
  }
  kept.sort((a, b) => a.start - b.start || 0)
  const segs: any[] = []
  let cursor = 0
  for (const h of kept) {
    if (h.start < cursor) continue
    if (h.start > cursor) segs.push({ kind: 'context', text: cur.slice(cursor, h.start) })
    segs.push({ kind: 'hunk', ...h })
    cursor = Math.max(cursor, h.end)
  }
  if (cursor < cur.length) segs.push({ kind: 'context', text: cur.slice(cursor) })
  return segs
})
const highlightBuild = ref<string | null>(null)
const reloadAfterResolve = async () => {
  if (!detail.value) return
  const id = detail.value.id
  const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${id}`, { method: 'GET' })
  // Rejecting the suggestion that would have INTRODUCED an instruction retires
  // it server-side — there is no row left to reload. Keeping the pane open on
  // the stale copy would leave the reviewer reading an instruction the tree no
  // longer lists. Only a 404 means that: on a transient failure (500, network
  // blip, expired token) the row still exists, so hold the pane rather than
  // throwing the reviewer out of a review they are in the middle of.
  if (!data.value) {
    const code = (error.value as any)?.statusCode ?? (error.value as any)?.status
    if (code !== 404) return
    allInstructions.value = allInstructions.value.filter(i => i.id !== id)
    editing.value = false; detail.value = null; selectedId.value = null
    versions.value = []; mainText.value = null; mainVersionId.value = null
    pendingBuilds.value = []; closeDiff()
    refreshLists(); fetchPendingMap(); fetchCounts(); fetchReviewCount()
    return
  }
  detail.value = data.value; if (!editing.value) syncDraft(data.value)
  await loadPending(id); await loadVersions(id); refreshLists(); fetchReviewCount()
}
// Scroll container of the review/diff pane — preserved across resolve reloads so
// accepting a change doesn't jump the page back to the top.
const reviewScroll = ref<HTMLElement | null>(null)
const doResolveFor = async (buildId: string, promoteText: string, remainingText: string, key: string) => {
  if (!detail.value || resolving.value !== null) return
  resolving.value = key
  const prevScroll = reviewScroll.value?.scrollTop ?? 0
  try {
    const { error } = await useMyFetch(`/api/instructions/${detail.value.id}/resolve`, { method: 'POST', body: { build_id: buildId, promote_text: promoteText, remaining_text: remainingText } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    await reloadAfterResolve()
    restoreScroll(prevScroll)
  } catch (e: any) { toast.add({ title: t('agentsPage.toastApplyFailed'), description: e?.message, color: 'red' }) } finally { resolving.value = null }
}
// Restore the review pane's scroll across re-renders: once after Vue patches,
// then again on the next frame after layout settles (content height changed).
const restoreScroll = (top: number) => {
  nextTick(() => {
    if (reviewScroll.value) reviewScroll.value.scrollTop = top
    requestAnimationFrame(() => { if (reviewScroll.value) reviewScroll.value.scrollTop = top })
  })
}
function hunkCountOf(ops: any[]) { let hc = 0, inH = false; for (const o of ops) { if (o.type === 0) inH = false; else { if (!inH) { hc++; inH = true } } } return hc }
// Set of this build's hunks EXCEPT the one being acted on — what stays pending.
function keepAllBut(ops: any[], idx: number) { const keep = new Set<number>(); const hc = hunkCountOf(ops); for (let i = 0; i < hc; i++) if (i !== idx) keep.add(i); return keep }
// Hunks/ops are computed against the rebased ("merged") text.
// Accept: promote = current + the accepted hunk; remaining = the build's FULL
//   rebased target (merged). On reload the accepted hunk is already in current,
//   so the rebase shows only the still-pending hunks (and a single-hunk build
//   resolves out cleanly). Using "keep all but this hunk" here is WRONG: for a
//   deletion it reverts the just-accepted removal and re-adds the text.
// Reject: main is unchanged (promote = current); the build keeps proposing the
//   OTHER hunks (keepAllBut) and drops the rejected one.
const acceptMergedHunk = (seg: any) => doResolveFor(seg.buildId, applyHunks(seg.buildOps, new Set([seg.idx])), seg.merged ?? mergedTextFor(seg.build), `${seg.buildId}:${seg.idx}`)
const rejectMergedHunk = (seg: any) => {
  doResolveFor(seg.buildId, detail.value?.text || '', applyHunks(seg.buildOps, keepAllBut(seg.buildOps, seg.idx)), `${seg.buildId}:${seg.idx}`)
}
// Accept / reject every pending suggestion on this instruction. Resolve one at a
// time (each reload re-rebases the rest onto the new current), newest first so a
// later edit that supersedes an earlier one lands last.
const bulkResolving = ref(false)
const resolveAll = async (mode: 'accept' | 'reject') => {
  if (bulkResolving.value || resolving.value !== null) return
  bulkResolving.value = true
  try {
    let guard = 0
    while (pendingViews.value.length && guard++ < 100) {
      const v = pendingViews.value[0]
      const cur = detail.value?.text || ''
      if (mode === 'accept') await doResolveFor(v.build.build_id, v.merged, v.merged, `src:${v.build.build_id}`)
      else await doResolveFor(v.build.build_id, cur, cur, `src:${v.build.build_id}`)
    }
  } finally { bulkResolving.value = false }
}
const acceptSource = (pb: any) => { const m = mergedTextFor(pb); closeDiff(); doResolveFor(pb.build_id, m, m, `src:${pb.build_id}`) }
const rejectSource = (pb: any) => { closeDiff(); doResolveFor(pb.build_id, detail.value?.text || '', detail.value?.text || '', `src:${pb.build_id}`) }
const scrollToBuild = (buildId: string) => {
  highlightBuild.value = buildId
  nextTick(() => {
    document.getElementById(`rh-${buildId}-0`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    setTimeout(() => { if (highlightBuild.value === buildId) highlightBuild.value = null }, 1800)
  })
}
// Clicking a suggestion always returns to the merged review (exit any version
// compare so the inline hunks exist) and scrolls to it — fixes the "clicking a
// suggestion while on a version does nothing" confusion.
const locateSuggestion = (pb: any) => { closeDiff(); scrollToBuild(pb.build_id) }
// Right panel: version history only, toggled via the clock button.
const toggleHistory = () => { if (canEditDetail.value) showHistory.value = !showHistory.value }
const sourceLabel = (pb: any) => pb?.source === 'ai' ? 'AI' : 'Proposed'

// Agent trace: open the report/completion that produced this suggestion.
const canViewConsole = computed(() => useCan('view_console'))
const showTraceModal = ref(false)
const traceReportId = ref<string | null>(null)
const traceCompletionId = ref<string | null>(null)
const openTrace = (pb: any) => {
  if (!pb || !canViewConsole.value) return
  traceReportId.value = pb.report_id || null
  traceCompletionId.value = pb.completion_id || null
  showTraceModal.value = true
}
// Clean inline word-diff (current ↔ selected version / suggestion), like ReportAgent/GlobalCreate.
const diffOps = computed(() => {
  if (!diff.value) return []
  const base = diff.value.original || ''
  const next = diff.value.modified || ''
  if (base === next) return [{ type: 0, text: base }]
  return wordDiffOps(base, next)
})
// Compare an older version to the CURRENT live text. Direction matters: the
// old version is the base and the live text is the target, so an insertion
// (green) reads "added since v25" and a deletion (red) reads "removed since
// v25" — the same old→new convention the suggestion diffs use. It used to run
// the other way round (live as base, the old version as the target), which
// rendered every change inverted, and it based the diff on `detail.text` — the
// row cache, which is exactly the value staged suggestions leave stale, so on
// an instruction with pending changes it wasn't diffing against current at all.
const viewVersion = async (v: any, isCurrent: boolean) => {
  if (isCurrent || !detail.value) { closeDiff(); return }
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${detail.value.id}/versions/${v.id}`, { method: 'GET' })
    diff.value = {
      title: `${t('nav.version')} v${v.version_number}`,
      label: `v${v.version_number}`,
      original: data.value?.text || '',
      modified: liveText.value,
      versionId: v.id,
      buildId: null,
    }
  } catch {}
}
const viewSuggestion = (pb: any) => {
  activeSuggestion.value = pb
  diff.value = { title: pb.source === 'ai' ? 'AI suggestion' : 'Proposed change', label: `v${pb.pending_version_number}`, original: detail.value?.text || '', modified: mergedTextFor(pb), buildId: pb.build_id, versionId: null }
  // Reset any prior run view and lazily load eval suites for the run strip.
  evalActiveRun.value = null; evalResults.value = []; stopEvalPoll()
  fetchEvalSuites()
}
const approveSuggestion = async (pb: any) => {
  if (!pb?.build_id) return
  approving.value = pb.build_id
  try {
    await useMyFetch(`/api/builds/${pb.build_id}/publish`, { method: 'POST' })
    toast.add({ title: t('agentsPage.toastApprovedPublished'), color: 'green' })
    closeDiff()
    await refreshLists()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) openInstruction(fresh)
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) } finally { approving.value = null }
}
const discardSuggestion = async (pb: any) => {
  if (!pb?.build_id || discarding.value) return
  if (!window.confirm('Discard this suggested change? It will be rejected and removed from the review queue.')) return
  discarding.value = pb.build_id
  try {
    const { error } = await useMyFetch(`/api/builds/${pb.build_id}/reject`, { method: 'POST', body: { reason: 'Discarded from the Agents review queue' } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Reject failed')
    toast.add({ title: t('agentsPage.toastSuggestionDiscarded'), color: 'gray' })
    if (diff.value?.buildId === pb.build_id) closeDiff()
    await refreshLists()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) openInstruction(fresh)
  } catch (e: any) { toast.add({ title: t('agentsPage.toastDiscardFailed'), description: e?.message, color: 'red' }) } finally { discarding.value = null }
}

// ── Suggestion evals: run a test suite against the candidate (pending) build,
//    showing live progress like BuildExplorerModal. ───────────────────────────
const canManageTests = computed(() => useCanAny('manage_evals', 'data_source'))
const evalSuites = ref<any[]>([])
const selectedEvalSuiteId = ref<string | null>(null)
const evalRunning = ref(false)
const evalActiveRun = ref<any | null>(null)
const evalResults = ref<any[]>([])
let evalPoll: ReturnType<typeof setInterval> | null = null

const evalSuiteOptions = computed(() => evalSuites.value.map((s: any) => ({ value: s.id, label: `${s.name} (${s.tests_count || 0})` })))
const evalHasCases = computed(() => {
  const s = evalSuites.value.find((x: any) => x.id === selectedEvalSuiteId.value)
  return (s?.tests_count || 0) > 0
})
const evalSummary = computed(() => {
  const r = evalResults.value
  const total = r.length
  const passed = r.filter((x: any) => x.status === 'pass').length
  const failed = r.filter((x: any) => x.status === 'fail' || x.status === 'error').length
  const inProgress = r.filter((x: any) => x.status === 'in_progress').length
  const progressPercent = total > 0 ? Math.round(((passed + failed) / total) * 100) : 0
  return { total, passed, failed, inProgress, progressPercent }
})
const evalPrettyStatus = (s?: string) => s === 'in_progress' ? t('agentsPage.evalStatusRunning') : s === 'success' ? t('agentsPage.evalStatusPassed') : (s === 'fail' || s === 'error') ? t('agentsPage.evalStatusFailed') : (s || '—')

const fetchEvalSuites = async () => {
  if (!canManageTests.value || evalSuites.value.length) return
  try {
    const { data } = await useMyFetch<any[]>('/api/tests/suites/summary', { method: 'GET' })
    evalSuites.value = data.value || []
    if (evalSuites.value.length && !selectedEvalSuiteId.value) selectedEvalSuiteId.value = evalSuites.value[0].id
  } catch {}
}
const fetchEvalResults = async (runId: string) => {
  try { const { data } = await useMyFetch<any[]>(`/api/tests/runs/${runId}/results`, { method: 'GET' }); evalResults.value = data.value || [] } catch {}
}
const stopEvalPoll = () => { if (evalPoll) { clearInterval(evalPoll); evalPoll = null } }
const pollEvalRun = async () => {
  if (!evalActiveRun.value) return
  try {
    const { data } = await useMyFetch<any>(`/api/tests/runs/${evalActiveRun.value.id}`, { method: 'GET' })
    if (data.value) {
      evalActiveRun.value = data.value
      await fetchEvalResults(data.value.id)
      if (data.value.status !== 'in_progress') stopEvalPoll()
    }
  } catch {}
}
const startEvalPoll = () => { if (evalPoll) return; evalPoll = setInterval(pollEvalRun, 2000) }
const runSuggestionEval = async () => {
  const buildId = diff.value?.buildId
  if (!buildId || !selectedEvalSuiteId.value || evalRunning.value) return
  evalRunning.value = true
  try {
    const { data, error } = await useMyFetch<any>('/api/tests/runs/batch', { method: 'POST', body: { suite_id: selectedEvalSuiteId.value, build_id: buildId, trigger_reason: 'manual' } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed to start eval')
    if (data.value) {
      evalActiveRun.value = data.value
      await fetchEvalResults(data.value.id)
      startEvalPoll()
      toast.add({ title: t('agentsPage.toastEvalStarted'), color: 'blue' })
    }
  } catch (e: any) { toast.add({ title: t('agentsPage.toastEvalStartFailed'), description: e?.message, color: 'red' }) } finally { evalRunning.value = false }
}
onUnmounted(() => stopEvalPoll())

const labelOpts = computed(() => labels.value.map(l => ({ value: l.id, label: l.name })))
const activeFilterCount = computed(() => fStatus.value.length + fLoad.value.length + fSource.value.length + fCategory.value.length)
const clearFilters = () => { fStatus.value = []; fLoad.value = []; fSource.value = []; fCategory.value = [] }

// Connections shown in the footer. Agent-attached connections carry richer
// per-agent fields, but childless connections (created but not yet linked to any
// agent/data source) only exist in the org-wide /connections list — fetch that
// too so they're visible and can be managed instead of being orphaned.
const orgConnections = ref<any[]>([])
// Parallel to agentsLoaded: gate the connections area's spinner on the first load.
const connectionsLoaded = ref(false)
const fetchConnections = async () => {
  try { const { data } = await useMyFetch<any[]>('/connections', { method: 'GET' }); orgConnections.value = data.value || [] } catch (e) { console.error(e) } finally { connectionsLoaded.value = true }
}
const connections = computed(() => {
  const m = new Map<string, any>()
  for (const a of agents.value) for (const c of (a.connections || [])) if (!m.has(c.id)) m.set(c.id, c)
  for (const c of orgConnections.value) if (!m.has(c.id)) m.set(c.id, c)
  return Array.from(m.values())
})

// requires sign-in (ported from /agents/index.vue)
const requiresUserAuth = (a: any) => (a.connections || []).some((c: any) => c.auth_policy === 'user_required')
const needsSignIn = (a: any) => {
  if (!requiresUserAuth(a)) return false
  for (const c of (a.connections || [])) {
    if (c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials && c.user_status?.effective_auth !== 'system') return true
  }
  return false
}
// In-app OBO/user sign-in (was: window.open the legacy /old_agents page).
const openAgentTab = (id: string) => { connectAgent(id) }

// ── Expansion ───────────────────────────────────────────
const isOpen = (key: string) => expanded.value.has(key)
const expand = (key: string, force?: boolean) => {
  if (force) expanded.value.add(key)
  else if (expanded.value.has(key)) expanded.value.delete(key)
  else expanded.value.add(key)
  if (key.startsWith('agent:') && expanded.value.has(key)) { const id = key.slice('agent:'.length); expanded.value.add('instr:' + id); loadAgentMeta(id) }
  // Lazy-load instruction rows on first expand of a group (rows arrive from the
  // backend; counts/badges were already loaded on mount).
  if (expanded.value.has(key)) {
    if (key === 'global' || key === 'skills') loadGroup(key)
    else if (key.startsWith('agent:')) loadGroup(key.slice('agent:'.length))
    else if (key.startsWith('instr:')) loadGroup(key.slice('instr:'.length))
  }
  expanded.value = new Set(expanded.value)
}

// ── Fetching (lazy) ─────────────────────────────────────
// Aggregate badges — one cheap call, no rows. Drives every count/dot in the tree.
const fetchCounts = async () => {
  try {
    const { data } = await useMyFetch<any>('/api/instructions/counts', { method: 'GET' })
    if (data.value) {
      counts.value = data.value
      // Counts already carries the full per-instruction pending set, so the
      // per-row "pending" dots come from this one call — no separate org-wide
      // /pending-changes sweep needed on the hot path.
      if (Array.isArray(data.value.pending_instruction_ids)) {
        pendingInstrIds.value = new Set<string>(data.value.pending_instruction_ids.map((x: any) => String(x)))
      }
    }
  } catch (e) { console.error(e) }
}
// Merge fetched rows into the lazy cache (dedupe by id; newest wins).
const mergeRows = (rows: Instruction[]) => {
  if (!rows?.length) return
  const byId = new Map(allInstructions.value.map(i => [i.id, i]))
  for (const r of rows) byId.set(r.id, r)
  allInstructions.value = Array.from(byId.values())
}
// Lazy-load a group's rows on first expand (cached after). Keys:
//   'global' -> global_only ; 'skills' -> kind=skill ; <agentId> -> data_source_ids
const loadGroup = async (key: string, force = false) => {
  if (!key) return
  if (!force && loadedGroups.value.has(key)) return
  if (loadingGroups.value.has(key)) return
  loadingGroups.value = new Set(loadingGroups.value).add(key)
  try {
    const query: Record<string, any> = { include_own: true, include_drafts: true, include_archived: true }
    if (key === 'global') query.global_only = true
    else if (key === 'skills') query.kind = 'skill'
    else { query.data_source_ids = key; query.include_global = false }
    // Pages through every row — the group is only marked loaded on success, so
    // a failure retries on the next expand instead of caching a partial set.
    const { items } = await fetchAllInstructions(query)
    mergeRows(items)
    // Load the folder overlay for this scope alongside its rows (skills has no
    // folders). 'global' key maps to the global scope; an agent key is its id.
    if (key === 'global') await loadDirectories(GLOBAL_SCOPE)
    else if (key !== 'skills') await loadDirectories(key)
    loadedGroups.value = new Set(loadedGroups.value).add(key)
  } catch (e) { console.error(e) } finally {
    const s = new Set(loadingGroups.value); s.delete(key); loadingGroups.value = s
  }
}
// True while a group is fetching for the first time (drives the per-node Spinner).
const groupLoading = (key: string) => loadingGroups.value.has(key) && !loadedGroups.value.has(key)
// Reload everything currently loaded (counts + each loaded group). Used after
// a mutation so badges and the visible rows both stay correct.
const fetchAll = async () => {
  try {
    await Promise.all([fetchCounts(), ...Array.from(loadedGroups.value).map(k => loadGroup(k, true))])
  } finally { instrLoading.value = false }
}
// Refresh badges + pending dots + visible rows after a mutation. fetchAll() runs
// fetchCounts(), which also refreshes the per-row pending-dot set — so no extra
// /pending-changes sweep is needed here. While the "Pending changes" view is
// open its flat list is refreshed too, so a just-resolved instruction drops out
// instead of lingering until the next enter.
const refreshLists = async () => {
  await fetchAll()
  if (canApprove.value) await loadPendingChanges()
}
const fetchAgents = async () => {
  try {
    // include_unconnected=true so members also see user_required (OBO) agents
    // they haven't connected yet — otherwise they can never reach the Connect
    // flow (parity with the legacy /agents page). show_all is an admin-only
    // toggle that surfaces every agent in the org (admin_only entries flagged).
    const query: Record<string, any> = { include_unconnected: true }
    if (showAllAgents.value) query.show_all = true
    const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET', query })
    agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name, type: d.type, icon: d.icon, connections: d.connections || [], user_status: d.user_status, is_public: d.is_public, is_connector: d.is_connector, connector_key: d.connector_key, status: d.status, publish_status: d.publish_status, description: d.description, auth_policy: d.auth_policy, admin_only: d.admin_only }))
  } catch (e) { console.error(e) } finally { agentsLoaded.value = true }
}
const agentStatusDot = (a: any) => a?.publish_status === 'disabled' ? 'bg-gray-300' : (a?.status === 'active' ? 'bg-green-400' : 'bg-gray-300')
// Group an agent's tools by their connection (MCP server / custom API), resolving
// the connection name + type from the agent's connections for the tree headers.
// Count shown on the Files tree node: uploads + total glob rules.
const filesGroupCount = (agentId: string) => {
  const ups = agentFiles.value[agentId]?.length || 0
  const globs = (agentFileConns.value[agentId] || []).reduce((n: number, c: any) => n + (c.globs?.length || 0), 0)
  return (ups + globs) || undefined
}
const toolGroups = (agentId: string) => {
  const tools = agentTools.value[agentId] || []
  const a = agents.value.find(x => x.id === agentId)
  const connMap: Record<string, any> = {}
  for (const c of (a?.connections || [])) connMap[c.id] = c
  const groups: Record<string, { connId: string; name: string; type?: string; connector_key?: string; tools: any[] }> = {}
  for (const t of tools) {
    const cid = String(t.connection_id ?? t.connection?.id ?? 'tools')
    if (!groups[cid]) groups[cid] = { connId: cid, name: connMap[cid]?.name || t.connection_name || 'Tools', type: connMap[cid]?.type || t.connection_type, connector_key: connMap[cid]?.connector_key, tools: [] }
    groups[cid].tools.push(t)
  }
  return Object.values(groups)
}
const fetchLabels = async () => { try { const { data } = await useMyFetch<any[]>('/instructions/labels', { method: 'GET' }); labels.value = data.value || [] } catch {} }
const fetchCategories = async () => { try { const { data } = await useMyFetch<string[]>('/instructions/categories', { method: 'GET' }); categories.value = data.value || [] } catch {} }
const fetchGitStatus = async () => {
  // Every /git/* endpoint requires create_data_source; the button is hidden
  // without it, so skip the guaranteed-403 fetch for regular members.
  if (!canManageGit.value) return
  try {
    const { data } = await useMyFetch<any[]>('/git/repositories', { method: 'GET' })
    const repos = data.value || []
    gitRepos.value = repos.map((r: any) => ({ provider: r.provider, repoName: (r.repo_url || '').split('/').pop()?.replace(/\.git$/, '') || 'Repo' }))
    gitLastIndexed.value = repos.map((r: any) => r.last_indexed_at).filter(Boolean).sort().pop() || null
  } catch {}
}
const onGitChanged = () => { fetchGitStatus(); fetchAll() }
const loadAgentMeta = async (id: string) => {
  if (agentLoaded.value.has(id)) return
  agentLoaded.value.add(id)
  try {
    // Paginated + selected-only: the tree renders active tables only, and the
    // unpaginated legacy branch hydrates the ENTIRE catalog (150k rows / ~200MB
    // on a 50-connection agent — see docs/feedback-loops/agents-hub-agent-many-connections.md).
    const { data } = await useMyFetch<any>(`/data_sources/${id}/full_schema?page=1&page_size=500&selected_state=selected&sort_by=name&sort_dir=asc`, { method: 'GET' })
    const v: any = data.value
    const items = Array.isArray(v) ? v : (v?.tables || v?.items || [])
    agentTables.value[id] = items.map((t: any) => ({ id: String(t.id ?? t.name), name: t.name, is_active: t.is_active !== false }))
    agentTableTotals.value[id] = typeof v?.total === 'number' ? v.total : agentTables.value[id].length
  } catch { agentTables.value[id] = []; delete agentTableTotals.value[id] }
  try { const { data } = await useMyFetch<any[]>(`/data_sources/${id}/tools`, { method: 'GET' }); agentTools.value[id] = data.value || [] } catch { agentTools.value[id] = [] }
  try { const { data } = await useMyFetch<any[]>(`/data_sources/${id}/files`, { method: 'GET' }); agentFiles.value[id] = data.value || [] } catch { agentFiles.value[id] = [] }
  // File-source connections + their glob rules (shown in the Files tree).
  try {
    const { data } = await useMyFetch<any[]>(`/data_sources/${id}/connections`, { method: 'GET' })
    agentFileConns.value[id] = (data.value || [])
      .filter((c: any) => FILE_CONN_TYPES.has(c.type))
      .map((c: any) => ({
        id: c.id, name: c.name, type: c.type, connector_key: c.connector_key,
        globs: String(c.config?.include_globs || '').split(/[,\n]/).map((s: string) => s.trim()).filter(Boolean),
      }))
    agentFileConns.value = { ...agentFileConns.value }
  } catch { agentFileConns.value[id] = [] }
  agentTables.value = { ...agentTables.value }; agentTools.value = { ...agentTools.value }; agentFiles.value = { ...agentFiles.value }
}

// ── File preview ────────────────────────────────────────
const TEXT_EXT = /\.(md|markdown|txt|csv|tsv|json|sql|ya?ml|log|xml|html?|ini|toml|env|sh)$/i
const fileIcon = (ct?: string, name?: string) => {
  const c = ct || ''
  if (/^image\//.test(c) || /\.(png|jpe?g|gif|webp|svg)$/i.test(name || '')) return 'i-heroicons-photo'
  if (c === 'application/pdf' || /\.pdf$/i.test(name || '')) return 'i-heroicons-document'
  if (/csv|excel|spreadsheet/.test(c) || /\.(csv|tsv|xlsx?)$/i.test(name || '')) return 'i-heroicons-table-cells'
  if (/^text\/|json/.test(c) || TEXT_EXT.test(name || '')) return 'i-heroicons-document-text'
  return 'i-heroicons-paper-clip'
}
const isImage = (f: any) => /^image\//.test(f?.content_type || '') || /\.(png|jpe?g|gif|webp|svg)$/i.test(f?.filename || '')
const isPdf = (f: any) => f?.content_type === 'application/pdf' || /\.pdf$/i.test(f?.filename || '')
const isText = (f: any) => /^text\/|json|csv/.test(f?.content_type || '') || TEXT_EXT.test(f?.filename || '')
const previewFileAgentId = ref<string | null>(null)
const closePreview = () => { previewFile.value = null; previewFileAgentId.value = null; if (previewUrl.value) { URL.revokeObjectURL(previewUrl.value); previewUrl.value = null } previewText.value = null }
const downloadPreview = () => { if (previewUrl.value) window.open(previewUrl.value, '_blank') }
const deleteFile = async (agentId: string, f: any) => {
  if (!agentId || !f?.id) return
  if (!window.confirm(`Delete "${f.filename}"? This can't be undone.`)) return
  try {
    await useMyFetch(`/data_sources/${agentId}/files/${f.id}`, { method: 'DELETE' })
    agentFiles.value[agentId] = (agentFiles.value[agentId] || []).filter((x: any) => x.id !== f.id)
    agentFiles.value = { ...agentFiles.value }
    if (previewFile.value?.id === f.id) closePreview()
    toast.add({ title: t('agentsPage.toastFileDeleted'), color: 'green' })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}
const openFile = async (f: any, agentId?: string) => {
  detail.value = null; creating.value = false; editing.value = false; selectedId.value = null
  closeDiff(); closePanel(); closeAgentView(); pendingBuilds.value = []; closePreview(); previewFile.value = f; previewFileAgentId.value = agentId || null; previewLoading.value = true
  try {
    const { data } = await useMyFetch<any>(`/api/files/${f.id}/content`, { method: 'GET', responseType: 'blob' as any })
    const blob = data.value as Blob | null
    if (blob) { if (isText(f)) previewText.value = await blob.text(); else previewUrl.value = URL.createObjectURL(blob) }
  } catch (e) { /* ignore */ } finally { previewLoading.value = false }
}

// ── Counts ──────────────────────────────────────────────
// An instruction is "pending" iff the cheap sweep flags it AND the
// authoritative per-hunk pass hasn't already proven this session that nothing
// is left to review (the sweep is optimistic for drifted suggestions).
const isPending = (ins: Instruction) => canEditInstruction(ins) && pendingInstrIds.value.has(ins.id) && !verifiedNotPending.value.has(ins.id)
// Strip unpublished-build metadata from every status helper used for a row the
// viewer cannot manage. Those generic helpers otherwise turn current_build_*
// into a second "Pending review" leak even when isPending() correctly says no.
const visibleInstructionState = (ins: Instruction) => canEditInstruction(ins)
  ? ins
  : { ...ins, current_build_id: null, current_build_status: null }
// The aggregate API count is view-scoped and may include instructions the user
// cannot review. Count the authority-filtered pending rows loaded above instead.
const pendingCount = computed(() => {
  return new Set(pendingRows.value
    .filter(ins => canEditInstruction(ins) && !verifiedNotPending.value.has(ins.id))
    .map(ins => ins.id)).size
})
const globalCount = computed(() => counts.value?.global || 0)
const skillCount = computed(() => counts.value?.skills || 0)
const agentCount = (id: string) => counts.value?.by_agent?.[id] || 0

// Plural choice for the agent header counts ("1 table" vs "2 tables").
// The counts render an en-dash while still loading, so coerce anything
// non-numeric to the plural form rather than feeding a string to vue-i18n.
// Locales whose message has no "|" are unaffected and render as before.
const statChoice = (n: unknown) => (typeof n === 'number' && Number.isFinite(n) ? n : 2)
const agentPending = (id: string) => pendingRows.value.some(ins =>
  !verifiedNotPending.value.has(ins.id)
  && (ins.data_sources || []).some(ds => ds.id === id))

// ── Leaf lists ──────────────────────────────────────────
const applyFilters = (list: Instruction[]) => {
  let out = list
  if (fStatus.value.length) out = out.filter(i => fStatus.value.includes(isPending(i) ? 'pending_review' : i.status))
  if (fLoad.value.length) out = out.filter(i => fLoad.value.includes(i.load_mode || 'always'))
  if (fSource.value.length) out = out.filter(i => fSource.value.includes(h.getSourceType(i)))
  if (fCategory.value.length) out = out.filter(i => fCategory.value.includes(i.category))
  // Text search is server-side now (see runSearch / searchResults) — not applied here.
  return out
}
const listFor = (kind: string) => {
  let base = allInstructions.value
  if (kind === 'skills') base = base.filter(i => (i as any).kind === 'skill')
  else if (kind === 'pending') base = base.filter(isPending)
  else if (kind === 'global') base = base.filter(i => (i.data_sources || []).length === 0)
  return applyFilters(base)
}
// An agent's Instructions node lists EVERY instruction attached to it, table-
// scoped ones included. It used to exclude anything carrying a datasource_table
// reference on the assumption the Tables subtree would host it instead — but
// that subtree only renders tables the agent currently has selected AND active
// (see activeTables), so a rule pinned to a table that is out of scope, or past
// the first schema page, had no node anywhere and vanished from the tree while
// still showing in the report agent panel. Appearing under both its table and
// its agent is the lesser problem: nothing an agent carries goes unlisted.
const listForAgent = (id: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id)))
// Which tables an instruction is filed under. List rows come from the light
// projection, which carries `table_ref_ids` and no reference rows; a hydrated
// row (the open instruction) carries the full `references` instead. Reading
// only the latter is what left every table node in the tree empty.
const tableRefIds = (ins: any): string[] => {
  const light = (ins?.table_ref_ids || []) as string[]
  if (light.length) return light.map(String)
  return (ins?.references || [])
    .filter((r: any) => r.object_type === 'datasource_table')
    .map((r: any) => String(r.object_id))
}
const listForTable = (agentId: string, tableId: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === agentId) && tableRefIds(i).includes(tableId)))
// The tree only surfaces ACTIVE (in-scope) tables — the lean working set the
// agent actually reasons with. The full catalog (active + inactive) lives on the
// agent's Tables page; the tree is not a schema browser.
const activeTables = (agentId: string) => (agentTables.value[agentId] || []).filter((t: any) => t.is_active)

// ── Detail / create ─────────────────────────────────────
const openInstruction = async (ins: Instruction) => {
  closePreview(); closeDiff(); closePanel(); closeAgentView(); closeReview(); closeEvalCase(); creating.value = false; bottomTab.value = 'details'
  // Clear every draft-derived value before swapping rows. Without this, an
  // in-flight manager request can briefly render its hunks/history after the
  // user has selected an instruction they may only view.
  pendingBuilds.value = []
  reviewLoading.value = false
  reviewEmpty.value = false
  reviewHunks.value = { total: 0, busy: false }
  versions.value = []
  versionsLoading.value = false
  showHistory.value = false
  // Drop the previous row's live-text snapshot — loadPending() below refetches
  // it, and until then no version may be labelled current from stale state.
  mainText.value = null; mainVersionId.value = null
  // The row came from the light list, so it has `preview` but no body. Seed the
  // pane with the preview so it shows the opening lines rather than blank while
  // GET /instructions/{id} (below) fetches the real text.
  selectedId.value = ins.id
  detail.value = { ...ins, text: (ins as any).text ?? (ins as any).preview ?? '' } as Instruction
  editing.value = false
  syncDraft(detail.value)
  try {
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${ins.id}`, { method: 'GET' })
    if (data.value && selectedId.value === ins.id) {
      detail.value = data.value; if (!editing.value) syncDraft(data.value)
      // keep the tree leaf consistent with the hydrated build/status
      const idx = allInstructions.value.findIndex(i => i.id === ins.id)
      if (idx >= 0) { allInstructions.value[idx] = { ...allInstructions.value[idx], status: data.value.status, current_build_id: data.value.current_build_id, current_build_status: data.value.current_build_status }; allInstructions.value = [...allInstructions.value] }
    }
  } catch (e) {}
  ensureDirScopes(detail.value)
  // Draft hunks and version history are management surfaces. A viewer gets the
  // approved text from the detail response and never requests either endpoint.
  if (canEditDetail.value) await Promise.all([loadPending(ins.id), loadVersions(ins.id)])
}
const syncDraft = (ins: Instruction) => {
  draft.title = ins.title || ''; draft.description = (ins as any).description || ''; draft.text = ins.text || ''
  draft.kind = (ins as any).kind || 'instruction'
  draft.load_mode = ins.load_mode || 'always'; draft.status = ins.status || 'published'
  draft.category = ins.category || 'general'
  draft.applicable_modes = sanitizeModes((ins as any).applicable_modes)
  draft.applicable_channels = ((ins as any).applicable_channels) || []
  // Surface the Advanced section when this instruction is already scoped.
  showAdvanced.value = draft.applicable_modes.length > 0 || draft.applicable_channels.length > 0
  draft.data_source_ids = (ins.data_sources || []).map(d => d.id)
  draft.label_ids = (ins.labels || []).map((l: any) => l.id)
  draft.references = (ins.references || []).map((r: any) => ({ object_type: r.object_type, object_id: String(r.object_id), relation_type: r.relation_type || 'scope', display_text: r.display_text || r.object?.name || String(r.object_id), column_name: r.column_name || null }))
  draft.data_source_ids.forEach(id => loadAgentMeta(id))
}
// The agent the user is "sitting on" when they hit a context-free New button:
// the agent whose pane is open, else the one whose sub-panel is. Read BEFORE
// closeAgentView() below clears it.
const currentAgentId = () => agentView.value?.agentId || panelView.value?.agentId || null
const openCreate = (scope?: { agentId?: string; tableId?: string; tableName?: string; global?: boolean }) => {
  // An explicit agent wins; `global: true` (the Global instructions group's own
  // + button) forces no agent; otherwise inherit the agent in view, so New from
  // inside an agent doesn't quietly create an org-wide instruction.
  const agentId = scope?.global ? null : (scope?.agentId || currentAgentId())
  closePreview(); closeDiff(); closePanel(); closeAgentView(); closeReview(); closeEvalCase(); pendingBuilds.value = []; detail.value = null; selectedId.value = null; versions.value = []; mainText.value = null; mainVersionId.value = null
  creating.value = true; editing.value = true
  draft.title = ''; draft.description = ''; draft.text = ''; draft.kind = 'instruction'; draft.load_mode = 'always'; draft.status = 'published'; draft.category = 'general'
  draft.applicable_modes = []; draft.applicable_channels = []
  showAdvanced.value = false
  draft.data_source_ids = agentId ? [agentId] : []
  draft.label_ids = []
  draft.references = scope?.tableId ? [{ object_type: 'datasource_table', object_id: scope.tableId, relation_type: 'scope', display_text: scope.tableName }] : []
  draft.data_source_ids.forEach(id => loadAgentMeta(id))
}
const startEdit = () => { if (detail.value && canEditDetail.value) { syncDraft(detail.value); editing.value = true } }
const cancelEdit = () => { if (creating.value) { creating.value = false; editing.value = false; draft.references = [] } else { if (detail.value) syncDraft(detail.value); editing.value = false } }
const deleteInstruction = async () => {
  if (!detail.value || creating.value) return
  const id = detail.value.id
  const label = detail.value.title || 'this instruction'
  if (!window.confirm(`Delete "${label}"? This can't be undone.`)) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/api/instructions/${id}`, { method: 'DELETE' })
    if (error.value) throw new Error((error.value as any)?.data?.detail || (error.value as any)?.message || 'Delete failed')
    toast.add({ title: t('agentsPage.toastDeleted'), color: 'green' })
    allInstructions.value = allInstructions.value.filter(i => i.id !== id)
    editing.value = false; detail.value = null; selectedId.value = null; versions.value = []; mainText.value = null; mainVersionId.value = null
    fetchPendingMap()
    fetchCounts()   // same staleness as create: the group badge is server-side
  } catch (e: any) {
    toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' })
  } finally { deleting.value = false }
}
const save = async () => {
  saving.value = true
  try {
    const body: any = { title: draft.title || null, description: draft.description || null, text: draft.text, kind: draft.kind, load_mode: draft.load_mode, status: draft.status, category: draft.category, data_source_ids: draft.data_source_ids, label_ids: draft.label_ids, references: draft.references, applicable_modes: draft.applicable_modes, applicable_channels: draft.applicable_channels }
    if (creating.value) {
      const endpoint = draft.data_source_ids.length ? '/api/instructions' : '/api/instructions/global'
      const { data, error } = await useMyFetch<Instruction>(endpoint, { method: 'POST', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Create failed')
      toast.add({ title: t('agentsPage.toastCreated'), color: 'green' })
      creating.value = false; editing.value = false; draft.references = []
      // Insert the new instruction in place (no full list re-fetch / flicker);
      // the tree grouping computed places it. Fall back to a refresh only if the
      // POST didn't return the row.
      const createdRow = data.value as any
      if (createdRow?.id) {
        if (!allInstructions.value.some(i => i.id === createdRow.id)) {
          allInstructions.value = [...allInstructions.value, createdRow]
        }
        fetchPendingMap()
        // The group badges come from the server's counts, not from the local
        // list — without this the new instruction lands in a group still
        // labelled with its old count (0), which reads as "it wasn't created".
        fetchCounts()
        // Open the group it landed in, so the row is actually on screen. A
        // collapsed (or never-loaded) group would otherwise swallow it.
        const dsIds = (createdRow.data_sources || []).map((d: any) => String(d.id))
        if (dsIds.length) dsIds.forEach(id => { expand('agent:' + id, true); expand('instr:' + id, true) })
        else expand('global', true)
        openInstruction(createdRow)
      } else {
        await refreshLists()
      }
    } else if (detail.value) {
      const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Save failed')
      toast.add({ title: t('agentsPage.toastSaved'), color: 'green' }); editing.value = false
      // Update just this instruction in place — no full list re-fetch, so the
      // tree keeps its scroll/expanded state (no page-refresh feel).
      if (data.value) {
        const merged = { ...detail.value, ...data.value }
        detail.value = merged; syncDraft(merged)
        const idx = allInstructions.value.findIndex(i => i.id === merged.id)
        if (idx >= 0) { allInstructions.value[idx] = { ...allInstructions.value[idx], ...data.value }; allInstructions.value = [...allInstructions.value] }
      }
      fetchPendingMap()
      loadVersions(detail.value!.id)
    }
  } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e.message, color: 'red' }) } finally { saving.value = false }
}

// ── Detail tabs (Instruction / Analyze) ─────────────────
const bottomTab = ref<'details' | 'analyze'>('details')
// Advanced (scoping) subsection in the Details panel — holds the run-mode and
// channel restrictions. Collapsed by default; auto-opens when the instruction
// already carries scoping so it's discoverable.
const showAdvanced = ref(false)
const advancedHasValues = computed(() => (draft.applicable_modes?.length || 0) > 0 || (draft.applicable_channels?.length || 0) > 0)
// Who may edit the bottom metadata inline (autosave) vs see read-only chips.
// Scoped to the OPEN instruction, matching the backend: manage_instructions on
// every agent it is attached to (org-level when attached to none). Using the
// bare org-level perm here is what left per-agent managers with a read-only
// load_mode ("Smart"/"Always") chip on agents they fully manage.
const canEditInstr = computed(() => canEditDetail.value)
// Editable controls also show while creating (the new instruction is authored here).
const metaEditable = computed(() => canEditInstr.value || creating.value)
const savingMeta = ref(false)
let metaTimer: any = null
const saveMeta = async () => {
  if (!detail.value || creating.value || editing.value) return
  savingMeta.value = true
  try {
    const body: any = { title: draft.title || null, description: draft.description || null, text: draft.text, kind: draft.kind, load_mode: draft.load_mode, status: draft.status, category: draft.category, data_source_ids: draft.data_source_ids, label_ids: draft.label_ids, references: draft.references, applicable_modes: draft.applicable_modes, applicable_channels: draft.applicable_channels }
    const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
    // useMyFetch doesn't throw on HTTP errors — surface them so the change isn't silently dropped.
    if (error.value) throw new Error((error.value as any)?.data?.detail || (error.value as any)?.message || 'Save failed')
    // The PUT response is the full row — the only post-save shape that carries
    // `text` and `references`. Re-seeding the pane from the refreshed LIST row
    // instead (what this used to do) silently emptied both: those rows are the
    // light projection, which drops the body and the references by design. The
    // draft is what the next autosave sends back, so the clobber didn't just
    // hide the reference you had just added — the following metadata change
    // PUT it away again as `references: []` and the body as `text: ""`.
    if (data.value) {
      detail.value = { ...detail.value, ...(data.value as any) }
      if (!editing.value) syncDraft(detail.value)
    }
    await refreshLists()
    toast.add({ title: t('agentsPage.toastSaved'), color: 'green' })
  } catch (e: any) { toast.add({ title: t('agentsPage.toastSaveFailed'), description: e?.message, color: 'red' }) } finally { savingMeta.value = false }
}
// Fire after a metadata control changes (user-initiated only — not on load/edit).
const onMetaChange = () => { if (editing.value || creating.value) return; clearTimeout(metaTimer); metaTimer = setTimeout(saveMeta, 400) }
// Skills always use 'intelligent' (smart) retrieval — force it when switching to skill.
const onKindChange = () => { if (draft.kind === 'skill') draft.load_mode = 'intelligent'; onMetaChange() }

// ── Analyze (related instructions + impact) ─────────────
const analysis = reactive<{ related: any[]; tokens: string[]; impactedPrompts: any[]; impactScore: number; impactMatched: number; impactTotal: number }>(
  { related: [], tokens: [], impactedPrompts: [], impactScore: 0, impactMatched: 0, impactTotal: 0 }
)
const analyzeLoading = ref(false)
const escapeHtml = (s: string) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c])
const highlightRelated = (text: string, tokens: string[]) => {
  let out = escapeHtml(text || '')
  for (const tok of (tokens || [])) {
    if (!tok || tok.length < 3) continue
    try { out = out.replace(new RegExp('(' + tok.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi'), '<mark class="bg-yellow-100 rounded px-0.5">$1</mark>') } catch {}
  }
  return out
}
const runAnalysis = async () => {
  const text = (editing.value ? draft.text : detail.value?.text) || ''
  if (!text.trim()) { analysis.related = []; analysis.impactedPrompts = []; analysis.impactScore = 0; return }
  analyzeLoading.value = true
  try {
    const { data } = await useMyFetch<any>('/api/instructions/analysis', { method: 'POST', body: { text, include: ['impact', 'related_instructions'], instruction_id: detail.value?.id || undefined, limits: { prompts: 5, instructions: 5 } } })
    const res = data.value as any
    if (res?.impact) { analysis.impactScore = res.impact.score ?? 0; analysis.impactedPrompts = res.impact.prompts || []; analysis.impactMatched = res.impact.matched_count ?? 0; analysis.impactTotal = res.impact.total_count ?? 0 }
    if (res?.related_instructions) {
      analysis.tokens = res.related_instructions.tokens || []
      analysis.related = (res.related_instructions.items || []).map((it: any) => ({ id: it.id, text: it.text, status: it.status, createdByName: it.createdByName || 'unknown', highlightedHtml: highlightRelated(it.text || '', analysis.tokens) }))
    }
  } catch (e) {} finally { analyzeLoading.value = false }
}
const openAnalyzeTab = () => { bottomTab.value = 'analyze'; runAnalysis() }

// ── Versions ────────────────────────────────────────────
const loadVersions = async (id: string) => {
  const stillAuthorized = () => selectedId.value === id
    && detail.value?.id === id
    && canEditDetail.value
  if (!stillAuthorized()) {
    versions.value = []
    versionsLoading.value = false
    return
  }
  versionsLoading.value = true; versions.value = []
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${id}/versions`, { method: 'GET', query: { limit: 50 } })
    if (stillAuthorized()) versions.value = data.value?.items || []
  } catch (e) {
    if (stillAuthorized()) versions.value = []
  } finally {
    if (selectedId.value === id || !selectedId.value) versionsLoading.value = false
  }
}
const restore = async (v: any) => {
  if (!detail.value) return
  if (!window.confirm(`Restore version v${v.version_number}? This creates a new version.`)) return
  try { await useMyFetch(`/api/instructions/${detail.value.id}/versions/${v.id}/revert`, { method: 'POST' }); toast.add({ title: t('agentsPage.toastRestored', { n: v.version_number }), color: 'green' }); await refreshLists(); const fresh = allInstructions.value.find(i => i.id === detail.value?.id); if (fresh) openInstruction(fresh) } catch (e: any) { toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' }) }
}

// ── Display helpers ─────────────────────────────────────
// Tree/list rows carry `preview` instead of the body; the detail pane still has
// the full `text` once an instruction is opened.
const displayTitle = (ins: Instruction) => instructionRowLabel(ins)

// ── Markdown export ─────────────────────────────────────
// The body is already markdown; the title and description live in their own
// fields, so they get promoted to a heading and a lead paragraph to make the
// downloaded file a standalone document. `draft` mirrors `detail` while not
// editing, so it is the right source in both states.
const instructionMarkdown = () => {
  const parts = [draft.title.trim() ? `# ${draft.title.trim()}` : '', draft.description.trim(), draft.text.trim()]
  return parts.filter(Boolean).join('\n\n') + '\n'
}
// Strip path/filesystem-hostile characters while keeping non-Latin scripts
// (Hebrew/Arabic) intact, so a translated title still yields a readable name.
const mdFileName = () => {
  const base = (draft.title || (detail.value ? displayTitle(detail.value) : '')).replace(/[^\w\d֐-׿؀-ۿ -]+/g, '').trim()
  return `${base || 'instruction'}.md`
}
const downloadMarkdown = () => {
  if (!detail.value) return
  const blob = new Blob([instructionMarkdown()], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = mdFileName()
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// Export one agent as a zip bundle (instructions markdown + agent.yaml +
// evals/*.yaml). Gated on per-agent `manage` (canManageAgent) — full admins
// pass via the wildcard. Backend enforces the same permission.
const exportingInstructions = ref(false)
const exportAgentInstructions = async (agentId?: string) => {
  if (!agentId || exportingInstructions.value) return
  exportingInstructions.value = true
  try {
    const { data, error } = await useMyFetch<Blob>(`/api/data_sources/${agentId}/instructions/export`, { method: 'GET', responseType: 'blob' as any })
    if (error.value || !data.value) {
      // With responseType: 'blob' the error body arrives as a Blob — parse it
      // so the backend's `detail` ("Agent export timed out…", "Agent not
      // found") reaches the toast instead of a bare "Error".
      let detail = error.value?.data?.detail
      if (!detail && error.value?.data instanceof Blob) {
        try { detail = JSON.parse(await (error.value.data as Blob).text())?.detail } catch { /* not JSON */ }
      }
      throw new Error(detail || t('agentsPage.toastError'))
    }
    const name = (agentDetail.value?.name || agentViewName.value || 'agent').replace(/[^\w\d֐-׿؀-ۿ .-]+/g, '').trim() || 'agent'
    const url = URL.createObjectURL(data.value as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${name}-agent-export.zip`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e: any) {
    toast.add({ title: t('agentsPage.toastError'), description: e?.message, color: 'red' })
  } finally {
    exportingInstructions.value = false
  }
}
const refLabel = (ref: any) => ref.display_text || ref.object?.name || ref.object_type
const _df = useFormatDate()
const fmtDate = (s?: string) => { if (!s) return ''; try { return _df.format(s, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }

// ── Inline tree sub-components ──────────────────────────
const TreeGroup = defineComponent({
  props: { label: String, icon: String, count: { type: Number, default: undefined }, countAccent: Boolean, pending: Boolean, open: Boolean, mono: Boolean, indent: { type: Number, default: 0 }, addable: Boolean, folderable: Boolean, gearable: Boolean, reloadable: Boolean, renamable: Boolean, deletable: Boolean, runnable: Boolean, running: Boolean, badge: String, badgeInteractive: { type: Boolean, default: true }, disabled: Boolean, labelClickable: Boolean, active: Boolean, statusDot: String, lock: Boolean, dropActive: Boolean, onDropzone: Function, onDragover: Function, onDragleave: Function },
  emits: ['toggle', 'add', 'folder', 'gear', 'reload', 'rename', 'delete', 'run', 'badge', 'label'],
  setup(props, { slots, emit }) {
    // When `labelClickable` is set, the chevron/icon area toggles the tree and the
    // label text opens the panel (`@label`); otherwise the whole row toggles.
    return () => createElement('div', {}, [
      createElement('div', {
        class: ['group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0', props.dropActive ? 'bg-blue-50 dark:bg-blue-500/10 ring-1 ring-blue-300 dark:ring-blue-500/40' : (props.active ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-300'), props.disabled ? 'opacity-90' : 'hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer'],
        style: { paddingInlineStart: (6 + props.indent * 14) + 'px', paddingInlineEnd: '8px' },
        onClick: () => { if (!props.disabled && !props.labelClickable) emit('toggle') },
        onDragover: props.onDropzone ? (e: DragEvent) => (props.onDragover as any)?.(e) : undefined,
        onDragleave: props.onDropzone ? (e: DragEvent) => (props.onDragleave as any)?.(e) : undefined,
        onDrop: props.onDropzone ? (e: DragEvent) => { e.preventDefault(); (props.onDropzone as any)?.(e) } : undefined,
      }, [
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-chevron-right', class: ['w-3 h-3 transition-transform shrink-0', props.disabled ? 'text-gray-200 dark:text-gray-700' : 'text-gray-300 dark:text-gray-600', props.open ? 'rotate-90' : 'rtl:rotate-180', props.labelClickable ? 'cursor-pointer hover:text-gray-500 dark:hover:text-gray-300' : ''], onClick: props.labelClickable ? (e: Event) => { e.stopPropagation(); if (!props.disabled) emit('toggle') } : undefined }),
        props.statusDot ? createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', props.statusDot], title: t('agentsPage.tipStatus') }) : null,
        slots.icon ? slots.icon() : (props.icon ? createElement(resolveComponent('UIcon'), { name: props.icon, class: 'w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0' }) : null),
        createElement('span', { class: ['flex-1 text-start truncate', props.mono ? 'font-mono text-xs' : ''], onClick: props.labelClickable ? (e: Event) => { e.stopPropagation(); if (!props.disabled) emit('label') } : undefined }, props.label),
        props.lock ? createElement(resolveComponent('UIcon'), { name: 'i-heroicons-lock-closed', class: 'w-3 h-3 text-gray-400 dark:text-gray-500 shrink-0', title: t('agentsPage.tipPrivate') }) : null,
        // Interactive badge (e.g. "Sign In") is a clickable key-button that
        // triggers connect. A non-interactive badge (e.g. "Connector") is a
        // passive label — no key icon, no click, so it can't open an unrelated
        // bearer-token/sign-in dialog once the connection is already set up.
        props.badge ? (props.badgeInteractive
          ? createElement('button', { class: 'shrink-0 inline-flex items-center gap-0.5 px-1.5 h-5 rounded bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 text-[10px] font-medium hover:bg-blue-100 dark:hover:bg-blue-500/20', onClick: (e: Event) => { e.stopPropagation(); emit('badge') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-key', class: 'w-2.5 h-2.5' }), props.badge])
          : createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px] font-medium' }, props.badge)) : null,
        (props.reloadable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipReload'), onClick: (e: Event) => { e.stopPropagation(); emit('reload') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-arrow-path', class: 'w-3 h-3' })]) : null,
        (props.gearable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipManage'), onClick: (e: Event) => { e.stopPropagation(); emit('gear') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-cog-6-tooth', class: 'w-3 h-3' })]) : null,
        // Kept visible while running (no opacity-0) — the row is the only
        // feedback that the run started before its detail pane opens.
        (props.runnable && !props.disabled) ? createElement('button', { class: ['shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 flex items-center justify-center', props.running ? 'text-blue-500 opacity-100' : 'text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100'], disabled: props.running, title: t('agentsPage.tipRunSuite'), onClick: (e: Event) => { e.stopPropagation(); if (!props.running) emit('run') } }, [createElement(resolveComponent('UIcon'), { name: props.running ? 'i-heroicons-arrow-path' : 'i-heroicons-play', class: ['w-3 h-3', props.running ? 'animate-spin' : ''] })]) : null,
        (props.renamable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipRename'), onClick: (e: Event) => { e.stopPropagation(); emit('rename') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-pencil', class: 'w-3 h-3' })]) : null,
        (props.deletable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipDeleteSuite'), onClick: (e: Event) => { e.stopPropagation(); emit('delete') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-trash', class: 'w-3 h-3' })]) : null,
        (props.folderable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipNewFolder'), onClick: (e: Event) => { e.stopPropagation(); emit('folder') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-folder-plus', class: 'w-3 h-3' })]) : null,
        (props.addable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipAdd'), onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-plus', class: 'w-3 h-3' })]) : null,
        (props.count !== undefined && !props.badge) ? createElement('span', { class: ['text-xs tabular-nums shrink-0', props.countAccent ? 'text-amber-600 dark:text-amber-400 font-medium' : 'text-gray-400 dark:text-gray-500'] }, String(props.count)) : null,
      ]),
      (props.open && !props.disabled) ? createElement('div', { class: 'space-y-0.5 mt-0.5' }, slots.default ? slots.default() : []) : null,
    ])
  },
})

const InstrLeaf = defineComponent({
  props: {
    ins: { type: Object as () => Instruction, required: true },
    indent: { type: Number, default: 0 },
    // When set (with `draggable`), the row can be dragged to re-file it within
    // this scope ('global' | agentId). Only passed inside directory-aware groups.
    dragScope: { type: String, default: '' },
    draggable: Boolean,
  },
  setup(props) {
    return () => {
      const ins = props.ins
      const sel = selectedId.value === ins.id
      const pending = isPending(ins)
      const visibleState = visibleInstructionState(ins)
      // Inactive (draft/archived) rows stay muted even while a change is
      // pending: the amber dot flags the pending review, a second gray dot
      // keeps the live lifecycle state visible, and the title never turns
      // amber for an instruction that isn't live.
      const inactive = (ins.status || 'published') !== 'published'
      const dragging = drag.value?.kind === 'instr' && drag.value?.id === ins.id
      // A div (not a button): the row nests its own action button, and nested
      // buttons are invalid HTML. role/tabindex/keydown keep it operable, and
      // select-none restores the button's behavior of not being text-selectable
      // — a stray selection would drag as text and never start a row drag.
      return createElement('div', {
        role: 'button',
        tabindex: 0,
        draggable: props.draggable ? 'true' : undefined,
        class: ['group w-full flex items-center gap-2 h-8 rounded-md text-[13px] transition-colors min-w-0 text-start select-none', sel ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70', dragging ? 'opacity-50' : '', props.draggable ? 'cursor-grab active:cursor-grabbing' : ''],
        // WebkitUserDrag: Safari refuses to start a drag on a plain element
        // (especially one with user-select: none) unless it is asked to treat
        // the element itself as the drag source. No effect in Chrome/Firefox.
        style: { paddingInlineStart: (20 + props.indent * 14) + 'px', paddingInlineEnd: '8px', ...(props.draggable ? { WebkitUserDrag: 'element' } : {}) },
        onClick: () => openInstruction(ins),
        onKeydown: (e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openInstruction(ins) } },
        onDragstart: props.draggable ? (e: DragEvent) => startDragInstr(props.dragScope, ins.id, e) : undefined,
        onDragend: props.draggable ? endDrag : undefined,
      }, [
        createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', pending ? 'bg-amber-400' : h.getStatusIconClass(visibleState)], title: pending ? t('agentsPage.pendingReview') : h.getStatusTooltip(visibleState) }),
        (pending && inactive) ? createElement('span', { class: 'shrink-0 w-1.5 h-1.5 rounded-full bg-gray-300 dark:bg-gray-600 -ms-1', title: h.formatStatus(ins.status) }) : null,
        createElement('span', { class: ['flex-1 text-start truncate', inactive ? 'text-gray-400 dark:text-gray-500' : (pending ? 'text-amber-700 dark:text-amber-300' : '')] }, displayTitle(ins)),
        pending ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-amber-50 dark:bg-amber-500/10 text-amber-600 dark:text-amber-400 text-[10px] font-medium', title: t('agentsPage.pendingApprovalHint') }, t('agentsPage.pendingReview')) : null,
        createElement(resolveComponent('UIcon'), { name: h.getCategoryIcon(ins.category).replace('heroicons:', 'i-heroicons-'), class: 'w-3 h-3 text-gray-300 dark:text-gray-600 shrink-0', title: h.formatCategory(ins.category) }),
        createElement(resolveComponent('UIcon'), { name: h.getSourceIcon(ins), class: 'w-3 h-3 text-gray-300 dark:text-gray-600 shrink-0', title: h.getSourceTooltip(ins) }),
        createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[11px] font-medium' }, h.getLoadModeLabel(ins.load_mode)),
        (ins.data_sources && ins.data_sources.length > 1) ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[11px] font-medium', title: ins.data_sources.map(d => d.name).join(', ') }, String(ins.data_sources.length)) : null,
      ])
    }
  },
})

// A folder in the tree: header row (rename/delete/new-subfolder on hover, drag
// to re-parent, drop target for instructions and folders) + its contents when
// open (child folders, then instructions filed directly in it). Recursive.
const DirNode = defineComponent({
  props: {
    dir: { type: Object as () => Dir, required: true },
    scope: { type: String, required: true },
    // The already-filtered instruction list for this scope (agent/global).
    list: { type: Array as () => Instruction[], default: () => [] },
    indent: { type: Number, default: 0 },
    canManage: Boolean,
  },
  setup(props) {
    return () => {
      const { dir, scope, indent } = props
      const key = 'dir:' + scope + ':' + dir.id
      const open = expanded.value.has(key)
      const kids = childDirs(scope, dir.id)
      const instrs = instrsInDir(scope, dir.id, props.list as Instruction[])
      const dropKey = 'dir:' + scope + ':' + dir.id
      const dropActive = dropTarget.value === dropKey && canDrop(scope, dir.id)
      const toggle = () => { if (open) expanded.value.delete(key); else expanded.value.add(key); expanded.value = new Set(expanded.value) }
      // The whole folder subtree (header + its rows) is ONE drop zone, handled on
      // the outer div below. stopPropagation makes the innermost folder under the
      // cursor win and stops the event bubbling to the scope-root zone — so only
      // the hovered folder's HEADER row highlights, never the whole group.
      const onDragover = (e: DragEvent) => {
        if (!canDrop(scope, dir.id)) return
        e.preventDefault(); e.stopPropagation()
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
        dropTarget.value = dropKey
      }
      const onDragleave = (e: DragEvent) => {
        e.stopPropagation()
        // Ignore moves between this folder's own descendants (prevents flicker);
        // only clear when the cursor actually leaves the folder subtree.
        const rt = e.relatedTarget as Node | null
        const ct = e.currentTarget as HTMLElement
        if (rt && ct?.contains?.(rt)) return
        if (dropTarget.value === dropKey) dropTarget.value = null
      }
      const onDrop = (e: DragEvent) => { e.preventDefault(); e.stopPropagation(); onDropInto(scope, dir.id, dropKey) }
      const header = createElement('div', {
        draggable: props.canManage ? 'true' : undefined,
        class: ['group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0 cursor-pointer', dropActive ? 'bg-blue-100 dark:bg-blue-500/20 ring-1 ring-inset ring-blue-400 dark:ring-blue-500/50 text-blue-800 dark:text-blue-200' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70'],
        style: { paddingInlineStart: (6 + indent * 14) + 'px', paddingInlineEnd: '8px', ...(props.canManage ? { WebkitUserDrag: 'element' } : {}) },
        onClick: toggle,
        onDragstart: props.canManage ? (e: DragEvent) => { e.stopPropagation(); startDragDir(scope, dir.id, e) } : undefined,
        onDragend: props.canManage ? endDrag : undefined,
      }, [
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-chevron-right', class: ['w-3 h-3 transition-transform shrink-0 text-gray-300 dark:text-gray-600', open ? 'rotate-90' : 'rtl:rotate-180'] }),
        createElement(resolveComponent('UIcon'), { name: open ? 'i-heroicons-folder-open' : 'i-heroicons-folder', class: 'w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0' }),
        createElement('span', { class: 'flex-1 text-start truncate' }, dir.name),
        (props.canManage && dir.parent_id) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.moveToTopLevel'), onClick: (e: Event) => { e.stopPropagation(); moveDirectory(scope, dir, null) } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-arrow-up-tray', class: 'w-3 h-3' })]) : null,
        props.canManage ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipNewSubfolder'), onClick: (e: Event) => { e.stopPropagation(); newDirectory(scope, dir.id) } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-folder-plus', class: 'w-3 h-3' })]) : null,
        props.canManage ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipRename'), onClick: (e: Event) => { e.stopPropagation(); renameDirectory(scope, dir) } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-pencil', class: 'w-3 h-3' })]) : null,
        props.canManage ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-400 dark:text-gray-500 hover:text-red-600 dark:hover:text-red-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: t('agentsPage.tipDeleteFolder'), onClick: (e: Event) => { e.stopPropagation(); deleteDirectory(scope, dir) } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-trash', class: 'w-3 h-3' })]) : null,
        createElement('span', { class: 'text-xs tabular-nums shrink-0 text-gray-400 dark:text-gray-500' }, String(instrs.length || '')),
      ])
      const body = open ? createElement('div', { class: 'space-y-0.5 mt-0.5' }, [
        ...kids.map(k => createElement(DirNode, { key: k.id, dir: k, scope, list: props.list, indent: indent + 1, canManage: props.canManage })),
        ...instrs.map(ins => createElement(InstrLeaf, { key: ins.id, ins, indent: indent + 1, dragScope: scope, draggable: props.canManage })),
        (!kids.length && !instrs.length) ? createElement('div', { class: 'text-[11px] text-gray-300 dark:text-gray-600 italic py-1', style: { paddingInlineStart: (20 + (indent + 1) * 14) + 'px' } }, t('agentsPage.dirEmpty')) : null,
      ]) : null
      // Outer div is the folder's drop zone (covers header + rows).
      return createElement('div', { onDragover, onDragleave, onDrop }, [header, body])
    }
  },
})

// A suite folder: header row + its cases. Flat by design — no recursion, unlike
// DirNode. Drop target for a dragged case.
const SuiteNode = defineComponent({
  props: {
    suite: { type: Object as () => any, required: true },
    scope: { type: String, required: true },
    indent: { type: Number, default: 2 },
    canManage: Boolean,
  },
  setup(props) {
    const key = () => 'suite:' + props.scope + ':' + props.suite.id
    const active = () => dropTarget.value === key() && drag.value?.kind === 'case' && drag.value?.scope === props.scope
    return () => {
      const cases = casesInSuite(props.scope, props.suite.id)
      return createElement('div', {
        // The whole subtree is the target, so a drop anywhere in the folder
        // files the case there.
        onDragover: (e: DragEvent) => {
          if (drag.value?.kind !== 'case' || drag.value?.scope !== props.scope) return
          e.preventDefault(); e.stopPropagation()
          if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
          dropTarget.value = key()
        },
        onDragleave: () => { if (dropTarget.value === key()) dropTarget.value = null },
        onDrop: (e: DragEvent) => {
          e.preventDefault(); e.stopPropagation()
          const d = drag.value
          dropTarget.value = null
          if (!d || d.kind !== 'case' || d.scope !== props.scope) { endDrag(); return }
          const id = d.id
          endDrag()
          if (String(props.suite.id) !== String((casesInSuite(props.scope, props.suite.id).find((c: any) => c.id === id) || {}).suite_id)) {
            moveCaseToSuite(props.scope, id, String(props.suite.id))
          }
        },
      }, [
        createElement(TreeGroup, {
          label: props.suite.name,
          icon: 'i-heroicons-folder',
          indent: props.indent,
          count: cases.length,
          addable: props.canManage,
          renamable: props.canManage,
          deletable: props.canManage,
          // Only offered when there is something to run — a play button on an
          // empty folder can only produce a 400.
          runnable: props.canManage && cases.length > 0,
          running: runningSuiteId.value === String(props.suite.id),
          dropActive: active(),
          open: isOpen(key()),
          onToggle: () => expand(key()),
          onAdd: () => openNewEvalCase(props.scope, String(props.suite.id)),
          onRename: () => renameSuite(props.scope, props.suite),
          onDelete: () => deleteSuite(props.scope, props.suite),
          onRun: () => runSuite(props.scope, props.suite),
        }, {
          default: () => [
            ...cases.map((c: any) => createElement(CaseLeaf, {
              key: c.id, case: c, scope: props.scope, indent: props.indent + 1,
              draggable: props.canManage,
            })),
            cases.length === 0
              ? createElement(EmptyHint, { text: t('agentsPage.noTestsInSuite'), pad: 20 + (props.indent + 1) * 14 })
              : null,
          ],
        }),
      ])
    }
  },
})

// One test case. Clicking opens it in the right pane rather than a dialog.
const CaseLeaf = defineComponent({
  props: {
    case: { type: Object as () => any, required: true },
    scope: { type: String, required: true },
    indent: { type: Number, default: 3 },
    draggable: Boolean,
  },
  setup(props) {
    return () => {
      const c = props.case
      const selected = evalCaseView.value?.caseId === String(c.id)
      return createElement('div', {
        class: ['group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0 cursor-pointer',
                selected ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70'],
        style: { paddingInlineStart: (6 + props.indent * 14) + 'px', paddingInlineEnd: '8px' },
        draggable: props.draggable ? 'true' : undefined,
        onDragstart: props.draggable ? (e: DragEvent) => startDragCase(props.scope, String(c.id), e) : undefined,
        onDragend: props.draggable ? () => endDrag() : undefined,
        onClick: () => openEvalCase(props.scope, c),
      }, [
        createElement('span', { class: 'w-3 shrink-0' }),
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-beaker', class: 'w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0' }),
        createElement('span', { class: 'flex-1 text-start truncate', title: evalCasePromptOf(c) }, evalCasePromptOf(c)),
        c.status === 'draft'
          ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-5 rounded bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400 text-[10px] font-medium' }, 'Draft')
          : null,
        c.auto_generated
          ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-5 rounded bg-purple-100 text-purple-800 dark:bg-purple-500/10 dark:text-purple-400 text-[10px] font-medium' }, 'Auto')
          : null,
      ])
    }
  },
})

const EmptyHint = defineComponent({
  props: { text: String, add: Boolean, pad: { type: Number, default: 34 } },
  emits: ['add'],
  setup(props, { emit }) {
    return () => createElement('div', { class: 'flex items-center gap-2 py-1', style: { paddingInlineStart: props.pad + 'px' } }, [
      createElement('span', { class: 'text-[11px] text-gray-300 dark:text-gray-600 italic' }, props.text),
      props.add ? createElement('button', { class: 'text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white font-medium', onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, t('agentsPage.addShort')) : null,
    ])
  },
})

const FilterSection = defineComponent({
  props: { label: String, options: { type: Array as () => { value: string; label: string }[], default: () => [] }, modelValue: { type: Array as () => string[], default: () => [] } },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const toggle = (v: string) => { const cur = [...(props.modelValue || [])]; const i = cur.indexOf(v); i >= 0 ? cur.splice(i, 1) : cur.push(v); emit('update:modelValue', cur) }
    return () => createElement('div', {}, [
      createElement('div', { class: 'text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mb-1' }, props.label),
      createElement('div', { class: 'flex flex-wrap gap-1' }, props.options.map(o => createElement('button', { key: o.value, type: 'button', class: ['px-2 h-6 rounded-md text-[11px] font-medium transition-colors', (props.modelValue || []).includes(o.value) ? 'bg-gray-900 dark:bg-gray-200 text-white dark:text-gray-900' : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'], onClick: () => toggle(o.value) }, o.label))),
    ])
  },
})

// Deep-link / URL sync. /agents (index.vue) and the catch-all [...slug].vue
// both render this component, so /agents, /agents/<id>, /agents/<id>/<panel>
// and /agents/instructions/<id> all resolve here. URLs are written with
// history.replaceState (NOT a
// router navigation) so the address bar updates without re-running the global
// middleware (auth/onboarding/permissions) or remounting/flickering the page.
const route = useRoute()
const PANEL_KINDS = ['tables', 'tools', 'evals', 'settings'] as const

// The URL that reflects the current right-pane state. Only one of agent /
// panel / instruction views is open at a time (each open() clears the others).
const explorerUrl = (): string => {
  // Global evals has no agentId — filter empty segments so it maps to
  // /agents/global-evals rather than /agents//global-evals.
  if (panelView.value) return `/agents/${[panelView.value.agentId, panelView.value.kind].filter(Boolean).join('/')}`
  if (agentView.value) return `/agents/${agentView.value.agentId}`
  if (selectedId.value && !creating.value) return `/agents/instructions/${selectedId.value}`
  return '/agents'
}
const syncUrl = () => {
  if (!process.client) return
  const target = explorerUrl()
  if (location.pathname.replace(/\/$/, '') === target) return
  try { history.replaceState({ ...history.state }, '', target) } catch {}
}
// Reflect every right-pane state change (agent / panel / instruction / close)
// in the URL from one place, so all open and close paths stay in sync.
watch([panelView, agentView, selectedId, () => creating.value], () => syncUrl())

// Restore the view from the URL on load and on back/forward navigation.
const restoreFromRoute = () => {
  const raw = route.params.slug
  const seg = (Array.isArray(raw) ? raw : (raw ? [raw] : [])).filter(Boolean) as string[]
  if (seg.length === 0) return
  // /agents/instructions/<id>
  if (seg[0] === 'instructions' && seg[1]) {
    const insId = seg[1]
    if (selectedId.value === insId) return
    const ins = allInstructions.value.find(i => i.id === insId)
    if (ins) { openInstruction(ins); return }
    // Lazy tree: the row may not be loaded — fetch the single instruction so the
    // deep link still opens it.
    useMyFetch<any>(`/api/instructions/${insId}`, { method: 'GET' })
      .then(({ data }: any) => { if (data?.value) openInstruction(data.value) })
      .catch(() => {})
    return
  }
  // /agents/global-evals — org-wide evals view, not bound to an agent
  if (seg[0] === 'global-evals') {
    if (panelView.value?.kind !== 'global-evals') openGlobalEvals()
    return
  }
  const agentId = seg[0]
  const panel = seg[1] as (typeof PANEL_KINDS)[number] | undefined
  const agent = agents.value.find(a => a.id === agentId)
  if (!agent) return
  // /agents/<id>/<panel>
  if (panel && (PANEL_KINDS as readonly string[]).includes(panel)) {
    if (panelView.value?.kind === panel && panelView.value?.agentId === agentId) return
    expand('agent:' + agentId, true)
    if ((panel === 'tables' || panel === 'tools') && !isOpen(panel + ':' + agentId)) expand(panel + ':' + agentId)
    openPanel(panel, agentId)
    return
  }
  // /agents/<id>
  if (agentView.value?.agentId === agentId) return
  expand('agent:' + agentId, true)
  openAgent(agentId)
}
watch(() => route.params.slug, () => restoreFromRoute())

// ── Activity sparkline + total tasks (org-wide, last 14 days) ───────────
const activitySeries = ref<number[]>([])
const totalTasks = ref(0)
const sparkPath = computed(() => {
  const v = activitySeries.value
  if (v.length < 2) return ''
  const w = 96, h = 26
  const max = Math.max(...v, 1), min = Math.min(...v, 0)
  const span = (max - min) || 1
  return v.map((y, i) => { const x = (i / (v.length - 1)) * w; const yy = h - ((y - min) / span) * h; return `${i ? 'L' : 'M'}${x.toFixed(1)},${yy.toFixed(1)}` }).join(' ')
})
// Per-agent activity (last 14 days). Fetched when an agent overview opens.
const fetchActivity = async (agentId?: string) => {
  activitySeries.value = []; totalTasks.value = 0
  if (!agentId) return
  try {
    const end = new Date(); const start = new Date(); start.setDate(start.getDate() - 13)
    const query: any = { start_date: start.toISOString(), end_date: end.toISOString(), data_source_ids: agentId }
    const { data: ts } = await useMyFetch<any>('/console/metrics/timeseries', { method: 'GET', query })
    if (agentView.value?.agentId !== agentId) return
    const msgs = (ts.value as any)?.activity_metrics?.messages || []
    activitySeries.value = msgs.map((p: any) => Number(p.value) || 0)
    const { data: cmp } = await useMyFetch<any>('/console/metrics/comparison', { method: 'GET', query })
    if (agentView.value?.agentId !== agentId) return
    totalTasks.value = (cmp.value as any)?.current?.total_messages ?? activitySeries.value.reduce((a, b) => a + b, 0)
  } catch {}
}

onMounted(async () => {
  // Lazy tree: load agents + aggregate counts only (no instruction rows). Each
  // group's rows load on first expand. fetchCounts also feeds the pending dots
  // and the "N pending" badge, so fetchPendingMap is no longer on the hot path.
  await Promise.all([fetchAgents(), fetchConnections(), fetchCounts(), fetchLabels(), fetchCategories(), fetchGitStatus(), fetchReviewCount()])
  instrLoading.value = false
  // fetchCounts already populated the per-row "pending" dot set from its own
  // response, so no separate org-wide /pending-changes sweep is needed here.
  restoreFromRoute()
})

// Permissions load asynchronously (whoami plugin); if they arrive after mount,
// the git-status fetch above was skipped — run it once the gate opens.
watch(canManageGit, (v) => { if (v) fetchGitStatus() })
</script>

<style scoped>
/* Full-height shell minus the optional top banner. 100dvh (where supported)
   tracks the *visible* viewport on mobile, so the connections footer isn't
   hidden behind the browser chrome; 100vh stays as the fallback. */
.ke-viewport {
  height: calc(100vh - var(--ke-banner, 0px));
  height: calc(100dvh - var(--ke-banner, 0px));
}
.prose-instruction :deep(.tiptap-prose) { min-height: 80px; }
/* Instruction body text size. */
.prose-instruction :deep(.tiptap-prose),
.prose-instruction :deep(.tiptap-prose p),
.prose-instruction :deep(.tiptap-prose li) { font-size: 13px; line-height: 1.6; }
</style>
