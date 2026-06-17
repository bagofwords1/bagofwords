<template>
  <div class="flex flex-col h-screen text-sm">
    <!-- Header -->
    <div class="flex items-center justify-between pl-3 pr-4 py-3 shrink-0">
      <div>
        <h1 class="text-lg font-semibold text-gray-900">Agents</h1>
        <p class="mt-1 text-sm text-gray-500">Configure your agents and the data, tools, skills and instructions they reason with.</p>
      </div>
      <div class="flex items-center gap-2.5">
        <button v-if="pendingCount > 0" class="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border border-amber-200 bg-amber-50 text-amber-700 text-xs font-medium hover:bg-amber-100 transition-colors" @click="expand('pending', true)">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>{{ pendingCount }} pending
        </button>
        <GitConnectionButton :has-connection="gitRepos.length > 0" :connected-repos="gitRepos" :last-indexed-at="gitLastIndexed" @click="showGitModal = true" />
        <UPopover :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-lg' }">
          <button class="inline-flex items-center gap-1.5 h-8 pl-2.5 pr-2 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors">
            <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" /> New
            <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 opacity-70" />
          </button>
          <template #panel="{ close }">
            <div class="p-1 w-52">
              <button class="w-full flex items-start gap-2.5 px-2 py-1.5 rounded-md hover:bg-gray-50 text-left" @click="openCreate(); close()">
                <UIcon name="i-heroicons-document-text" class="w-4 h-4 text-gray-400 mt-0.5 shrink-0" />
                <span><span class="block text-xs font-medium text-gray-800">Instruction</span><span class="block text-[10px] text-gray-400">A rule, skill or note for your agents</span></span>
              </button>
              <button v-if="canCreateDataSource" class="w-full flex items-start gap-2.5 px-2 py-1.5 rounded-md hover:bg-gray-50 text-left" @click="showNewAgent = true; close()">
                <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-400 mt-0.5 shrink-0" />
                <span><span class="block text-xs font-medium text-gray-800">Agent</span><span class="block text-[10px] text-gray-400">Connect data, tools and tables</span></span>
              </button>
            </div>
          </template>
        </UPopover>
      </div>
    </div>

    <!-- Body: tree → detail → versions -->
    <div class="flex-1 min-h-0 flex border-t border-gray-200">
      <!-- ── Pane 1: Tree ───────────────────────────────── -->
      <aside class="shrink-0 border-r border-gray-200 flex flex-col relative" :style="{ width: treeWidth + 'px' }">
        <div class="px-2 pt-2.5 pb-2 flex items-center gap-1.5">
          <div class="relative flex-1">
            <UIcon name="i-heroicons-magnifying-glass" class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
            <input v-model="search" type="text" placeholder="Search everything…" class="w-full h-9 pl-8 pr-2 text-[13px] bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400" />
          </div>
          <UPopover :popper="{ placement: 'bottom-end' }" :ui="{ ring: '', shadow: 'shadow-md' }">
            <button type="button" class="relative h-8 w-8 flex items-center justify-center rounded-md border border-gray-200 text-gray-500 hover:bg-gray-50" title="Filters">
              <UIcon name="i-heroicons-adjustments-horizontal" class="w-4 h-4" />
              <span v-if="activeFilterCount" class="absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full bg-gray-900 text-white text-[8px] font-semibold flex items-center justify-center">{{ activeFilterCount }}</span>
            </button>
            <template #panel="{ close }">
              <div class="p-3 w-56 space-y-3">
                <FilterSection label="Status" :options="statusOpts" v-model="fStatus" />
                <FilterSection label="Loading" :options="loadOpts" v-model="fLoad" />
                <FilterSection label="Source" :options="sourceOpts" v-model="fSource" />
                <FilterSection v-if="categoryOpts.length" label="Category" :options="categoryOpts" v-model="fCategory" />
                <div class="flex items-center justify-between pt-1 border-t border-gray-100">
                  <button class="text-[11px] text-gray-500 hover:text-gray-800" @click="clearFilters">Clear all</button>
                  <button class="text-[11px] font-medium text-gray-900" @click="close && close()">Done</button>
                </div>
              </div>
            </template>
          </UPopover>
        </div>

        <div class="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
          <TreeGroup label="Global instructions" icon="i-heroicons-globe-alt" :count="globalCount" addable :open="isOpen('global')" @toggle="expand('global')" @add="openCreate()">
            <EmptyHint v-if="listFor('global').length === 0" text="No global rules." add @add="openCreate()" />
            <InstrLeaf v-for="ins in listFor('global')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Skills" icon="i-heroicons-sparkles" :count="skillCount" :open="isOpen('skills')" @toggle="expand('skills')">
            <EmptyHint v-if="skillCount === 0" text="No skills yet." />
            <InstrLeaf v-for="ins in listFor('skills')" :key="ins.id" :ins="ins" />
          </TreeGroup>
          <TreeGroup label="Pending review" icon="i-heroicons-clock" :count="pendingCount" :count-accent="pendingCount > 0" :open="isOpen('pending')" @toggle="expand('pending')">
            <EmptyHint v-if="pendingCount === 0" text="Nothing awaiting review." />
            <InstrLeaf v-for="ins in listFor('pending')" :key="ins.id" :ins="ins" />
          </TreeGroup>

          <div class="h-px bg-gray-100 my-2 mx-1"></div>

          <div class="px-2 pt-1 pb-1 flex items-center justify-between">
            <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400">Agents</span>
            <UTooltip v-if="canViewAllAgents" :text="$t('data.showAllAgentsHint')">
              <label class="flex items-center gap-1 text-[10px] text-gray-400 cursor-pointer hover:text-gray-600 select-none">
                <UToggle v-model="showAllAgents" size="2xs" />
                <span>{{ $t('data.showAllAgents') }}</span>
              </label>
            </UTooltip>
          </div>

          <template v-for="agent in agents" :key="agent.id">
            <TreeGroup :label="agent.name" :count="agentCount(agent.id)" :pending="agentPending(agent.id)" :status-dot="agentStatusDot(agent)" :lock="agent.is_public === false" :badge="needsSignIn(agent) ? 'Sign in' : ''" :disabled="needsSignIn(agent)" :active="agentView?.agentId === agent.id" :open="isOpen('agent:' + agent.id)" @toggle="onAgentClick(agent)" @badge="openAgentTab(agent.id)">
              <template #icon><DataSourceIcon :type="agent.type" class="w-4 h-4 shrink-0" /></template>

              <TreeGroup label="Tables" icon="i-heroicons-table-cells" :count="agentTables[agent.id]?.length" :indent="1" reloadable :active="panelView?.kind === 'tables' && panelView?.agentId === agent.id" :open="isOpen('tables:' + agent.id)" @toggle="onPanelRowClick('tables', agent.id)" @reload="reloadTables(agent.id)">
                <TreeGroup v-for="t in (agentTables[agent.id] || [])" :key="t.id" :label="t.name" :icon="t.is_active ? 'i-heroicons-check-circle' : 'i-heroicons-table-cells'" :count="listForTable(agent.id, t.id).length || undefined" mono addable :indent="2" :open="isOpen('table:' + agent.id + ':' + t.id)" @toggle="expand('table:' + agent.id + ':' + t.id)" @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })">
                  <InstrLeaf v-for="ins in listForTable(agent.id, t.id)" :key="ins.id" :ins="ins" :indent="3" />
                  <EmptyHint v-if="listForTable(agent.id, t.id).length === 0" text="No rules attached." add @add="openCreate({ agentId: agent.id, tableId: t.id, tableName: t.name })" :pad="62" />
                </TreeGroup>
                <EmptyHint v-if="(agentTables[agent.id]?.length ?? -1) === 0" text="No accessible tables." :pad="48" />
              </TreeGroup>

              <TreeGroup label="Tools" icon="i-heroicons-wrench-screwdriver" :count="agentTools[agent.id]?.length" :indent="1" reloadable :active="panelView?.kind === 'tools' && panelView?.agentId === agent.id" :open="isOpen('tools:' + agent.id)" @toggle="onPanelRowClick('tools', agent.id)" @reload="reloadTools(agent.id)">
                <!-- Grouped by connection (MCP / custom API). Click a group to expand its tools. -->
                <TreeGroup v-for="grp in toolGroups(agent.id)" :key="grp.connId" :label="grp.name" :count="grp.tools.length" :indent="2" :open="isOpen('toolconn:' + agent.id + ':' + grp.connId)" @toggle="expand('toolconn:' + agent.id + ':' + grp.connId)">
                  <template #icon><DataSourceIcon v-if="grp.type" :type="grp.type" class="w-4 h-4 shrink-0" /><UIcon v-else name="i-heroicons-wrench-screwdriver" class="w-4 h-4 text-gray-400 shrink-0" /></template>
                  <div v-for="tool in grp.tools" :key="tool.id || tool.name" class="flex items-center gap-2 h-8 rounded-md text-[13px] text-gray-600" style="padding-left:62px;padding-right:8px">
                    <UIcon name="i-heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-300 shrink-0" />
                    <span class="flex-1 text-left truncate font-mono text-xs">{{ tool.name }}</span>
                    <span v-if="tool.is_enabled === false" class="text-[9px] px-1 rounded bg-gray-100 text-gray-400">off</span>
                    <span v-else-if="tool.policy && tool.policy !== 'allow'" class="text-[9px] px-1 rounded bg-gray-100 text-gray-500">{{ tool.policy }}</span>
                  </div>
                </TreeGroup>
                <EmptyHint v-if="(agentTools[agent.id]?.length ?? -1) === 0" text="No tools connected." :pad="48" />
              </TreeGroup>

              <TreeGroup label="Files" icon="i-heroicons-paper-clip" :count="agentFiles[agent.id]?.length" :indent="1" addable :open="isOpen('files:' + agent.id)" @toggle="expand('files:' + agent.id)" @add="triggerUpload(agent.id)">
                <div
                  v-for="f in (agentFiles[agent.id] || [])" :key="f.id"
                  class="group/file w-full flex items-center gap-2 h-8 rounded-md text-[13px] transition-colors min-w-0 cursor-pointer"
                  :class="previewFile && previewFile.id === f.id ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'"
                  style="padding-left:48px;padding-right:8px" @click="openFile(f, agent.id)"
                >
                  <UIcon :name="fileIcon(f.content_type, f.filename)" class="w-3.5 h-3.5 text-gray-400 shrink-0" />
                  <span class="flex-1 text-left truncate">{{ f.filename }}</span>
                  <button v-if="canManageAgent(agent.id)" class="shrink-0 w-4 h-4 rounded hover:bg-gray-200 text-gray-400 hover:text-red-600 opacity-0 group-hover/file:opacity-100 flex items-center justify-center" title="Delete file" @click.stop="deleteFile(agent.id, f)"><UIcon name="i-heroicons-trash" class="w-3 h-3" /></button>
                </div>
                <EmptyHint v-if="(agentFiles[agent.id]?.length ?? -1) === 0" text="No files." add @add="triggerUpload(agent.id)" :pad="48" />
                <div v-if="uploadingAgent === agent.id" class="text-[11px] text-gray-400 italic py-1" style="padding-left:48px">Uploading…</div>
              </TreeGroup>

              <TreeGroup label="Instructions" icon="i-heroicons-document-text" :count="listForAgent(agent.id).length" addable :indent="1" :open="isOpen('instr:' + agent.id)" @toggle="expand('instr:' + agent.id)" @add="openCreate({ agentId: agent.id })">
                <InstrLeaf v-for="ins in listForAgent(agent.id)" :key="ins.id" :ins="ins" :indent="2" />
                <EmptyHint v-if="listForAgent(agent.id).length === 0" text="No instructions yet." add @add="openCreate({ agentId: agent.id })" :pad="48" />
              </TreeGroup>

              <button v-if="canManageAgent(agent.id)" type="button" class="group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0" :class="panelView?.kind === 'evals' && panelView?.agentId === agent.id ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'" style="padding-left:20px;padding-right:8px" @click="openPanel('evals', agent.id)">
                <span class="w-3 shrink-0"></span>
                <UIcon name="i-heroicons-check-circle" class="w-4 h-4 text-gray-400 shrink-0" />
                <span class="flex-1 text-left truncate">Evals</span>
                <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 text-gray-300 shrink-0 opacity-0 group-hover:opacity-100" />
              </button>

              <button v-if="canManageAgent(agent.id)" type="button" class="group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0" :class="panelView?.kind === 'settings' && panelView?.agentId === agent.id ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'" style="padding-left:20px;padding-right:8px" @click="openPanel('settings', agent.id)">
                <span class="w-3 shrink-0"></span>
                <UIcon name="i-heroicons-cog-6-tooth" class="w-4 h-4 text-gray-400 shrink-0" />
                <span class="flex-1 text-left truncate">Settings</span>
                <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 text-gray-300 shrink-0 opacity-0 group-hover:opacity-100" />
              </button>
            </TreeGroup>
          </template>
        </div>

        <!-- Connections footer -->
        <div class="border-t border-gray-200 px-3 py-2 flex items-center gap-2">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 mr-1">Connections</span>
          <UTooltip v-for="c in connections.slice(0, 4)" :key="c.id" :text="`${c.name} · ${c.type}`">
            <button type="button" class="relative inline-flex items-center justify-center w-6 h-6 rounded-md border border-gray-200 hover:bg-gray-50" @click="openConnectionDetail(c)">
              <DataSourceIcon :type="c.type" class="w-3.5 h-3.5" />
              <span class="absolute -bottom-0.5 -right-0.5 w-1.5 h-1.5 rounded-full" :class="c.is_active === false ? 'bg-gray-300' : 'bg-green-500'"></span>
            </button>
          </UTooltip>
          <UTooltip v-if="connections.length > 4" :text="`View all ${connections.length} connections`">
            <button type="button" class="inline-flex items-center justify-center h-6 px-1.5 rounded-md border border-gray-200 text-[11px] font-medium text-gray-500 hover:bg-gray-50" @click="showConnectionsModal = true">+{{ connections.length - 4 }}</button>
          </UTooltip>
          <UTooltip v-if="canCreateDataSource" text="New connection">
            <button type="button" class="inline-flex items-center justify-center w-6 h-6 rounded-md border border-dashed border-gray-300 text-gray-400 hover:bg-gray-50 hover:text-gray-600" @click="connTargetAgentId = null; showAddConnection = true">
              <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />
            </button>
          </UTooltip>
          <button v-if="connections.length" type="button" class="ml-auto text-[11px] text-gray-400 hover:text-gray-700" @click="showConnectionsModal = true">View all</button>
        </div>

        <!-- Drag handle to resize the tree pane -->
        <div class="absolute top-0 right-0 h-full w-1 cursor-col-resize hover:bg-gray-300 transition-colors z-10" title="Drag to resize" @mousedown="startTreeResize"></div>
      </aside>

      <!-- ── Pane 2: Detail ───────────────────────────── -->
      <section class="flex-1 min-w-0 flex flex-col">
        <!-- Agent overview -->
        <template v-if="agentView">
          <div class="shrink-0 px-6 pt-4 pb-4 border-b border-gray-100">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 min-w-0">
                  <DataSourceIcon v-if="agentDetail" :type="agentDetail.type" class="w-4 h-4 shrink-0" />
                  <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="(agentDetail?.status || 'active') === 'active' ? 'bg-green-500' : 'bg-gray-300'" :title="(agentDetail?.status || 'active') === 'active' ? 'Active' : 'Inactive'"></span>
                  <h2 class="text-base font-semibold text-gray-900 truncate">{{ agentDetail?.name || agentViewName }}</h2>
                  <UPopover v-if="agentCanUpdate" :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-md' }">
                    <button type="button" class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium shrink-0 transition-colors" :class="agentDetail?.is_public ? 'border-blue-200 bg-blue-50 text-blue-600 hover:bg-blue-100' : 'border-gray-200 bg-gray-50 text-gray-500 hover:bg-gray-100'">
                      <UIcon :name="agentDetail?.is_public ? 'i-heroicons-globe-alt' : 'i-heroicons-lock-closed'" class="w-3 h-3" />{{ agentDetail?.is_public ? 'Public' : 'Private' }}
                      <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 opacity-60" />
                    </button>
                    <template #panel="{ close }">
                      <div class="p-1 w-40">
                        <button class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-gray-50 text-left" @click="setAgentPublic(true); close()"><UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-gray-400" />Public<UIcon v-if="agentDetail?.is_public" name="i-heroicons-check" class="w-3 h-3 ml-auto text-gray-900" /></button>
                        <button class="w-full flex items-center gap-2 px-2 py-1.5 text-[11px] rounded hover:bg-gray-50 text-left" @click="setAgentPublic(false); close()"><UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 text-gray-400" />Private<UIcon v-if="!agentDetail?.is_public" name="i-heroicons-check" class="w-3 h-3 ml-auto text-gray-900" /></button>
                      </div>
                    </template>
                  </UPopover>
                  <span v-else class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium shrink-0" :class="agentDetail?.is_public ? 'border-blue-200 bg-blue-50 text-blue-600' : 'border-gray-200 bg-gray-50 text-gray-500'"><UIcon :name="agentDetail?.is_public ? 'i-heroicons-globe-alt' : 'i-heroicons-lock-closed'" class="w-3 h-3" />{{ agentDetail?.is_public ? 'Public' : 'Private' }}</span>
                  <PublishStatusControl v-if="agentDetail" :key="agentView.agentId" :data-source-id="agentView.agentId" :status="agentDetail.publish_status || 'published'" @updated="onAgentPublishUpdated" />
                  <!-- Auth badges (parity with the legacy agents page) -->
                  <UTooltip v-if="agentDetail && usesServiceAccount(agentDetail)" text="Runs via the connection's service account (admin/owner fallback) — no personal sign-in needed">
                    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 h-5 rounded shrink-0 bg-emerald-50 text-emerald-700"><UIcon name="i-heroicons-cpu-chip" class="w-2.5 h-2.5" />Service account</span>
                  </UTooltip>
                  <UTooltip v-if="agentListItem?.admin_only" text="Visible to you via admin access — you are not a member of this agent">
                    <span class="inline-flex items-center gap-1 text-[10px] px-1.5 h-5 rounded shrink-0 bg-amber-100 text-amber-700 uppercase tracking-wide font-medium"><UIcon name="i-heroicons-shield-check" class="w-2.5 h-2.5" />Admin</span>
                  </UTooltip>
                </div>
                <div class="mt-1.5 group">
                  <input v-if="editingDesc" ref="descInputRef" v-model="descForm" type="text" placeholder="Add a description…" class="w-full text-sm text-gray-600 border-b border-blue-400 bg-transparent outline-none py-0.5" @keydown.enter="saveDesc" @keydown.escape="cancelDesc" @blur="saveDesc" />
                  <div v-else class="flex items-center gap-2">
                    <p class="text-sm text-gray-500 rounded px-1 -mx-1" :class="agentCanUpdate ? 'cursor-pointer hover:bg-gray-100' : ''" @click="agentCanUpdate && startEditDesc()">{{ agentDetail?.description || (agentCanUpdate ? 'Add a description…' : '') }}</p>
                    <button v-if="agentCanUpdate" class="text-[10px] text-blue-600 hover:underline opacity-0 group-hover:opacity-100 shrink-0" @click="startEditDesc">Edit</button>
                  </div>
                </div>
              </div>
              <div class="flex items-center gap-2 shrink-0">
                <!-- Per-agent activity sparkline + task total -->
                <div v-if="activitySeries.length" class="flex items-center gap-2.5 pr-1" title="Tasks over the last 14 days">
                  <span class="flex flex-col items-center leading-none">
                    <svg width="78" height="20" viewBox="0 0 96 26" preserveAspectRatio="none" class="overflow-visible"><path :d="sparkPath" fill="none" stroke="#10b981" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" /></svg>
                    <span class="mt-1 text-[10px] text-gray-400">Activity</span>
                  </span>
                  <span class="flex flex-col items-start leading-none">
                    <span class="text-sm font-semibold text-gray-900 tabular-nums">{{ totalTasks.toLocaleString() }}</span>
                    <span class="mt-1 text-[10px] text-gray-400">tasks</span>
                  </span>
                </div>
                <button class="h-7 px-2.5 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 inline-flex items-center gap-1" @click="createReportForAgent(agentView.agentId)"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />New report</button>
                <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" @click="exitAgentView"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
              </div>
            </div>
          </div>
          <div class="flex-1 overflow-y-auto px-6 py-5 max-w-3xl">
            <div v-if="agentDetailLoading" class="flex items-center justify-center py-16 text-gray-400">
              <Spinner class="w-5 h-5 animate-spin" />
            </div>
            <template v-else>
            <!-- Connections / Connect -->
            <div class="flex flex-wrap items-center gap-1.5 mb-3">
              <button v-for="c in (agentDetail?.connections || [])" :key="c.id" class="inline-flex items-center gap-1.5 px-2 h-6 rounded-md border border-gray-200 text-gray-600 text-[11px] hover:bg-gray-50" @click="openConnectionDetail(c)">
                <DataSourceIcon :type="c.type" class="w-3.5 h-3.5" />{{ c.name }}
                <span class="w-1.5 h-1.5 rounded-full" :class="c.is_active === false ? 'bg-gray-300' : 'bg-green-500'"></span>
              </button>
              <button v-if="agentDetail && needsSignIn(agentDetail)" class="inline-flex items-center gap-1.5 px-2.5 h-6 rounded-md bg-blue-50 border border-blue-200 text-blue-600 text-[11px] font-medium hover:bg-blue-100" @click="openAgentTab(agentView.agentId)"><UIcon name="i-heroicons-key" class="w-3 h-3" />Connect</button>
            </div>

            <!-- Counts (clean) -->
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 mb-6 pb-5 border-b border-gray-100">
              <span class="inline-flex items-center gap-1"><UIcon name="i-heroicons-table-cells" class="w-3.5 h-3.5 text-gray-400" />{{ agentTables[agentView.agentId]?.length ?? '–' }} tables</span>
              <span class="inline-flex items-center gap-1"><UIcon name="i-heroicons-wrench-screwdriver" class="w-3.5 h-3.5 text-gray-400" />{{ agentTools[agentView.agentId]?.length ?? '–' }} tools</span>
              <span class="inline-flex items-center gap-1"><UIcon name="i-heroicons-paper-clip" class="w-3.5 h-3.5 text-gray-400" />{{ agentFiles[agentView.agentId]?.length ?? '–' }} files</span>
              <span class="inline-flex items-center gap-1"><UIcon name="i-heroicons-document-text" class="w-3.5 h-3.5 text-gray-400" />{{ agentCount(agentView.agentId) }} instructions</span>
            </div>

            <!-- Primary instruction (inline, clean editor) -->
            <div v-if="creatingPrimary || editingPrimary">
              <div class="flex items-center justify-between gap-2 mb-2">
                <input v-model="primaryDraft.title" type="text" placeholder="Untitled" class="flex-1 min-w-0 text-sm font-medium text-gray-900 bg-transparent outline-none placeholder:text-gray-300" />
                <div class="flex items-center gap-1.5 shrink-0">
                  <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancelPrimary">Cancel</button>
                  <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50" :disabled="primarySaving || !primaryDraft.text.trim()" @click="savePrimary">{{ primarySaving ? 'Saving…' : 'Save' }}</button>
                </div>
              </div>
              <div class="prose-instruction">
                <InstructionEditor key="primary-edit" v-model="primaryDraft.text" mode="wysiwyg" :editable="true" :data-source-ids="[agentView.agentId]" placeholder="Write the agent's primary instruction in markdown… (type @ to mention a table or instruction)" />
              </div>
            </div>
            <template v-else-if="agentDetail?.primary_instruction">
              <div class="flex items-center justify-between gap-2 mb-1.5">
                <span class="text-sm font-medium text-gray-800">{{ agentDetail.primary_instruction.title || 'Primary instruction' }}</span>
                <button v-if="agentCanUpdate" class="text-[11px] text-blue-600 hover:underline" @click="startEditPrimary">Edit</button>
              </div>
              <InstructionText :text="agentDetail.primary_instruction.text" :references="agentDetail.primary_instruction.references || []" :prose="true" :markdown="true" />
            </template>
            <div v-else class="rounded-xl border border-dashed border-gray-200 bg-gray-50/40 px-6 py-8 text-center">
              <div class="mx-auto w-10 h-10 rounded-full bg-blue-50 flex items-center justify-center mb-3">
                <UIcon name="i-heroicons-document-text" class="w-5 h-5 text-blue-500" />
              </div>
              <p class="text-sm font-medium text-gray-800">No primary instruction</p>
              <p class="mt-1 max-w-md mx-auto text-xs text-gray-500">Give this agent a guiding instruction it applies to every report — context about the data, conventions to follow, or rules to enforce.</p>
              <div v-if="agentCanUpdate" class="mt-4 flex items-center justify-center gap-3">
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors" @click="startCreatePrimary"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />Add primary instruction</button>
                <span class="text-xs text-gray-400">or</span>
                <PrimaryInstructionPicker :agent-id="agentView.agentId" label="select existing" @select="onSelectExistingPrimary" />
              </div>
            </div>

            <!-- Conversation starters (editable) -->
            <div class="mt-6">
              <div class="flex items-center gap-2 mb-2">
                <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400">Conversation starters</span>
                <button v-if="agentCanUpdate" class="text-[10px] text-blue-600 hover:underline" @click="openEditStarters">Edit</button>
              </div>
              <div v-if="(agentDetail?.conversation_starters || []).length" class="flex flex-wrap gap-2">
                <button v-for="(cs, i) in agentDetail.conversation_starters" :key="i" type="button" :disabled="startingReport" class="group/cs inline-flex items-center gap-1.5 bg-gray-100 rounded-lg px-3 py-2 text-xs text-gray-700 hover:bg-gray-900 hover:text-white disabled:opacity-50 transition-colors" @click="startReportWithStarter(agentView.agentId, cs, i)">
                  <Spinner v-if="startingReport && startingStarterIdx === i" class="w-3 h-3 animate-spin shrink-0" />
                  <span>{{ starterTitle(cs) }}</span>
                </button>
              </div>
              <p v-else class="text-[11px] text-gray-300 italic">No conversation starters.</p>
            </div>
            </template>
          </div>
        </template>

        <!-- Tables / Tools editable panel -->
        <template v-else-if="panelView">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100">
            <div class="flex items-center gap-1.5 min-w-0">
              <button type="button" class="flex items-center gap-1.5 min-w-0 rounded px-1 -mx-1 hover:bg-gray-100" title="Open agent" @click="openAgent(panelView.agentId)">
                <DataSourceIcon :type="panelAgent?.type" class="w-[18px] h-[18px] shrink-0" />
                <span class="text-[13px] font-medium text-gray-700 truncate hover:text-gray-900">{{ panelAgent?.name || 'Agent' }}</span>
              </button>
              <UIcon name="i-heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 shrink-0" />
              <span class="text-[13px] text-gray-500 shrink-0">{{ panelKindLabel }}</span>
              <span v-if="(panelView.kind === 'tables' || panelView.kind === 'tools') && !panelCanUpdate" class="text-[11px] px-1.5 h-4 inline-flex items-center rounded bg-gray-100 text-gray-400 shrink-0">read-only</span>
            </div>
            <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" @click="closePanel"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
          </div>
          <div class="flex-1 overflow-auto">
            <AgentEvalsPanel v-if="panelView.kind === 'evals'" :key="'evals-' + panelView.agentId" :agent-id="panelView.agentId" />
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
              />
              <ToolsSelector
                v-else
                :key="'tools-' + panelView.agentId + '-' + toolsRefreshKey"
                :ds-id="panelView.agentId"
                :connections="panelConnections"
                :can-update="panelCanUpdate"
                @add-mcp="openAddMcp(panelView.agentId)"
                @add-custom-api="openAddCustomApi(panelView.agentId)"
                @edit-connection="openConnectionDetail"
                @delete-connection="onToolsConnectionChanged"
              />
            </div>
          </div>
        </template>

        <!-- File preview -->
        <template v-else-if="previewFile">
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100">
            <div class="flex items-center gap-2 min-w-0">
              <UIcon :name="fileIcon(previewFile.content_type, previewFile.filename)" class="w-4 h-4 text-gray-400 shrink-0" />
              <span class="text-xs font-medium text-gray-700 truncate">{{ previewFile.filename }}</span>
              <span class="text-[10px] text-gray-300 shrink-0">{{ previewFile.content_type }}</span>
            </div>
            <div class="flex items-center gap-1.5">
              <button v-if="previewUrl" class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="downloadPreview">Open</button>
              <button v-if="previewFileAgentId && canManageAgent(previewFileAgentId)" class="h-7 px-3 rounded-md border border-gray-200 text-red-600 text-xs font-medium hover:bg-red-50" @click="deleteFile(previewFileAgentId, previewFile)">Delete</button>
              <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" @click="closePreview"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
            </div>
          </div>
          <div class="flex-1 overflow-auto p-6">
            <div v-if="previewLoading" class="text-center text-xs text-gray-400 py-10">Loading…</div>
            <img v-else-if="isImage(previewFile) && previewUrl" :src="previewUrl" class="max-w-full rounded-lg border border-gray-200" />
            <iframe v-else-if="isPdf(previewFile) && previewUrl" :src="previewUrl" class="w-full h-[72vh] rounded-lg border border-gray-200"></iframe>
            <pre v-else-if="previewText !== null" class="text-xs text-gray-800 whitespace-pre-wrap font-mono bg-gray-50 border border-gray-200 rounded-lg p-4 overflow-auto">{{ previewText }}</pre>
            <div v-else class="text-center text-sm text-gray-400 py-10">
              <UIcon :name="fileIcon(previewFile.content_type, previewFile.filename)" class="w-9 h-9 mx-auto text-gray-200" />
              <p class="mt-2">No inline preview for this file type.</p>
              <button v-if="previewUrl" class="mt-2 text-xs text-gray-700 underline" @click="downloadPreview">Open file</button>
            </div>
          </div>
        </template>

        <template v-else-if="detail || creating">
          <!-- Header: status + actions -->
          <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100">
            <div class="flex items-center gap-2 min-w-0">
              <template v-if="creating">
                <span class="text-xs font-medium text-gray-500">New instruction</span>
              </template>
              <template v-else>
                <span class="w-1.5 h-1.5 rounded-full" :class="h.getStatusIconClass(detail)"></span>
                <span class="text-xs font-medium text-gray-500">{{ h.getStatusLabel(detail) }}</span>
              </template>
            </div>
            <div class="flex items-center gap-1.5">
              <span v-if="savingMeta" class="text-[10px] text-gray-400">Saving…</span>
              <button v-if="!creating" class="h-7 w-7 rounded-md flex items-center justify-center transition-colors" :class="showHistory ? 'bg-gray-100 text-gray-700' : 'text-gray-400 hover:bg-gray-100'" title="Version history" @click="showHistory = !showHistory">
                <UIcon name="i-heroicons-clock" class="w-4 h-4" />
              </button>
              <template v-if="!editing && !diff">
                <button class="h-7 px-3 rounded-md border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="startEdit">Edit</button>
              </template>
              <template v-else-if="!diff">
                <button class="h-7 px-3 rounded-md text-gray-500 text-xs hover:bg-gray-100" @click="cancelEdit">Cancel</button>
                <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50" :disabled="saving" @click="save">{{ saving ? 'Saving…' : (creating ? 'Create' : 'Save') }}</button>
              </template>
            </div>
          </div>

          <!-- Diff view (version compare / suggestion) -->
          <div v-if="diff" class="flex-1 flex flex-col min-h-0">
            <div class="px-6 py-3 flex items-center justify-between border-b border-gray-100">
              <div class="flex items-center gap-2 min-w-0">
                <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="activeSuggestion?.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
                <span class="text-xs font-medium text-gray-700 truncate">{{ diff.title }}</span>
                <span v-if="diff.buildId && hunkCount" class="text-[11px] text-gray-400 shrink-0 tabular-nums">· {{ hunkCount }} change{{ hunkCount === 1 ? '' : 's' }}</span>
              </div>
              <div class="flex items-center gap-1.5">
                <template v-if="diff.buildId && canApprove">
                  <button class="inline-flex items-center px-2 py-1 rounded-md text-[11px] font-medium text-gray-500 hover:text-gray-700 hover:bg-gray-100 disabled:opacity-40 transition-colors" :disabled="resolving !== null || !hunkCount" @click="rejectAll">{{ resolving === 'reject-all' ? 'Rejecting…' : 'Reject all' }}</button>
                  <button class="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-gray-50 hover:bg-gray-100 border border-gray-150 text-[11px] font-medium text-gray-700 disabled:opacity-40 transition-colors" :disabled="resolving !== null || !hunkCount" @click="acceptAll"><UIcon :name="resolving === 'all' ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5 text-green-600', { 'animate-spin': resolving === 'all' }]" />{{ resolving === 'all' ? 'Accepting…' : 'Accept all' }}</button>
                </template>
                <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" title="Close" @click="closeDiff"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
              </div>
            </div>
            <!-- Run this suggestion's evals (validate the candidate build) -->
            <div v-if="diff.buildId && canManageTests" class="px-6 py-3 border-b border-gray-100 bg-gray-50/40 shrink-0">
              <div v-if="evalSuiteOptions.length" class="flex items-center gap-2">
                <UIcon name="i-heroicons-beaker" class="w-3.5 h-3.5 text-gray-400 shrink-0" />
                <select v-model="selectedEvalSuiteId" class="h-7 flex-1 min-w-0 text-xs border border-gray-200 rounded-md px-2 bg-white text-gray-700 outline-none">
                  <option v-for="o in evalSuiteOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
                <button class="h-7 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50 inline-flex items-center gap-1 shrink-0" :disabled="!selectedEvalSuiteId || evalRunning || !evalHasCases" @click="runSuggestionEval">
                  <UIcon :name="evalRunning ? 'i-heroicons-arrow-path' : 'i-heroicons-play'" :class="['w-3 h-3', { 'animate-spin': evalRunning }]" />
                  {{ evalRunning ? 'Running…' : 'Run eval' }}
                </button>
              </div>
              <p v-else class="text-[11px] text-gray-400">No test cases yet — create them in <NuxtLink to="/evals" class="text-blue-600 hover:underline">/evals</NuxtLink>.</p>

              <!-- Active / latest run progress -->
              <div v-if="evalActiveRun" class="mt-2 bg-white border border-gray-200 rounded-lg p-2.5 space-y-1.5">
                <div class="flex items-center justify-between">
                  <div class="flex items-center gap-1.5">
                    <UIcon :name="evalActiveRun.status === 'in_progress' ? 'i-heroicons-arrow-path' : (evalActiveRun.status === 'success' ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle')" :class="['w-3.5 h-3.5', evalActiveRun.status === 'in_progress' ? 'text-blue-500 animate-spin' : (evalActiveRun.status === 'success' ? 'text-green-500' : 'text-red-500')]" />
                    <span class="text-xs font-medium text-gray-700">{{ evalPrettyStatus(evalActiveRun.status) }}</span>
                  </div>
                  <NuxtLink :to="`/evals/runs/${evalActiveRun.id}`" class="text-[10px] text-blue-500 hover:underline inline-flex items-center gap-0.5">View details<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" /></NuxtLink>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 text-[10px]">
                  <span class="px-1.5 py-0.5 rounded border bg-slate-50 text-slate-600 border-slate-200">Cases: {{ evalSummary.total }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-green-50 text-green-700 border-green-200">Pass: {{ evalSummary.passed }}</span>
                  <span class="px-1.5 py-0.5 rounded border bg-red-50 text-red-700 border-red-200">Fail: {{ evalSummary.failed }}</span>
                  <span v-if="evalSummary.inProgress" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200"><UIcon name="i-heroicons-arrow-path" class="w-2.5 h-2.5 animate-spin" />Running: {{ evalSummary.inProgress }}</span>
                </div>
                <div v-if="evalActiveRun.status === 'in_progress'" class="w-full bg-gray-100 rounded-full h-1.5">
                  <div class="bg-blue-500 h-1.5 rounded-full transition-all duration-300" :style="{ width: `${evalSummary.progressPercent}%` }" />
                </div>
              </div>
            </div>

            <div class="flex-1 min-h-0 overflow-auto px-8 py-6 max-w-3xl">
              <!-- Inline per-hunk review for suggestions. Clean tracked changes;
                   hover a change to reveal provenance + accept/reject. -->
              <template v-if="diff.buildId">
                <div v-if="!hunkCount" class="text-center text-xs text-gray-400 py-10">No remaining changes — this suggestion is resolved.</div>
                <div v-else class="text-[15px] leading-[1.7] whitespace-pre-wrap break-words text-gray-800">
                  <template v-for="(seg, si) in hunks" :key="si">
                    <span v-if="seg.kind === 'context'">{{ seg.text }}</span>
                    <span v-else class="group/h relative inline-block align-baseline rounded transition-shadow" :class="resolving === seg.idx ? 'ring-2 ring-gray-900/15' : 'hover:ring-2 hover:ring-gray-900/10'">
                      <template v-for="(op, oi) in seg.ops" :key="oi">
                        <del v-if="op.type === -1" class="text-red-400 line-through decoration-red-300 decoration-1">{{ op.text }}</del>
                        <ins v-else class="text-emerald-700 bg-emerald-50 rounded px-px no-underline">{{ op.text }}</ins>
                      </template>
                      <span v-if="resolving === seg.idx" class="absolute inset-0 rounded bg-white/50 flex items-center justify-center"><UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-500 animate-spin" /></span>

                      <!-- Hover card: provenance + per-hunk actions -->
                      <span v-if="canApprove" class="invisible opacity-0 group-hover/h:visible group-hover/h:opacity-100 transition-opacity absolute z-30 top-full left-0 mt-1.5 w-64 cursor-default select-none rounded-xl bg-white shadow-lg ring-1 ring-gray-200 p-3 text-left whitespace-normal" @click.stop>
                        <span class="flex items-center gap-1.5 text-[11px] text-gray-500">
                          <span class="w-1.5 h-1.5 rounded-full" :class="activeSuggestion?.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
                          <span class="font-medium text-gray-700">{{ activeSuggestion?.source === 'ai' ? 'AI suggestion' : 'Proposed change' }}</span>
                          <span v-if="activeSuggestion?.created_at">· {{ fmtDate(activeSuggestion.created_at) }}</span>
                        </span>
                        <span v-if="activeSuggestion?.created_by?.name" class="block mt-1 text-[11px] text-gray-400">by {{ activeSuggestion.created_by.name }}</span>
                        <span v-if="activeSuggestion?.build_title" class="block mt-1.5 text-[12px] font-medium text-gray-800">{{ activeSuggestion.build_title }}</span>
                        <span v-if="activeSuggestion?.message" class="block mt-0.5 text-[11px] text-gray-500 line-clamp-3">{{ activeSuggestion.message }}</span>
                        <button v-if="activeSuggestion?.completion_id || activeSuggestion?.report_id" type="button" class="mt-2 inline-flex items-center gap-1 text-[11px] text-blue-600 hover:underline" @click.stop="openTrace(activeSuggestion)"><UIcon name="i-heroicons-arrows-pointing-out" class="w-3 h-3" />View trace</button>
                        <span class="mt-2.5 pt-2.5 border-t border-gray-100 flex items-center gap-1.5">
                          <button class="inline-flex items-center gap-1 px-2 py-1 rounded-md bg-gray-50 hover:bg-gray-100 border border-gray-150 text-[11px] font-medium text-gray-700 disabled:opacity-40" :disabled="resolving !== null" @click.stop="acceptHunk(seg.idx)"><UIcon name="i-heroicons-check" class="w-3.5 h-3.5 text-green-600" />Accept</button>
                          <button class="inline-flex items-center gap-1 px-2 py-1 rounded-md hover:bg-gray-100 text-[11px] font-medium text-gray-500 hover:text-gray-700 disabled:opacity-40" :disabled="resolving !== null" @click.stop="rejectHunk(seg.idx)"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />Reject</button>
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
            <!-- Pending-change banner: clear, always-visible affordance to review -->
            <button v-if="!editing && !creating && pendingBuilds.length" type="button" class="shrink-0 flex items-center gap-2 px-8 py-2 border-b border-amber-100 bg-amber-50/60 text-left hover:bg-amber-50 transition-colors" @click="viewSuggestion(pendingBuilds[0])">
              <span class="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0"></span>
              <span class="text-[12px] text-amber-800">{{ pendingBuilds.length === 1 ? 'A pending change is waiting for review' : `${pendingBuilds.length} pending changes are waiting for review` }}</span>
              <span class="ml-auto text-[11px] font-medium text-amber-700 inline-flex items-center gap-0.5 shrink-0">Review<UIcon name="i-heroicons-arrow-right" class="w-3 h-3" /></span>
            </button>
            <!-- Scrollable content: title + body -->
            <div class="flex-1 overflow-y-auto px-8 py-6 w-full">
              <div class="max-w-3xl">
                <input v-if="editing" v-model="draft.title" placeholder="Untitled instruction" class="w-full text-lg font-semibold text-gray-900 outline-none placeholder:text-gray-300 mb-4" />
                <h2 v-else class="text-lg font-semibold text-gray-900 mb-4">{{ displayTitle(detail) }}</h2>
                <div class="prose-instruction">
                  <InstructionEditor :key="(detail?.id || 'new') + (editing ? '-edit' : '-view')" v-model="draft.text" mode="wysiwyg" :editable="editing" :data-source-ids="draft.data_source_ids" :is-all-data-sources="draft.data_source_ids.length === 0" placeholder="Write the instruction in markdown… (type @ to mention a table or instruction)" />
                </div>
              </div>
            </div>

            <!-- Frozen bottom panel: Details (compact, horizontal) / Analyze tabs -->
            <div v-if="detail || creating" class="shrink-0 border-t border-gray-100 bg-gray-50/40">
              <div class="px-8 flex items-stretch gap-1 border-b border-gray-100/70">
                <button type="button" class="flex items-center gap-1.5 py-2 text-[11px] font-medium border-b-2 -mb-px transition-colors" :class="bottomTab === 'details' ? 'border-gray-900 text-gray-900' : 'border-transparent text-gray-400 hover:text-gray-700'" @click="bottomTab = 'details'"><UIcon name="i-heroicons-adjustments-horizontal" class="w-3.5 h-3.5" />Details</button>
                <button v-if="detail" type="button" class="flex items-center gap-1.5 py-2 ml-3 text-[11px] font-medium border-b-2 -mb-px transition-colors" :class="bottomTab === 'analyze' ? 'border-gray-900 text-gray-900' : 'border-transparent text-gray-400 hover:text-gray-700'" @click="openAnalyzeTab"><UIcon name="i-heroicons-chart-bar" class="w-3.5 h-3.5" />Analyze</button>
              </div>

              <!-- Details: compact horizontal pills (inline-editable for admins) -->
              <div v-if="bottomTab === 'details'" class="px-8 py-3 w-full overflow-y-auto" style="max-height:34vh">
                <div class="max-w-4xl flex flex-wrap items-center gap-1.5">
                  <!-- Status -->
                  <KSelect v-if="metaEditable" v-model="draft.status" :options="statusEditOpts" @update:modelValue="onMetaChange" />
                  <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium">{{ h.getStatusLabel(detail) }}</span>
                  <!-- Loading -->
                  <KSelect v-if="metaEditable" v-model="draft.load_mode" :options="loadOpts" icon="i-heroicons-bolt" @update:modelValue="onMetaChange" />
                  <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium"><UIcon name="i-heroicons-bolt" class="w-3 h-3 mr-1 text-gray-400" />{{ h.getLoadModeLabel(detail.load_mode) }}</span>
                  <!-- Category -->
                  <KSelect v-if="metaEditable" v-model="draft.category" :options="categoryOpts" placeholder="General" @update:modelValue="onMetaChange" />
                  <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium">{{ h.formatCategory(detail.category) }}</span>
                  <!-- Agents -->
                  <KSelect v-if="metaEditable" v-model="draft.data_source_ids" :options="agentOpts" multiple placeholder="All agents" icon="i-heroicons-cube" @update:modelValue="onMetaChange" />
                  <template v-else>
                    <span v-if="(detail.data_sources || []).length === 0" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px]"><UIcon name="i-heroicons-globe-alt" class="w-3 h-3 text-gray-400" />All agents</span>
                    <span v-for="ds in detail.data_sources" :key="ds.id" class="inline-flex items-center gap-1 px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px]"><DataSourceIcon :type="ds.type" class="w-3 h-3" />{{ ds.name }}</span>
                  </template>
                  <!-- References -->
                  <span v-for="(r, i) in draft.references" :key="'ref'+i" class="inline-flex items-center gap-1 pl-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px] font-mono" :class="metaEditable ? 'pr-1' : 'pr-2'">
                    <UIcon :name="h.getRefIcon(r.object_type)" class="w-3 h-3 text-gray-400" />{{ r.display_text || r.object_id }}
                    <button v-if="metaEditable" type="button" class="w-3.5 h-3.5 rounded hover:bg-gray-200 flex items-center justify-center" @click="removeRef(i); onMetaChange()"><UIcon name="i-heroicons-x-mark" class="w-2.5 h-2.5" /></button>
                  </span>
                  <KSelect v-if="metaEditable && refOptions.length" v-model="refIds" :options="refOptions" multiple placeholder="+ Reference" icon="i-heroicons-table-cells" @update:modelValue="onMetaChange" />
                  <!-- Labels -->
                  <KSelect v-if="metaEditable && labelOpts.length" v-model="draft.label_ids" :options="labelOpts" multiple placeholder="+ Label" icon="i-heroicons-tag" @update:modelValue="onMetaChange" />
                  <span v-for="l in (!metaEditable ? (detail.labels || []) : [])" :key="l.id" class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px]">{{ l.name }}</span>
                  <!-- Kind (last) -->
                  <KSelect v-if="metaEditable" v-model="draft.kind" :options="kindOpts" :icon="draft.kind === 'skill' ? 'i-heroicons-sparkles' : 'i-heroicons-document-text'" @update:modelValue="onMetaChange" />
                  <span v-else class="inline-flex items-center px-2 h-7 rounded-md bg-gray-100 text-gray-600 text-[11px] font-medium"><UIcon :name="draft.kind === 'skill' ? 'i-heroicons-sparkles' : 'i-heroicons-document-text'" class="w-3 h-3 mr-1 text-gray-400" />{{ draft.kind === 'skill' ? 'Skill' : 'Instruction' }}</span>
                </div>
                <!-- Source + author/timestamps -->
                <div v-if="detail" class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400">
                  <span class="inline-flex items-center gap-1"><UIcon :name="h.getSourceIcon(detail)" class="w-3 h-3" />{{ h.getSourceTooltip(detail) }}</span>
                  <span v-if="detail.user" class="inline-flex items-center gap-1"><UIcon name="i-heroicons-user-circle" class="w-3 h-3" />{{ detail.user.name || detail.user.email }}</span>
                  <span v-if="detail.created_at">Created {{ fmtDate(detail.created_at) }}</span>
                  <span v-if="detail.updated_at && detail.updated_at !== detail.created_at">· Updated {{ fmtDate(detail.updated_at) }}</span>
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
          </div>
        </template>

        <div v-else class="flex-1 flex items-center justify-center px-6">
          <div class="relative w-full max-w-lg h-72 overflow-hidden">
            <img src="/assets/empty-states/empty-integrations.png" alt="" class="absolute inset-x-0 bottom-8 w-full opacity-80 select-none pointer-events-none" />
            <div class="absolute inset-x-0 bottom-0 flex flex-col items-center justify-center text-center px-6 pb-2">
              <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white/70 backdrop-blur-sm ring-1 ring-gray-200/70 shadow-sm"><UIcon name="i-heroicons-book-open" class="w-5 h-5 text-gray-400" /></div>
              <h3 class="mt-3 text-base font-medium text-gray-900">Configure your agents</h3>
              <p class="mt-1.5 max-w-xs text-sm leading-relaxed text-gray-500">{{ agents.length ? 'Select an agent on the left to explore and edit its data, tools, skills and instructions.' : 'Connect your data to create your first agent.' }}</p>
              <div v-if="canCreateDataSource" class="mt-4 flex items-center gap-2">
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors" @click="showNewAgent = true"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />New agent</button>
                <button class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-gray-200 bg-white/70 text-gray-700 text-xs font-medium hover:bg-gray-50 transition-colors" @click="connTargetAgentId = null; showAddConnection = true"><UIcon name="i-heroicons-circle-stack" class="w-3.5 h-3.5 text-gray-400" />Connect data</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Pane 3: Versions + suggestions ──────────── -->
      <aside v-if="detail && !creating && showHistory" class="w-64 shrink-0 border-l border-gray-200 flex flex-col">
        <div class="flex-1 overflow-y-auto">
          <!-- Suggested changes (pending builds) -->
          <template v-if="pendingBuilds.length">
            <div class="h-9 px-3 flex items-center border-b border-gray-100 bg-amber-50/40">
              <span class="text-[11px] font-semibold uppercase tracking-wider text-amber-600">Suggested changes</span>
            </div>
            <div class="p-2 space-y-1 border-b border-gray-100">
              <div v-for="pb in pendingBuilds" :key="pb.build_id"
                   class="px-2.5 py-2 rounded-md border transition-colors cursor-pointer"
                   :class="diff && diff.buildId === pb.build_id ? 'border-amber-300 bg-amber-50' : 'border-transparent hover:bg-amber-50/60'"
                   @click="viewSuggestion(pb)">
                <div class="flex items-center justify-between">
                  <span class="text-xs font-medium text-gray-700">{{ pb.source === 'ai' ? 'AI suggestion' : 'Proposed' }} · v{{ pb.pending_version_number }}</span>
                  <span class="text-[9px] font-semibold uppercase tracking-wider px-1.5 rounded" :class="pb.status === 'pending_approval' ? 'text-amber-700 bg-amber-100' : 'text-gray-500 bg-gray-100'">{{ pb.status === 'pending_approval' ? 'review' : 'draft' }}</span>
                </div>
                <div class="text-[10px] text-gray-400 mt-0.5 flex items-center gap-1 flex-wrap">
                  <span>{{ pb.created_by?.name || 'system' }} · {{ fmtDate(pb.created_at) }}</span>
                  <NuxtLink v-if="pb.report_id" :to="`/reports/${pb.report_id}`" class="text-blue-500 hover:underline inline-flex items-center gap-0.5" @click.stop>· from report<UIcon name="i-heroicons-arrow-top-right-on-square" class="w-2.5 h-2.5" /></NuxtLink>
                </div>
                <div v-if="pb.build_title || pb.message" class="mt-1">
                  <div v-if="pb.build_title" class="text-[11px] font-medium text-gray-700 truncate">{{ pb.build_title }}</div>
                  <div v-if="pb.message" class="text-[10px] text-gray-500 mt-0.5 line-clamp-2 whitespace-pre-line">{{ pb.message }}</div>
                </div>
                <div v-if="canApprove" class="mt-1.5 flex items-center">
                  <span class="text-[10px] font-medium text-amber-700 inline-flex items-center gap-0.5">Review changes<UIcon name="i-heroicons-arrow-right" class="w-2.5 h-2.5" /></span>
                </div>
              </div>
            </div>
          </template>

          <!-- Version history -->
          <div class="h-9 px-3 flex items-center border-b border-gray-100"><span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400">Version history</span></div>
          <div class="p-2 space-y-0.5">
            <div v-if="versionsLoading" class="p-3 text-center text-[11px] text-gray-400">Loading…</div>
            <div v-else-if="versions.length === 0" class="p-3 text-center text-[11px] text-gray-300">No history yet.</div>
            <div v-for="(v, i) in versions" :key="v.id"
                 class="px-2.5 py-2 rounded-md border transition-colors cursor-pointer"
                 :class="diff && diff.versionId === v.id ? 'border-gray-300 bg-gray-50' : 'border-transparent hover:bg-gray-50'"
                 @click="viewVersion(v, i === 0)">
              <div class="flex items-center justify-between">
                <span class="text-xs font-medium text-gray-700">v{{ v.version_number }}</span>
                <span v-if="i === 0" class="text-[9px] font-semibold uppercase tracking-wider text-gray-500 bg-gray-100 px-1.5 rounded">current</span>
                <button v-else class="text-[10px] text-gray-400 hover:text-gray-700" @click.stop="restore(v)">Restore</button>
              </div>
              <div class="text-[10px] text-gray-400 mt-0.5">{{ fmtDate(v.created_at) }}</div>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <GitRepoModalComponent v-model="showGitModal" @changed="onGitChanged" />

    <!-- Agent trace for a suggestion (opened from the inline review hover card) -->
    <TraceModal v-if="canViewConsole" v-model="showTraceModal" :report-id="traceReportId" :completion-id="traceCompletionId" />

    <!-- All connections (clean list) -->
    <UModal v-model="showConnectionsModal" :ui="{ width: 'sm:max-w-lg' }">
      <div class="p-5">
        <div class="flex items-center justify-between mb-3">
          <div>
            <div class="text-sm font-semibold text-gray-900">Connections</div>
            <div class="text-xs text-gray-500">{{ connections.length }} connected source{{ connections.length === 1 ? '' : 's' }}</div>
          </div>
          <button v-if="canCreateDataSource" type="button" class="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg border border-gray-200 text-gray-700 text-xs font-medium hover:bg-gray-50" @click="showConnectionsModal = false; connTargetAgentId = null; showAddConnection = true"><UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />New</button>
        </div>
        <div class="max-h-[60vh] overflow-auto -mx-1 px-1 space-y-0.5">
          <button v-for="c in connections" :key="c.id" type="button" class="w-full flex items-center gap-3 px-2.5 py-2 rounded-lg hover:bg-gray-50 text-left transition-colors" @click="showConnectionsModal = false; openConnectionDetail(c)">
            <span class="relative inline-flex items-center justify-center w-8 h-8 rounded-md border border-gray-200 bg-white shrink-0">
              <DataSourceIcon :type="c.type" class="w-4 h-4" />
              <span class="absolute -bottom-0.5 -right-0.5 w-2 h-2 rounded-full ring-2 ring-white" :class="c.is_active === false ? 'bg-gray-300' : 'bg-green-500'"></span>
            </span>
            <span class="min-w-0 flex-1">
              <span class="block text-sm font-medium text-gray-800 truncate">{{ c.name }}</span>
              <span class="block text-xs text-gray-400 truncate">{{ c.type }}</span>
            </span>
            <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 text-gray-300 shrink-0" />
          </button>
        </div>
      </div>
    </UModal>

    <ConnectionDetailModal v-model="showConnectionModal" :connection="selectedConnection" @updated="onConnectionChanged" />
    <AddConnectionModal v-model="showAddConnection" @created="onConnCreated" />
    <NewAgentWizardModal v-model="showNewAgent" @finished="onNewAgentFinished" />
    <AddMCPModal v-model="showAddMCP" :existing-connections="mcpExistingConnections" @created="onConnCreated" />
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="credsAgent" @saved="onCredsSaved" />
    <input ref="fileInputRef" type="file" multiple class="hidden" @change="onUploadInput" />

    <UModal v-model="showEditStarters" :ui="{ width: 'sm:max-w-2xl' }">
      <div class="p-5">
        <div class="text-sm font-medium text-gray-900">Edit conversation starters</div>
        <div class="text-xs text-gray-500 mt-1">Short prompts users can click to start a conversation with this agent.</div>
        <div class="mt-4 space-y-2 max-h-[60vh] overflow-auto pe-1">
          <div v-for="(item, idx) in editStarters" :key="idx" class="rounded-md border border-gray-100 p-2">
            <div class="flex items-center justify-between mb-1">
              <span class="text-[10px] uppercase tracking-wide text-gray-400">Starter {{ idx + 1 }}</span>
              <button class="text-[11px] text-gray-500 hover:text-red-600" @click="removeStarter(idx)">Remove</button>
            </div>
            <div class="space-y-1">
              <input v-model="item.title" type="text" placeholder="Title" class="w-full h-8 text-sm border border-gray-200 rounded-md px-2 focus:outline-none focus:ring-2 focus:ring-blue-200" />
              <textarea v-model="item.prompt" rows="2" placeholder="Prompt" class="w-full text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200"></textarea>
            </div>
          </div>
          <button class="text-xs border border-gray-300 text-gray-700 rounded-lg px-2 py-1 hover:bg-gray-50" @click="addStarter">Add starter</button>
        </div>
        <div class="flex justify-end gap-2 mt-4">
          <button class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg" @click="showEditStarters = false">Cancel</button>
          <button class="px-3 py-1.5 text-xs bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50" :disabled="savingStarters" @click="saveStarters">{{ savingStarters ? 'Saving…' : 'Save' }}</button>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { h as createElement } from 'vue'
import InstructionEditor from '~/components/instructions/InstructionEditor.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import PrimaryInstructionPicker from '~/components/instructions/PrimaryInstructionPicker.vue'
import AgentEvalsPanel from '~/components/AgentEvalsPanel.vue'
import AgentSettingsPanel from '~/components/AgentSettingsPanel.vue'
import PublishStatusControl from '~/components/datasources/PublishStatusControl.vue'
import InstructionAnalysisPanel from '~/components/InstructionAnalysisPanel.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import KSelect from '~/components/KSelect.vue'
import GitConnectionButton from '~/components/instructions/GitConnectionButton.vue'
import GitRepoModalComponent from '~/components/GitRepoModalComponent.vue'
import ConnectionDetailModal from '~/components/ConnectionDetailModal.vue'
import AddConnectionModal from '~/components/AddConnectionModal.vue'
import NewAgentWizardModal from '~/components/NewAgentWizardModal.vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'
import ToolsSelector from '~/components/datasources/ToolsSelector.vue'
import AddMCPModal from '~/components/AddMCPModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import TrackedChangesView from '~/components/instructions/TrackedChangesView.vue'
import TraceModal from '~/components/console/TraceModal.vue'
import DiffMatchPatch from 'diff-match-patch'
import { useCan, useCanAny } from '~/composables/usePermissions'
import { useConnectionSignIn } from '~/composables/useConnectionSignIn'
import { useInstructionHelpers, type Instruction } from '~/composables/useInstructionHelpers'

const h = useInstructionHelpers()
const toast = useToast()

// ── State ───────────────────────────────────────────────
const allInstructions = ref<Instruction[]>([])
const agents = ref<any[]>([])
// Admin-only "show all" toggle: include every agent in the org, not just the
// caller's memberships. Re-fetches the agent list when flipped.
const showAllAgents = ref(false)
watch(showAllAgents, () => { fetchAgents() })
const labels = ref<{ id: string; name: string }[]>([])
const categories = ref<string[]>([])
const search = ref('')

const fStatus = ref<string[]>([]); const fLoad = ref<string[]>([]); const fSource = ref<string[]>([]); const fCategory = ref<string[]>([])

const expanded = ref<Set<string>>(new Set())
const agentTables = ref<Record<string, { id: string; name: string; is_active: boolean }[]>>({})
const agentTools = ref<Record<string, any[]>>({})
const agentFiles = ref<Record<string, any[]>>({})
const agentLoaded = ref<Set<string>>(new Set())

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
const draft = reactive<{ title: string; text: string; kind: string; load_mode: string; status: string; category: string; data_source_ids: string[]; label_ids: string[]; references: any[] }>(
  { title: '', text: '', kind: 'instruction', load_mode: 'always', status: 'published', category: 'general', data_source_ids: [], label_ids: [], references: [] }
)
const kindOpts = [{ value: 'instruction', label: 'Instruction' }, { value: 'skill', label: 'Skill' }]
// Reference options come from the selected agents' tables (valid datasource_table ids).
const refOptions = computed(() => {
  const opts: { value: string; label: string; type?: string }[] = []
  for (const aid of draft.data_source_ids) {
    const a = agents.value.find(x => x.id === aid)
    for (const t of (agentTables.value[aid] || [])) opts.push({ value: t.id, label: t.name, type: a?.type })
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
      return { object_type: 'datasource_table', object_id: id, relation_type: 'scope', display_text: opt?.label || id }
    })
  },
})
const removeRef = (i: number) => { draft.references.splice(i, 1) }

const showHistory = ref(true)
const versions = ref<any[]>([])
const versionsLoading = ref(false)

// git
const gitRepos = ref<{ provider: string; repoName: string }[]>([])
const gitLastIndexed = ref<string | null>(null)
const showGitModal = ref(false)

const statusOpts = [{ value: 'published', label: 'Active' }, { value: 'draft', label: 'Inactive' }, { value: 'pending_review', label: 'Pending review' }]
const statusEditOpts = [{ value: 'published', label: 'Active' }, { value: 'draft', label: 'Inactive' }]
const loadOpts = [{ value: 'always', label: 'Always' }, { value: 'intelligent', label: 'Smart' }, { value: 'disabled', label: 'Off' }]
const sourceOpts = [{ value: 'user', label: 'User' }, { value: 'ai', label: 'AI' }, { value: 'git', label: 'Git' }]
const categoryOpts = computed(() => categories.value.filter(c => c !== 'dashboard').map(c => ({ value: c, label: h.formatCategory(c) })))
const agentOpts = computed(() => agents.value.map(a => ({ value: a.id, label: a.name, type: a.type })))

// right-pane panel for Tables/Tools/Evals/Settings
const panelView = ref<null | { kind: 'tables' | 'tools' | 'evals' | 'settings'; agentId: string }>(null)
const closePanel = () => { panelView.value = null }
const panelKindLabel = computed(() => ({ tables: 'Tables', tools: 'Tools', evals: 'Evals', settings: 'Settings' } as Record<string, string>)[panelView.value?.kind || ''] || '')
const panelAgent = computed(() => panelView.value ? agents.value.find(a => a.id === panelView.value!.agentId) : null)
const panelConnections = computed(() => {
  const a = panelAgent.value as any
  return (a?.connections || []).filter((c: any) => c.type === 'mcp' || c.type === 'custom_api')
})
const openPanel = (kind: 'tables' | 'tools' | 'evals' | 'settings', agentId: string) => {
  clearRightPane()
  loadAgentMeta(agentId)
  panelView.value = { kind, agentId }
}
const onAgentSettingsUpdated = async () => { await fetchAgents(); if (agentView.value) refreshAgentDetail() }
const onAgentDeleted = async () => { closePanel(); await fetchAgents() }
// Row-click on Tables/Tools opens the editable panel immediately (like clicking
// an agent). Re-clicking the already-open row just collapses the tree node.
const onPanelRowClick = (kind: 'tables' | 'tools', agentId: string) => {
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
const closeAgentView = () => { agentView.value = null; agentDetail.value = null; agentDetailLoading.value = false; editingDesc.value = false; creatingPrimary.value = false; editingPrimary.value = false }
const refreshAgentDetail = async () => {
  const id = agentView.value?.agentId; if (!id) return
  try { const { data } = await useMyFetch<any>(`/data_sources/${id}`, { method: 'GET' }); if (agentView.value?.agentId === id) agentDetail.value = data.value } catch {} finally { if (agentView.value?.agentId === id) agentDetailLoading.value = false }
}
const fetchAgentReports = async (id: string) => {
  agentReportCount.value = 0
  try { const { data } = await useMyFetch<any>('/reports', { method: 'GET', query: { data_source_id: id, limit: 1, filter: 'published' } }); agentReportCount.value = (data.value as any)?.total ?? 0 } catch {}
}
const onAgentPublishUpdated = (val: string) => {
  if (agentDetail.value) agentDetail.value.publish_status = val
  const a = agents.value.find(x => x.id === agentView.value?.agentId); if (a) { a.publish_status = val; agents.value = [...agents.value] }
}
const setAgentPublic = async (val: boolean) => {
  const id = agentView.value?.agentId; if (!id) return
  try {
    await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { is_public: val } })
    if (agentDetail.value) agentDetail.value.is_public = val
    const a = agents.value.find(x => x.id === id); if (a) { a.is_public = val; agents.value = [...agents.value] }
    toast.add({ title: val ? 'Made public' : 'Made private', color: 'green' })
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}
const openAgent = async (id: string) => {
  clearRightPane()
  agentView.value = { agentId: id }; agentDetail.value = null; agentDetailLoading.value = true
  creatingPrimary.value = false; editingPrimary.value = false; editingDesc.value = false
  // Reflect the agent in the URL (/agents/<id>) WITHOUT a router navigation —
  // router.replace re-runs the global middleware (auth/onboarding/permissions)
  // and flickers the page. history.replaceState just updates the address bar.
  if (process.client && !location.pathname.replace(/\/$/, '').endsWith('/agents/' + id)) {
    try { history.replaceState({ ...history.state }, '', `/agents/${id}`) } catch {}
  }
  loadAgentMeta(id); fetchAgentReports(id); refreshAgentDetail(); fetchActivity(id)
}
// Close button: clear the view and drop the agent id from the URL.
const exitAgentView = () => {
  closeAgentView()
  if (process.client && /\/agents\/.+/.test(location.pathname)) { try { history.replaceState({ ...history.state }, '', '/agents') } catch {} }
}
const onAgentClick = (agent: any) => {
  if (needsSignIn(agent)) { openAgentTab(agent.id); return }
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
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
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
  try { await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { description: v } }); if (agentDetail.value) agentDetail.value.description = v; toast.add({ title: 'Saved', color: 'green' }) } catch { toast.add({ title: 'Failed to save description', color: 'red' }) }
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
    toast?.add?.({ title: 'Saved', description: 'Primary instruction updated.' })
  } catch (e: any) { toast?.add?.({ title: 'Error', description: String(e?.message || e), color: 'red' }) }
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
    toast.add({ title: 'Saved', color: 'green' })
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) } finally { primarySaving.value = false }
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
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) } finally { startingReport.value = false; startingStarterIdx.value = null }
}
const openEditStarters = () => {
  const arr = agentDetail.value?.conversation_starters || []
  editStarters.value = arr.map((s: any) => typeof s === 'string'
    ? { title: (s.split('\n')[0] || '').trim(), prompt: s.split('\n').slice(1).join('\n').trim() }
    : { title: s.title || '', prompt: s.prompt || '' })
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
  try { await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: { conversation_starters } }); await refreshAgentDetail(); showEditStarters.value = false; toast.add({ title: 'Saved', color: 'green' }) }
  catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) } finally { savingStarters.value = false }
}
// reload tables / tools from the tree
const tablesRefreshKey = ref(0)
const reloadTables = async (id: string) => {
  try { await useMyFetch(`/data_sources/${id}/refresh_schema`, { method: 'GET' }) } catch {}
  agentLoaded.value.delete(id); await loadAgentMeta(id)
  tablesRefreshKey.value++  // force the open TablesSelector panel to re-fetch
  toast.add({ title: 'Tables reloaded', color: 'green' })
}
const reloadTools = async (id: string) => {
  for (const c of (agents.value.find(a => a.id === id)?.connections || [])) { try { await useMyFetch(`/connections/${c.id}/refresh-tools`, { method: 'POST' }) } catch {} }
  agentLoaded.value.delete(id); await loadAgentMeta(id); toast.add({ title: 'Tools reloaded', color: 'green' })
}

// ── File upload (per agent) ─────────────────────────────
const uploadingAgent = ref<string | null>(null)
const uploadTargetAgent = ref<string | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const triggerUpload = (agentId: string) => { uploadTargetAgent.value = agentId; nextTick(() => fileInputRef.value?.click()) }
const onUploadInput = async (e: Event) => {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files || [])
  const agentId = uploadTargetAgent.value
  if (!files.length || !agentId) return
  uploadingAgent.value = agentId
  try {
    for (const file of files) {
      const fd = new FormData(); fd.append('file', file)
      await useMyFetch(`/data_sources/${agentId}/files`, { method: 'POST', body: fd })
    }
    toast.add({ title: `Uploaded ${files.length} file(s)`, color: 'green' })
    agentLoaded.value.delete(agentId)
    await loadAgentMeta(agentId)
    if (!isOpen('files:' + agentId)) expand('files:' + agentId)
  } catch (err: any) { toast.add({ title: 'Upload failed', description: err?.message, color: 'red' }) }
  finally { uploadingAgent.value = null; if (input) input.value = '' }
}

// Clear every right-pane mode (preview / diff / tables-tools panel / agent view / detail)
const clearRightPane = () => {
  closePreview(); closeDiff(); closePanel(); closeAgentView()
  detail.value = null; selectedId.value = null; creating.value = false; editing.value = false
  versions.value = []; pendingBuilds.value = []
}

// tree pane resize
const treeWidth = ref(300)
const clampTreeWidth = (w: number) => Math.min(600, Math.max(220, w))
const startTreeResize = (e: MouseEvent) => {
  e.preventDefault()
  const startX = e.clientX
  const startWidth = treeWidth.value
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'col-resize'
  const onMove = (ev: MouseEvent) => { treeWidth.value = clampTreeWidth(startWidth + (ev.clientX - startX)) }
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
const diff = ref<null | { title: string; label: string; original: string; modified: string; buildId?: string | null; versionId?: string | null }>(null)
const activeSuggestion = ref<any | null>(null)
const resolving = ref<number | 'all' | 'reject-all' | null>(null)
const approving = ref<string | null>(null)
const discarding = ref<string | null>(null)
// connection modals
const showConnectionModal = ref(false)
const showConnectionsModal = ref(false)
const selectedConnection = ref<any>(null)
const showAddConnection = ref(false)
const showNewAgent = ref(false)
const showAddMCP = ref(false)

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
    if (result.error) toast.add({ title: 'Could not start sign-in', description: result.error, color: 'red' })
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
const openAddCustomApi = (agentId: string) => { connTargetAgentId.value = agentId; showAddConnection.value = true }
const mcpExistingConnections = computed(() => connections.value.filter((c: any) => c.type === 'mcp'))
// New connection created: link it to the target agent (if any) and refresh its tools.
const onConnCreated = async (conn?: any) => {
  const aid = connTargetAgentId.value
  if (aid && conn?.id) {
    try { await useMyFetch(`/data_sources/${aid}/connections/${conn.id}`, { method: 'POST' }) } catch {}
    try { await useMyFetch(`/connections/${conn.id}/refresh-tools`, { method: 'POST' }) } catch {}
  }
  showAddMCP.value = false; showAddConnection.value = false
  if (aid) { agentLoaded.value.delete(aid); await loadAgentMeta(aid); if (agentView.value?.agentId === aid) await refreshAgentDetail() }
  await fetchAgents(); toolsRefreshKey.value++
  connTargetAgentId.value = null
}
// Connection deleted from the Tools panel: just refresh the agent's tools.
const onToolsConnectionChanged = async () => {
  showAddMCP.value = false
  const aid = panelView.value?.agentId
  if (aid) { agentLoaded.value.delete(aid); await loadAgentMeta(aid) }
  await fetchAgents(); toolsRefreshKey.value++
}
// perms
const canApprove = computed(() => useCanAny('manage_instructions', 'data_source'))
const canCreateDataSource = computed(() => useCan('create_data_source'))
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
// Editing tables/tools requires manage on the data source (org-wide or on this resource).
const canManageAgent = (id?: string) => id ? (useCan('update_data_source') || useCan('update_data_source', { type: 'data_source', id })) : false
const panelCanUpdate = computed(() => canManageAgent(panelView.value?.agentId))

const openConnectionDetail = (c: any) => { selectedConnection.value = c; showConnectionModal.value = true }
const onConnectionChanged = async () => { await fetchAgents() }
const loadPending = async (id: string) => {
  pendingBuilds.value = []
  try { const { data } = await useMyFetch<any[]>(`/api/instructions/${id}/pending-builds`, { method: 'GET' }); pendingBuilds.value = data.value || [] } catch {}
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
  } catch (e: any) {
    toast.add({ title: 'Couldn’t apply change', description: e?.message, color: 'red' })
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
  const dmp = new (DiffMatchPatch as any)()
  const ops = dmp.diff_main(base, next)
  dmp.diff_cleanupSemantic(ops)
  return ops.map((o: [number, string]) => ({ type: o[0], text: o[1] }))
})
const viewVersion = async (v: any, isCurrent: boolean) => {
  if (isCurrent || !detail.value) { closeDiff(); return }
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${detail.value.id}/versions/${v.id}`, { method: 'GET' })
    diff.value = { title: `Version v${v.version_number}`, label: `v${v.version_number}`, original: detail.value?.text || '', modified: data.value?.text || '', versionId: v.id, buildId: null }
  } catch {}
}
const viewSuggestion = (pb: any) => {
  activeSuggestion.value = pb
  diff.value = { title: pb.source === 'ai' ? 'AI suggestion' : 'Proposed change', label: `v${pb.pending_version_number}`, original: detail.value?.text || '', modified: pb.pending_text || '', buildId: pb.build_id, versionId: null }
  // Reset any prior run view and lazily load eval suites for the run strip.
  evalActiveRun.value = null; evalResults.value = []; stopEvalPoll()
  fetchEvalSuites()
}
const approveSuggestion = async (pb: any) => {
  if (!pb?.build_id) return
  approving.value = pb.build_id
  try {
    await useMyFetch(`/api/builds/${pb.build_id}/publish`, { method: 'POST' })
    toast.add({ title: 'Approved & published', color: 'green' })
    closeDiff()
    await refreshLists()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) openInstruction(fresh)
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) } finally { approving.value = null }
}
const discardSuggestion = async (pb: any) => {
  if (!pb?.build_id || discarding.value) return
  if (!window.confirm('Discard this suggested change? It will be rejected and removed from the review queue.')) return
  discarding.value = pb.build_id
  try {
    const { error } = await useMyFetch(`/api/builds/${pb.build_id}/reject`, { method: 'POST', body: { reason: 'Discarded from the Agents review queue' } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Reject failed')
    toast.add({ title: 'Suggestion discarded', color: 'gray' })
    if (diff.value?.buildId === pb.build_id) closeDiff()
    await refreshLists()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh) openInstruction(fresh)
  } catch (e: any) { toast.add({ title: 'Couldn’t discard', description: e?.message, color: 'red' }) } finally { discarding.value = null }
}

// ── Suggestion evals: run a test suite against the candidate (pending) build,
//    showing live progress like BuildExplorerModal. ───────────────────────────
const canManageTests = computed(() => useCan('manage_tests'))
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
const evalPrettyStatus = (s?: string) => s === 'in_progress' ? 'Running' : s === 'success' ? 'Passed' : (s === 'fail' || s === 'error') ? 'Failed' : (s || '—')

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
      toast.add({ title: 'Eval started', color: 'blue' })
    }
  } catch (e: any) { toast.add({ title: 'Failed to start eval', description: e?.message, color: 'red' }) } finally { evalRunning.value = false }
}
onUnmounted(() => stopEvalPoll())

const labelOpts = computed(() => labels.value.map(l => ({ value: l.id, label: l.name })))
const activeFilterCount = computed(() => fStatus.value.length + fLoad.value.length + fSource.value.length + fCategory.value.length)
const clearFilters = () => { fStatus.value = []; fLoad.value = []; fSource.value = []; fCategory.value = [] }

// connections (deduped from agents)
const connections = computed(() => { const m = new Map<string, any>(); for (const a of agents.value) for (const c of (a.connections || [])) if (!m.has(c.id)) m.set(c.id, c); return Array.from(m.values()) })

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
  expanded.value = new Set(expanded.value)
}

// ── Fetching ────────────────────────────────────────────
const fetchAll = async () => {
  try { const { data } = await useMyFetch<any>('/api/instructions', { method: 'GET', query: { skip: 0, limit: 200, include_own: true, include_drafts: true, include_archived: true } }); allInstructions.value = data.value?.items || [] } catch (e) { console.error(e) }
}
// Refresh BOTH the instruction list and the pending-builds map so the left tree
// (Pending review count, top "N pending" badge, per-row amber dots) stays in
// sync after a mutation (approve / discard / save). fetchAll alone only updates
// instruction statuses; pendingInstrIds is what drives the pending signals.
const refreshLists = async () => { await Promise.all([fetchAll(), fetchPendingMap()]) }
const fetchAgents = async () => {
  try {
    // include_unconnected=true so members also see user_required (OBO) agents
    // they haven't connected yet — otherwise they can never reach the Connect
    // flow (parity with the legacy /agents page). show_all is an admin-only
    // toggle that surfaces every agent in the org (admin_only entries flagged).
    const query: Record<string, any> = { include_unconnected: true }
    if (showAllAgents.value) query.show_all = true
    const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET', query })
    agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name, type: d.type, connections: d.connections || [], user_status: d.user_status, is_public: d.is_public, status: d.status, description: d.description, conversation_starters: d.conversation_starters || [], auth_policy: d.auth_policy, admin_only: d.admin_only }))
  } catch (e) { console.error(e) }
}
const agentStatusDot = (a: any) => a?.status === 'active' ? 'bg-green-400' : 'bg-gray-300'
// Group an agent's tools by their connection (MCP server / custom API), resolving
// the connection name + type from the agent's connections for the tree headers.
const toolGroups = (agentId: string) => {
  const tools = agentTools.value[agentId] || []
  const a = agents.value.find(x => x.id === agentId)
  const connMap: Record<string, any> = {}
  for (const c of (a?.connections || [])) connMap[c.id] = c
  const groups: Record<string, { connId: string; name: string; type?: string; tools: any[] }> = {}
  for (const t of tools) {
    const cid = String(t.connection_id ?? t.connection?.id ?? 'tools')
    if (!groups[cid]) groups[cid] = { connId: cid, name: connMap[cid]?.name || t.connection_name || 'Tools', type: connMap[cid]?.type || t.connection_type, tools: [] }
    groups[cid].tools.push(t)
  }
  return Object.values(groups)
}
const fetchLabels = async () => { try { const { data } = await useMyFetch<any[]>('/instructions/labels', { method: 'GET' }); labels.value = data.value || [] } catch {} }
const fetchCategories = async () => { try { const { data } = await useMyFetch<string[]>('/instructions/categories', { method: 'GET' }); categories.value = data.value || [] } catch {} }
const fetchGitStatus = async () => {
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
  try { const { data } = await useMyFetch<any>(`/data_sources/${id}/full_schema`, { method: 'GET' }); const items = Array.isArray(data.value) ? data.value : (data.value?.items || []); agentTables.value[id] = items.map((t: any) => ({ id: String(t.id ?? t.name), name: t.name, is_active: t.is_active !== false })) } catch { agentTables.value[id] = [] }
  try { const { data } = await useMyFetch<any[]>(`/data_sources/${id}/tools`, { method: 'GET' }); agentTools.value[id] = data.value || [] } catch { agentTools.value[id] = [] }
  try { const { data } = await useMyFetch<any[]>(`/data_sources/${id}/files`, { method: 'GET' }); agentFiles.value[id] = data.value || [] } catch { agentFiles.value[id] = [] }
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
    toast.add({ title: 'File deleted', color: 'green' })
  } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
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
// Authoritative: an instruction is "pending" iff it has a real pending change
// (from /pending-changes). Avoids the old over-count from inherited/stale rows.
const isPending = (ins: Instruction) => pendingInstrIds.value.has(ins.id)
const pendingCount = computed(() => allInstructions.value.filter(isPending).length)
const globalCount = computed(() => allInstructions.value.filter(i => (i.data_sources || []).length === 0).length)
const skillCount = computed(() => allInstructions.value.filter(i => (i as any).kind === 'skill').length)
const agentCount = (id: string) => allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id)).length
const agentPending = (id: string) => allInstructions.value.some(i => isPending(i) && (i.data_sources || []).some(d => d.id === id))

// ── Leaf lists ──────────────────────────────────────────
const applyFilters = (list: Instruction[]) => {
  let out = list
  if (fStatus.value.length) out = out.filter(i => fStatus.value.includes(isPending(i) ? 'pending_review' : i.status))
  if (fLoad.value.length) out = out.filter(i => fLoad.value.includes(i.load_mode || 'always'))
  if (fSource.value.length) out = out.filter(i => fSource.value.includes(h.getSourceType(i)))
  if (fCategory.value.length) out = out.filter(i => fCategory.value.includes(i.category))
  const q = search.value.trim().toLowerCase()
  if (q) out = out.filter(i => (i.title || '').toLowerCase().includes(q) || (i.text || '').toLowerCase().includes(q))
  return out
}
const listFor = (kind: string) => {
  let base = allInstructions.value
  if (kind === 'skills') base = base.filter(i => (i as any).kind === 'skill')
  else if (kind === 'pending') base = base.filter(isPending)
  else if (kind === 'global') base = base.filter(i => (i.data_sources || []).length === 0)
  return applyFilters(base)
}
const hasTableRef = (ins: Instruction) => (ins.references || []).some((r: any) => r.object_type === 'datasource_table')
const listForAgent = (id: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === id) && !hasTableRef(i)))
const listForTable = (agentId: string, tableId: string) => applyFilters(allInstructions.value.filter(i => (i.data_sources || []).some(d => d.id === agentId) && (i.references || []).some((r: any) => r.object_type === 'datasource_table' && String(r.object_id) === tableId)))

// ── Detail / create ─────────────────────────────────────
const openInstruction = async (ins: Instruction) => {
  closePreview(); closeDiff(); closePanel(); closeAgentView(); creating.value = false; bottomTab.value = 'details'
  selectedId.value = ins.id; detail.value = ins; editing.value = false
  syncDraft(ins); loadVersions(ins.id)
  try {
    const { data } = await useMyFetch<Instruction>(`/api/instructions/${ins.id}`, { method: 'GET' })
    if (data.value && selectedId.value === ins.id) {
      detail.value = data.value; if (!editing.value) syncDraft(data.value)
      // keep the tree leaf consistent with the hydrated build/status
      const idx = allInstructions.value.findIndex(i => i.id === ins.id)
      if (idx >= 0) { allInstructions.value[idx] = { ...allInstructions.value[idx], status: data.value.status, current_build_id: data.value.current_build_id, current_build_status: data.value.current_build_status }; allInstructions.value = [...allInstructions.value] }
    }
  } catch (e) {}
  // Surface pending changes immediately: load them against the now-current text
  // and auto-open the review so the diff isn't hidden behind a link.
  await loadPending(ins.id)
  if (selectedId.value === ins.id && !editing.value && !diff.value && pendingBuilds.value.length) {
    viewSuggestion(pendingBuilds.value[0])
  }
}
const syncDraft = (ins: Instruction) => {
  draft.title = ins.title || ''; draft.text = ins.text || ''
  draft.kind = (ins as any).kind || 'instruction'
  draft.load_mode = ins.load_mode || 'always'; draft.status = ins.status || 'published'
  draft.category = ins.category || 'general'
  draft.data_source_ids = (ins.data_sources || []).map(d => d.id)
  draft.label_ids = (ins.labels || []).map((l: any) => l.id)
  draft.references = (ins.references || []).map((r: any) => ({ object_type: r.object_type, object_id: String(r.object_id), relation_type: r.relation_type || 'scope', display_text: r.display_text || r.object?.name || String(r.object_id), column_name: r.column_name || null }))
  draft.data_source_ids.forEach(id => loadAgentMeta(id))
}
const openCreate = (scope?: { agentId?: string; tableId?: string; tableName?: string }) => {
  closePreview(); closeDiff(); closePanel(); closeAgentView(); pendingBuilds.value = []; detail.value = null; selectedId.value = null; versions.value = []
  creating.value = true; editing.value = true
  draft.title = ''; draft.text = ''; draft.kind = 'instruction'; draft.load_mode = 'always'; draft.status = 'published'; draft.category = 'general'
  draft.data_source_ids = scope?.agentId ? [scope.agentId] : []
  draft.label_ids = []
  draft.references = scope?.tableId ? [{ object_type: 'datasource_table', object_id: scope.tableId, relation_type: 'scope', display_text: scope.tableName }] : []
  draft.data_source_ids.forEach(id => loadAgentMeta(id))
}
const startEdit = () => { if (detail.value) { syncDraft(detail.value); editing.value = true } }
const cancelEdit = () => { if (creating.value) { creating.value = false; editing.value = false; draft.references = [] } else { if (detail.value) syncDraft(detail.value); editing.value = false } }
const save = async () => {
  saving.value = true
  try {
    const body: any = { title: draft.title || null, text: draft.text, kind: draft.kind, load_mode: draft.load_mode, status: draft.status, category: draft.category, data_source_ids: draft.data_source_ids, label_ids: draft.label_ids, references: draft.references }
    if (creating.value) {
      const endpoint = draft.data_source_ids.length ? '/api/instructions' : '/api/instructions/global'
      const { data, error } = await useMyFetch<Instruction>(endpoint, { method: 'POST', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Create failed')
      toast.add({ title: 'Created', color: 'green' })
      creating.value = false; editing.value = false; draft.references = []
      await refreshLists()
      const created = (data.value as any)?.id ? allInstructions.value.find(i => i.id === (data.value as any).id) : null
      if (created) openInstruction(created)
    } else if (detail.value) {
      const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
      if (error.value) throw new Error((error.value as any)?.message || 'Save failed')
      toast.add({ title: 'Saved', color: 'green' }); editing.value = false
      if (data.value) detail.value = { ...detail.value, ...data.value }
      await refreshLists()
      const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
      if (fresh) { detail.value = fresh; syncDraft(fresh) }
      loadVersions(detail.value!.id)
    }
  } catch (e: any) { toast.add({ title: 'Error', description: e.message, color: 'red' }) } finally { saving.value = false }
}

// ── Detail tabs (Instruction / Analyze) ─────────────────
const bottomTab = ref<'details' | 'analyze'>('details')
// Admins edit the bottom metadata inline (autosave); others see read-only chips.
const canEditInstr = computed(() => useCan('manage_instructions'))
// Editable controls also show while creating (the new instruction is authored here).
const metaEditable = computed(() => canEditInstr.value || creating.value)
const savingMeta = ref(false)
let metaTimer: any = null
const saveMeta = async () => {
  if (!detail.value || creating.value || editing.value) return
  savingMeta.value = true
  try {
    const body: any = { title: draft.title || null, text: draft.text, kind: draft.kind, load_mode: draft.load_mode, status: draft.status, category: draft.category, data_source_ids: draft.data_source_ids, label_ids: draft.label_ids, references: draft.references }
    const { data, error } = await useMyFetch<Instruction>(`/api/instructions/${detail.value.id}`, { method: 'PUT', body })
    // useMyFetch doesn't throw on HTTP errors — surface them so the change isn't silently dropped.
    if (error.value) throw new Error((error.value as any)?.data?.detail || (error.value as any)?.message || 'Save failed')
    if (data.value) detail.value = { ...detail.value, ...(data.value as any) }
    await refreshLists()
    const fresh = allInstructions.value.find(i => i.id === detail.value?.id)
    if (fresh && !editing.value) { detail.value = fresh; syncDraft(fresh) }
    toast.add({ title: 'Saved', color: 'green' })
  } catch (e: any) { toast.add({ title: 'Couldn’t save', description: e?.message, color: 'red' }) } finally { savingMeta.value = false }
}
// Fire after a metadata control changes (user-initiated only — not on load/edit).
const onMetaChange = () => { if (editing.value || creating.value) return; clearTimeout(metaTimer); metaTimer = setTimeout(saveMeta, 400) }

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
  versionsLoading.value = true; versions.value = []
  try { const { data } = await useMyFetch<any>(`/api/instructions/${id}/versions`, { method: 'GET', query: { limit: 50 } }); versions.value = data.value?.items || [] } catch (e) {} finally { versionsLoading.value = false }
}
const restore = async (v: any) => {
  if (!detail.value) return
  if (!window.confirm(`Restore version v${v.version_number}? This creates a new version.`)) return
  try { await useMyFetch(`/api/instructions/${detail.value.id}/versions/${v.id}/revert`, { method: 'POST' }); toast.add({ title: `Restored v${v.version_number}`, color: 'green' }); await refreshLists(); const fresh = allInstructions.value.find(i => i.id === detail.value?.id); if (fresh) openInstruction(fresh) } catch (e: any) { toast.add({ title: 'Error', description: e?.message, color: 'red' }) }
}

// ── Display helpers ─────────────────────────────────────
const displayTitle = (ins: Instruction) => ins?.title || (ins?.text || '').split('\n')[0].slice(0, 60) || 'Untitled'
const refLabel = (ref: any) => ref.display_text || ref.object?.name || ref.object_type
const fmtDate = (s?: string) => { if (!s) return ''; try { return new Date(s).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }

// ── Inline tree sub-components ──────────────────────────
const TreeGroup = defineComponent({
  props: { label: String, icon: String, count: { type: Number, default: undefined }, countAccent: Boolean, pending: Boolean, open: Boolean, mono: Boolean, indent: { type: Number, default: 0 }, addable: Boolean, gearable: Boolean, reloadable: Boolean, badge: String, disabled: Boolean, labelClickable: Boolean, active: Boolean, statusDot: String, lock: Boolean },
  emits: ['toggle', 'add', 'gear', 'reload', 'badge', 'label'],
  setup(props, { slots, emit }) {
    // When `labelClickable` is set, the chevron/icon area toggles the tree and the
    // label text opens the panel (`@label`); otherwise the whole row toggles.
    return () => createElement('div', {}, [
      createElement('div', {
        class: ['group w-full flex items-center gap-1.5 h-8 rounded-md text-[13px] transition-colors min-w-0', props.active ? 'bg-gray-100 text-gray-900' : 'text-gray-600', props.disabled ? 'opacity-90' : 'hover:bg-gray-100 cursor-pointer'],
        style: { paddingLeft: (6 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => { if (!props.disabled && !props.labelClickable) emit('toggle') },
      }, [
        createElement(resolveComponent('UIcon'), { name: 'i-heroicons-chevron-right', class: ['w-3 h-3 transition-transform shrink-0', props.disabled ? 'text-gray-200' : 'text-gray-300', props.open ? 'rotate-90' : '', props.labelClickable ? 'cursor-pointer hover:text-gray-500' : ''], onClick: props.labelClickable ? (e: Event) => { e.stopPropagation(); if (!props.disabled) emit('toggle') } : undefined }),
        props.statusDot ? createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', props.statusDot], title: 'Status' }) : null,
        slots.icon ? slots.icon() : (props.icon ? createElement(resolveComponent('UIcon'), { name: props.icon, class: 'w-4 h-4 text-gray-400 shrink-0' }) : null),
        createElement('span', { class: ['flex-1 text-left truncate', props.mono ? 'font-mono text-xs' : ''], onClick: props.labelClickable ? (e: Event) => { e.stopPropagation(); if (!props.disabled) emit('label') } : undefined }, props.label),
        props.lock ? createElement(resolveComponent('UIcon'), { name: 'i-heroicons-lock-closed', class: 'w-3 h-3 text-gray-400 shrink-0', title: 'Private' }) : null,
        props.badge ? createElement('button', { class: 'shrink-0 inline-flex items-center gap-0.5 px-1.5 h-5 rounded bg-blue-50 text-blue-600 text-[10px] font-medium hover:bg-blue-100', onClick: (e: Event) => { e.stopPropagation(); emit('badge') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-key', class: 'w-2.5 h-2.5' }), props.badge]) : null,
        (props.reloadable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 text-gray-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: 'Reload', onClick: (e: Event) => { e.stopPropagation(); emit('reload') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-arrow-path', class: 'w-3 h-3' })]) : null,
        (props.gearable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 text-gray-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: 'Manage', onClick: (e: Event) => { e.stopPropagation(); emit('gear') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-cog-6-tooth', class: 'w-3 h-3' })]) : null,
        (props.addable && !props.disabled) ? createElement('button', { class: 'shrink-0 w-4 h-4 rounded hover:bg-gray-200 text-gray-400 opacity-0 group-hover:opacity-100 flex items-center justify-center', title: 'Add', onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, [createElement(resolveComponent('UIcon'), { name: 'i-heroicons-plus', class: 'w-3 h-3' })]) : null,
        (props.count !== undefined && !props.badge) ? createElement('span', { class: ['text-xs tabular-nums shrink-0', props.countAccent ? 'text-amber-600 font-medium' : 'text-gray-400'] }, String(props.count)) : null,
      ]),
      (props.open && !props.disabled) ? createElement('div', { class: 'space-y-0.5 mt-0.5' }, slots.default ? slots.default() : []) : null,
    ])
  },
})

const InstrLeaf = defineComponent({
  props: { ins: { type: Object as () => Instruction, required: true }, indent: { type: Number, default: 0 } },
  setup(props) {
    return () => {
      const ins = props.ins
      const sel = selectedId.value === ins.id
      return createElement('button', {
        class: ['group w-full flex items-center gap-2 h-8 rounded-md text-[13px] transition-colors min-w-0', sel ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-100'],
        style: { paddingLeft: (20 + props.indent * 14) + 'px', paddingRight: '8px' },
        onClick: () => openInstruction(ins),
      }, [
        createElement('span', { class: ['shrink-0 w-1.5 h-1.5 rounded-full', pendingInstrIds.value.has(ins.id) ? 'bg-amber-400' : h.getStatusIconClass(ins)], title: pendingInstrIds.value.has(ins.id) ? 'Pending review' : h.getStatusTooltip(ins) }),
        createElement('span', { class: 'flex-1 text-left truncate' }, displayTitle(ins)),
        createElement(resolveComponent('UIcon'), { name: h.getCategoryIcon(ins.category).replace('heroicons:', 'i-heroicons-'), class: 'w-3 h-3 text-gray-300 shrink-0', title: h.formatCategory(ins.category) }),
        createElement(resolveComponent('UIcon'), { name: h.getSourceIcon(ins), class: 'w-3 h-3 text-gray-300 shrink-0', title: h.getSourceTooltip(ins) }),
        createElement('span', { class: 'shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-gray-100 text-gray-500 text-[11px] font-medium' }, h.getLoadModeLabel(ins.load_mode)),
        (ins.data_sources && ins.data_sources.length > 1) ? createElement('span', { class: 'shrink-0 inline-flex items-center px-1 h-4 rounded bg-gray-100 text-gray-500 text-[11px] font-medium', title: ins.data_sources.map(d => d.name).join(', ') }, String(ins.data_sources.length)) : null,
      ])
    }
  },
})

const EmptyHint = defineComponent({
  props: { text: String, add: Boolean, pad: { type: Number, default: 34 } },
  emits: ['add'],
  setup(props, { emit }) {
    return () => createElement('div', { class: 'flex items-center gap-2 py-1', style: { paddingLeft: props.pad + 'px' } }, [
      createElement('span', { class: 'text-[11px] text-gray-300 italic' }, props.text),
      props.add ? createElement('button', { class: 'text-[11px] text-gray-500 hover:text-gray-900 font-medium', onClick: (e: Event) => { e.stopPropagation(); emit('add') } }, '+ Add') : null,
    ])
  },
})

const FilterSection = defineComponent({
  props: { label: String, options: { type: Array as () => { value: string; label: string }[], default: () => [] }, modelValue: { type: Array as () => string[], default: () => [] } },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    const toggle = (v: string) => { const cur = [...(props.modelValue || [])]; const i = cur.indexOf(v); i >= 0 ? cur.splice(i, 1) : cur.push(v); emit('update:modelValue', cur) }
    return () => createElement('div', {}, [
      createElement('div', { class: 'text-[11px] font-semibold uppercase tracking-wider text-gray-400 mb-1' }, props.label),
      createElement('div', { class: 'flex flex-wrap gap-1' }, props.options.map(o => createElement('button', { key: o.value, type: 'button', class: ['px-2 h-6 rounded-md text-[11px] font-medium transition-colors', (props.modelValue || []).includes(o.value) ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'], onClick: () => toggle(o.value) }, o.label))),
    ])
  },
})

// Deep-link: /agents/<id> opens that agent's overview in the explorer.
// Uses an optional dynamic route ([[id]].vue) so /agents ↔ /agents/<id> share
// the same component — navigation updates the URL without a remount (stays fast).
const route = useRoute()
const router = useRouter()
const openAgentFromRoute = () => {
  const pid = route.params.id
  const id = Array.isArray(pid) ? pid[0] : pid
  if (!id) return
  if (agentView.value?.agentId === id) return
  const agent = agents.value.find(a => a.id === id)
  if (agent) { expand('agent:' + agent.id, true); openAgent(agent.id) }
}
watch(() => route.params.id, () => openAgentFromRoute())

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
  await Promise.all([fetchAgents(), fetchAll(), fetchPendingMap(), fetchLabels(), fetchCategories(), fetchGitStatus()])
  openAgentFromRoute()
})
</script>

<style scoped>
.prose-instruction :deep(.tiptap-prose) { min-height: 80px; }
/* Instruction body text size. */
.prose-instruction :deep(.tiptap-prose),
.prose-instruction :deep(.tiptap-prose p),
.prose-instruction :deep(.tiptap-prose li) { font-size: 13px; line-height: 1.6; }
</style>
